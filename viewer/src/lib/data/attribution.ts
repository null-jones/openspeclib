/**
 * Licence and citation attribution for a set of spectra, from the per-row
 * `license` and `dataset_id` and the collection footers. Pure. Used by the
 * sidebar and by every export.
 */
import type { CollectionMeta, SpectrumRecord } from './types';
import { own } from '../own';

export interface LicenseUse {
  license: string;
  /** Human-readable name for LicenseRef- identifiers. */
  name?: string;
  url?: string;
  /** The terms of a LicenseRef- identifier, as the footer states them. */
  text?: string;
  count: number;
}

export interface DatasetUse {
  id: string;
  title: string;
  citation?: string;
  license?: string;
  doi?: string;
  url?: string;
  count: number;
}

export interface CollectionUse {
  id: string;
  title: string;
  citation: string;
  publisher?: string;
  url?: string;
  doi?: string;
  version?: string;
  licenseNote?: string;
  licenses: LicenseUse[];
  datasets: DatasetUse[];
  count: number;
}

export function buildAttribution(
  records: SpectrumRecord[],
  collections: Map<string, CollectionMeta>,
): CollectionUse[] {
  const byColl = new Map<string, CollectionUse>();
  for (const r of records) {
    const meta = collections.get(r.collection);
    let use = byColl.get(r.collection);
    if (!use) {
      const c = meta?.footer.collection;
      use = {
        id: r.collection,
        title: c?.title ?? r.collection,
        citation: c?.citation ?? '',
        publisher: c?.publisher,
        url: c?.url,
        doi: c?.doi,
        version: c?.version,
        licenseNote: c?.license_note,
        licenses: [],
        datasets: [],
        count: 0,
      };
      byColl.set(r.collection, use);
    }
    use.count++;
    let lic = use.licenses.find((l) => l.license === r.license);
    if (!lic) {
      const ref = own(meta?.footer.collection.license_refs, r.license);
      lic = { license: r.license, name: ref?.name, url: ref?.url, text: ref?.text, count: 0 };
      use.licenses.push(lic);
    }
    lic.count++;
    if (r.dataset_id) {
      let ds = use.datasets.find((d) => d.id === r.dataset_id);
      if (!ds) {
        const m = own(meta?.footer.datasets, r.dataset_id);
        ds = {
          id: r.dataset_id,
          title: m?.title ?? r.dataset_id,
          citation: m?.citation,
          license: m?.license,
          doi: m?.doi,
          url: m?.url,
          count: 0,
        };
        use.datasets.push(ds);
      }
      ds.count++;
    }
  }
  const out = [...byColl.values()];
  for (const c of out) {
    c.licenses.sort((a, b) => b.count - a.count);
    c.datasets.sort((a, b) => b.count - a.count || a.title.localeCompare(b.title));
  }
  return out.sort((a, b) => b.count - a.count);
}

const spectra = (n: number) => `${n} ${n === 1 ? 'spectrum' : 'spectra'}`;

function licenseLabel(l: LicenseUse): string {
  return l.name ? `${l.license} (${l.name})` : l.license;
}

/** Plain-text attribution block, one line per entry, for exports. */
export function attributionLines(uses: CollectionUse[], generatedBy: string): string[] {
  const lines = [
    'Attribution: spectra in this file come from the collections below.',
    'Each spectrum keeps its own licence; cite the collections and datasets you use.',
    `Exported by ${generatedBy} on ${localDate(new Date())}.`,
    '',
  ];
  for (const c of uses) {
    lines.push(`Collection: ${c.title} [${c.id}]${c.version ? `, version ${c.version}` : ''} (${spectra(c.count)})`);
    if (c.publisher) lines.push(`  Publisher: ${c.publisher}`);
    if (c.doi) lines.push(`  DOI: https://doi.org/${c.doi}`);
    else if (c.url) lines.push(`  URL: ${c.url}`);
    lines.push(`  Licence: ${c.licenses.map((l) => `${licenseLabel(l)}${c.licenses.length > 1 ? ` x${l.count}` : ''}`).join('; ')}`);
    if (c.licenseNote) lines.push(`  Licence note: ${c.licenseNote}`);
    for (const l of c.licenses) {
      if (!l.text && !l.url) continue;
      lines.push(`  ${l.license}:${l.text ? ` ${l.text}` : ''}${l.url ? ` (${l.url})` : ''}`);
    }
    if (c.citation) lines.push(`  Citation: ${c.citation}`);
    for (const d of c.datasets) {
      lines.push(`  Dataset: ${d.title} (${spectra(d.count)})${d.license ? `, licence ${d.license}` : ''}`);
      if (d.citation) lines.push(`    Citation: ${d.citation}`);
      if (d.doi) lines.push(`    DOI: https://doi.org/${d.doi}`);
      if (d.url) lines.push(`    URL: ${d.url}`);
    }
    lines.push('');
  }
  return lines;
}

/** The local calendar date as YYYY-MM-DD (toISOString would give the UTC date). */
export function localDate(d: Date): string {
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
