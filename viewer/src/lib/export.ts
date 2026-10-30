import JSZip from 'jszip';
import type { DownsampledResult } from './downsampling';
import type { CollectionMeta, SpectrumFull } from './data/types';
import { attributionLines, buildAttribution } from './data/attribution';
import { isMissingValue, toMicrometersWithValues } from './wavelength-utils';
import { OPENSPECLIB_VERSION } from '../constants/urls';

export interface ExportSpectrum {
  spectrum: SpectrumFull;
  downsampled: DownsampledResult[] | null;
}

export type ExportFormat = 'csv' | 'envi';

const GENERATOR = `the OpenSpecLib viewer (release ${OPENSPECLIB_VERSION})`;

/** Attribution text for the exported spectra (licences, citations). */
export function exportAttribution(items: ExportSpectrum[], collections: Map<string, CollectionMeta>): string[] {
  return attributionLines(buildAttribution(items.map((i) => i.spectrum), collections), GENERATOR);
}

function csvCell(v: string): string {
  return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}

/** A float64 (wavelength) in full. */
function num(v: number): string {
  return Number.isFinite(v) ? String(v) : 'NaN';
}

/** A float32 value: ~7 significant digits, so 0.1f prints as 0.1, not 0.10000000149011612. */
function num32(v: number): string {
  return Number.isFinite(v) ? String(Number(v.toPrecision(7))) : 'NaN';
}

/** One `#` comment line per line of text (citations may contain newlines). */
function commentLines(text: string): string[] {
  return text.split(/\r?\n/).map((l) => (l ? `# ${l}` : '#'));
}

/**
 * CSV: an attribution block of `#` comment lines, then one section per
 * spectrum (or one table of sensor bands when downsampled). Wavelengths stay
 * in each spectrum's native unit; NaN marks missing bands.
 */
export function buildCsv(items: ExportSpectrum[], useDownsampled: boolean, attribution: string[]): string {
  const out = attribution.flatMap(commentLines);
  out.push('');
  if (useDownsampled && items.some((i) => i.downsampled)) {
    const firstDs = items.find((i) => i.downsampled)!.downsampled!;
    const missing = notDownsampled(items);
    if (missing.length) {
      out.push(...commentLines(`Not downsampled (no usable overlap with the sensor's bands; written as NaN): ${missing.join(', ')}`), '');
    }
    out.push(['id', 'name', 'quantity', 'license', ...firstDs.map((b) => `${b.bandName} (${b.centerWavelength} um)`)].map(csvCell).join(','));
    for (const i of items) {
      const s = i.spectrum;
      const bandValues = i.downsampled
        ? i.downsampled.map((b) => (b.value !== null ? num32(b.value) : 'NaN'))
        : firstDs.map(() => 'NaN');
      out.push([s.id, s.name, s.quantity, s.license, ...bandValues].map(csvCell).join(','));
    }
    return out.join('\n') + '\n';
  }
  for (const { spectrum: s } of items) {
    out.push(...commentLines(`id: ${s.id}`));
    out.push(...commentLines(`name: ${s.name}; material: ${s.material_name}; collection: ${s.collection}`));
    out.push(...commentLines(`quantity: ${s.quantity}; geometry: ${s.geometry}; license: ${s.license}${s.dataset_id ? `; dataset: ${s.dataset_id}` : ''}`));
    out.push(`wavelength_${s.wavelength_unit.replace('-', '')},${s.quantity}`);
    for (let k = 0; k < s.wavelengths.length; k++) out.push(`${num(s.wavelengths[k])},${num32(s.values[k])}`);
    out.push('');
  }
  return out.join('\n');
}

/** Ids of spectra a downsampled export holds without downsampled values. */
function notDownsampled(items: ExportSpectrum[]): string[] {
  return items.filter((i) => !i.downsampled).map((i) => i.spectrum.id);
}

/** ENVI no-data value, declared in the header's `data ignore value`. */
export const NODATA = -9999;


function sameGrid(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > 1e-9) return false;
  return true;
}

/** Linear interpolation; NODATA outside the axis or next to a missing value. */
function linearInterp(xSrc: number[], ySrc: number[], x: number): number {
  const n = xSrc.length;
  if (n === 0 || x < xSrc[0] || x > xSrc[n - 1]) return NODATA;
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xSrc[mid] <= x) lo = mid;
    else hi = mid;
  }
  const [y0, y1] = [ySrc[lo], ySrc[hi]];
  if (isMissingValue(y0) || isMissingValue(y1)) return NODATA;
  const span = xSrc[hi] - xSrc[lo];
  return span === 0 ? y0 : y0 + ((x - xSrc[lo]) / span) * (y1 - y0);
}

