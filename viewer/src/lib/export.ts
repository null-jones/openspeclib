import JSZip from 'jszip';
import type { DownsampledResult } from './downsampling';
import type { SpectrumFull } from '../types/catalog';
import { toMicrometersWithValues } from './wavelength-utils';

export interface ExportSpectrum {
  spectrum: SpectrumFull;
  downsampled: DownsampledResult[] | null;
}

export type ExportFormat = 'csv' | 'envi';

export function exportLibrary(
  items: ExportSpectrum[],
  useDownsampled: boolean,
  format: ExportFormat,
): void {
  if (format === 'csv') {
    exportCSV(items, useDownsampled);
  } else {
    exportENVI(items, useDownsampled);
  }
}

/** Export as CSV. */
function exportCSV(items: ExportSpectrum[], useDownsampled: boolean): void {
  let csv: string;

  if (useDownsampled && items.some((i) => i.downsampled)) {
    const firstDs = items.find((i) => i.downsampled)!.downsampled!;
    const header = ['name', ...firstDs.map((b) => `${b.bandName} (${b.centerWavelength} um)`)];
    const rows = items
      .filter((i) => i.downsampled)
      .map((i) => {
        const vals = i.downsampled!.map((b) =>
          b.value !== null ? b.value.toFixed(6) : '',
        );
        return [i.spectrum.name, ...vals];
      });
    csv = [header.join(','), ...rows.map((r) => r.join(','))].join('\n');
  } else {
    const sections: string[] = [];
    for (const item of items) {
      const s = item.spectrum;
      sections.push(`# ${s.name} (${s.material_name}, ${s.source_library})`);
      sections.push('wavelength_um,value');
      for (let i = 0; i < s.wavelengths.length; i++) {
        sections.push(`${s.wavelengths[i]},${s.values[i]}`);
      }
      sections.push('');
    }
    csv = sections.join('\n');
  }

  downloadBlob(csv, 'spectral_library.csv', 'text/csv');
}

/**
 * Export as ENVI spectral library (.sli + .hdr) in a zip.
 *
 * ENVI spectral library format:
 * - .sli: flat binary, float32, BSQ interleave (each spectrum is one "line")
 * - .hdr: ASCII header with metadata
 *
 * All spectra are resampled to a common wavelength grid.
 * When downsampled, the sensor bands become the grid.
 */
function exportENVI(items: ExportSpectrum[], useDownsampled: boolean): void {
  let wavelengths: number[];
  let spectraNames: string[];
  let spectraData: Float32Array[];

  if (useDownsampled && items.some((i) => i.downsampled)) {
    // Use downsampled band wavelengths as the grid
    const firstDs = items.find((i) => i.downsampled)!.downsampled!;
    wavelengths = firstDs.map((b) => b.centerWavelength);
    spectraNames = [];
    spectraData = [];

    for (const item of items) {
      if (!item.downsampled) continue;
      spectraNames.push(item.spectrum.name);
      const vals = new Float32Array(wavelengths.length);
      for (let i = 0; i < item.downsampled.length; i++) {
        vals[i] = item.downsampled[i].value ?? -9999;
      }
      spectraData.push(vals);
    }
  } else {
    // Find the spectrum with most points and use its wavelengths
    // For simplicity, use a common grid: the first spectrum's wavelengths
    // (ENVI requires all spectra on the same grid)
    const first = items[0].spectrum;
    const { wavelengths: wl } = toMicrometersWithValues(
      first.wavelengths,
      first.values,
      first.wavelength_unit,
    );
    wavelengths = wl;
    spectraNames = [];
    spectraData = [];

    for (const item of items) {
      const s = item.spectrum;
      const { wavelengths: srcWl, values: srcVal } = toMicrometersWithValues(
        s.wavelengths,
        s.values,
        s.wavelength_unit,
      );
      spectraNames.push(s.name);

      // Interpolate onto the common grid if needed
      if (srcWl.length === wavelengths.length) {
        // Filter sentinel values
        const vals = new Float32Array(srcVal.length);
        for (let i = 0; i < srcVal.length; i++) {
          vals[i] = srcVal[i] < -1e10 || srcVal[i] > 1e10 ? -9999 : srcVal[i];
        }
        spectraData.push(vals);
      } else {
        // Linear interpolation to match the common grid
        const vals = new Float32Array(wavelengths.length);
        for (let i = 0; i < wavelengths.length; i++) {
          vals[i] = linearInterp(srcWl, srcVal, wavelengths[i]);
        }
        spectraData.push(vals);
      }
    }
  }

  const samples = wavelengths.length; // number of bands
  const lines = spectraData.length;   // number of spectra

  // Build binary .sli (BSQ: each spectrum is one "line")
  const sli = new ArrayBuffer(lines * samples * 4);
  const sliView = new DataView(sli);
  for (let line = 0; line < lines; line++) {
    for (let s = 0; s < samples; s++) {
      sliView.setFloat32((line * samples + s) * 4, spectraData[line][s], true); // little-endian
    }
  }

  // Build .hdr
  const hdr = [
    'ENVI',
    'description = {OpenSpecLib Spectral Library Export}',
    `samples = ${samples}`,
    `lines = ${lines}`,
    'bands = 1',
    'header offset = 0',
    'file type = ENVI Spectral Library',
    'data type = 4',
    'interleave = bsq',
    'byte order = 0',
    `wavelength units = Micrometers`,
    `wavelength = {${wavelengths.map((w) => w.toFixed(6)).join(', ')}}`,
    `spectra names = {${spectraNames.join(', ')}}`,
    '',
  ].join('\n');

  // Zip both files
  const zip = new JSZip();
  zip.file('spectral_library.sli', sli);
  zip.file('spectral_library.hdr', hdr);
  zip.generateAsync({ type: 'blob' }).then((blob) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'spectral_library_envi.zip';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });
}

/** Simple linear interpolation. Returns -9999 for out-of-range. */
function linearInterp(xSrc: number[], ySrc: number[], xTarget: number): number {
  if (xTarget <= xSrc[0]) return ySrc[0] < -1e10 ? -9999 : ySrc[0];
  if (xTarget >= xSrc[xSrc.length - 1]) {
    const last = ySrc[ySrc.length - 1];
    return last < -1e10 ? -9999 : last;
  }
  // Binary search for the bracket
  let lo = 0;
  let hi = xSrc.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xSrc[mid] <= xTarget) lo = mid;
    else hi = mid;
  }
  const y0 = ySrc[lo];
  const y1 = ySrc[hi];
  if (y0 < -1e10 || y1 < -1e10) return -9999;
  const t = (xTarget - xSrc[lo]) / (xSrc[hi] - xSrc[lo]);
  return y0 + t * (y1 - y0);
}

function downloadBlob(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
