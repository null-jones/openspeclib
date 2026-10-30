/**
 * The explorer's data model for OpenSpecLib Format v1: search rows built
 * from index (or collection) files, collection metadata from their footers,
 * and spectra fetched from collection files on demand.
 */

/** One spectrum's searchable metadata (no arrays). */
export interface SpectrumRecord {
  id: string;
  /** Key of the DataSource the record came from. */
  sourceKey: string;
  collection: string;
  name: string;
  material_name: string;
  material_category: string;
  material_formula: string | null;
  sample_description: string | null;
  quantity: string;
  geometry: string;
  wavelength_unit: string;
  wavelength_min_nm: number;
  wavelength_max_nm: number;
  /** Length of the spectrum, when the source says (index files do). */
  num_points: number | null;
  license: string;
  dataset_id: string | null;
  /** `simulated` is among the measurement_processing flags. */
  simulated: boolean;
  /** The collection file holding the arrays, as named by the source. */
  file: string;
  /** Lower-cased text the search box matches against. */
  search: string;
}

export interface LicenseRefMeta {
  name: string;
  text?: string;
  url?: string;
}

export interface CollectionInfo {
  id: string;
  title: string;
  description?: string;
  version?: string;
  url?: string;
  doi?: string;
  publisher?: string;
  license: string;
  license_note?: string;
  license_refs?: Record<string, LicenseRefMeta>;
  citation: string;
  inclusion: string;
  inclusion_notes?: string;
  keywords?: string[];
}

export interface DatasetMeta {
  title: string;
  description?: string;
  url?: string;
  doi?: string;
  license?: string;
  license_note?: string;
  citation?: string;
  authors?: string;
  organization?: string;
  inclusion?: string;
  inclusion_notes?: string;
  spectra_available?: number;
  spectra_included?: number;
}

/** The `openspeclib` footer of a collection file. */
export interface CollectionFooter {
  format_version: string;
  collection: CollectionInfo;
  datasets?: Record<string, DatasetMeta>;
  extensions: string[];
  created: string;
  generator: string;
}

/** A collection as the explorer knows it. */
export interface CollectionMeta {
  id: string;
  footer: CollectionFooter;
  sourceKey: string;
  /** The `file` name rows of this collection carry. */
  file: string;
  numSpectra: number;
  sha256?: string;
}

export type SourceKind = 'release' | 'local' | 'url';

/** Where spectra come from: the OpenSpecLib release, or a file the user opened. */
export interface DataSource {
  key: string;
  label: string;
  kind: SourceKind;
  /** Collection ids it provides. */
  collections: string[];
  /** DuckDB table holding its non-array rows (for details). */
  table: string;
  /** Columns of that table. */
  columns: string[];
  /** Map a row's `file` to a reference DuckDB can read. */
  fileRefs: Record<string, string>;
  /** Files registered with DuckDB for this source (dropped when it is removed). */
  registered?: string[];
  /** Index footer fields, for release sources. */
  release?: string;
  created?: string;
  generator?: string;
}

/** A spectrum with its arrays, ready to plot or export. */
export interface SpectrumFull extends SpectrumRecord {
  wavelengths: number[];
  values: number[];
}

/** All non-array columns of one row, for the details panel. */
export type SpectrumDetails = Record<string, unknown>;