/** ENVI header values cannot hold commas or braces inside a list. */
function enviName(s: string): string {
  return s.replace(/[,{}]/g, ' ').replace(/\s+/g, ' ').trim();
}

export interface EnviFiles {
  sli: ArrayBuffer;
  hdr: string;
  attribution: string;
}

/**
 * ENVI spectral library: every spectrum on one wavelength grid in µm (the
 * first spectrum's, or the sensor bands when downsampled), float32, BSQ.
 */
export function buildEnvi(items: ExportSpectrum[], useDownsampled: boolean, attribution: string[]): EnviFiles {
  let wavelengths: number[];
  const names: string[] = [];
  const data: Float32Array[] = [];
  const used: SpectrumFull[] = [];

  if (useDownsampled && items.some((i) => i.downsampled)) {
    const firstDs = items.find((i) => i.downsampled)!.downsampled!;
    wavelengths = firstDs.map((b) => b.centerWavelength);
    for (const item of items) {
      used.push(item.spectrum);
      names.push(item.spectrum.name);
      data.push(
        item.downsampled ? Float32Array.from(item.downsampled, (b) => b.value ?? NODATA) : new Float32Array(wavelengths.length).fill(NODATA),
      );
    }
  } else {
    const first = items[0].spectrum;
    wavelengths = toMicrometersWithValues(first.wavelengths, first.values, first.wavelength_unit).wavelengths;
    for (const { spectrum: s } of items) {
      const { wavelengths: wl, values } = toMicrometersWithValues(s.wavelengths, s.values, s.wavelength_unit);
      used.push(s);
      names.push(s.name);
      // Same length is not the same grid: compare the values.
      data.push(
        sameGrid(wl, wavelengths)
          ? Float32Array.from(values, (v) => (isMissingValue(v) ? NODATA : v))
          : Float32Array.from(wavelengths, (x) => linearInterp(wl, values, x)),
      );
    }
  }

  const samples = wavelengths.length;
  const lines = data.length;
  const sli = new ArrayBuffer(lines * samples * 4);
  const view = new DataView(sli);
  for (let l = 0; l < lines; l++) {
    for (let k = 0; k < samples; k++) view.setFloat32((l * samples + k) * 4, data[l][k], true);
  }
  const quantities = [...new Set(used.map((s) => s.quantity))].join(', ');
  const hdr = [
    'ENVI',
    `description = {OpenSpecLib viewer export. Values: ${quantities}. Licences and citations: attribution.txt}`,
    `samples = ${samples}`,
    `lines = ${lines}`,
    'bands = 1',
    'header offset = 0',
    'file type = ENVI Spectral Library',
    'data type = 4',
    'interleave = bsq',
    'byte order = 0',
    `data ignore value = ${NODATA}`,
    'wavelength units = Micrometers',
    `wavelength = {${wavelengths.map((w) => w.toFixed(6)).join(', ')}}`,
    `spectra names = {${names.map(enviName).join(', ')}}`,
    '',
  ].join('\n');

  const table = ['', 'Spectra (line, id, quantity, geometry, licence):'];
  used.forEach((s, i) => table.push(`${i + 1}\t${s.id}\t${s.quantity}\t${s.geometry}\t${s.license}`));
  const missing = useDownsampled ? notDownsampled(items) : [];
  if (missing.length) {
    table.push('', `Not downsampled (no usable overlap with the sensor's bands; written as ${NODATA}): ${missing.join(', ')}`);
  }
  return { sli, hdr, attribution: [...attribution, ...table, ''].join('\n') };
}

export function exportLibrary(
  items: ExportSpectrum[],
  useDownsampled: boolean,
  format: ExportFormat,
  collections: Map<string, CollectionMeta>,
): void {
  if (items.length === 0) return;
  const attribution = exportAttribution(items, collections);
  if (format === 'csv') {
    download(new Blob([buildCsv(items, useDownsampled, attribution)], { type: 'text/csv' }), 'spectral_library.csv');
    return;
  }
  const files = buildEnvi(items, useDownsampled, attribution);
  const zip = new JSZip();
  zip.file('spectral_library.sli', files.sli);
  zip.file('spectral_library.hdr', files.hdr);
  zip.file('attribution.txt', files.attribution);
  void zip.generateAsync({ type: 'blob' }).then((blob) => download(blob, 'spectral_library_envi.zip'));
}

function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
