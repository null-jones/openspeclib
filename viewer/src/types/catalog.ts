export type MaterialCategory =
  | 'mineral'
  | 'rock'
  | 'soil'
  | 'vegetation'
  | 'npv'
  | 'water'
  | 'snow_ice'
  | 'man_made'
  | 'meteorite'
  | 'lunar'
  | 'organic_compound'
  | 'mixture'
  | 'other';

export type MeasurementTechnique =
  | 'reflectance'
  | 'emissivity'
  | 'absorbance'
  | 'transmittance';

export type WavelengthUnit = 'um' | 'nm' | 'cm-1';

export interface CatalogRecord {
  id: string;
  name: string;
  chunk_file: string;
  source: {
    library: string;
    library_version: string;
    original_id: string;
    filename: string | null;
    url: string;
    license: string;
    citation: string;
  };
  material: {
    name: string;
    category: MaterialCategory;
    subcategory: string | null;
    formula: string | null;
    keywords: string[];
  };
  sample: {
    id: string | null;
    description: string | null;
    particle_size: string | null;
    origin: string | null;
    owner: string | null;
    collection_date: string | null;
    preparation: string | null;
  };
  measurement: {
    instrument: string | null;
    instrument_type: string | null;
    laboratory: string | null;
    technique: MeasurementTechnique;
    geometry: string | null;
    date: string | null;
  };
  spectral_data: {
    type: MeasurementTechnique;
    wavelength_unit: WavelengthUnit;
    wavelength_min: number;
    wavelength_max: number;
    num_points: number;
  };
  quality: {
    has_bad_bands: boolean;
    bad_band_count: number;
    coverage_fraction: number;
    notes: string | null;
  };
  additional_properties: Record<string, unknown>;
}

export interface CatalogFile {
  openspeclib_version: string;
  generated_at: string;
  sources: Record<string, {
    name: string;
    version: string;
    url: string;
    license: string;
    citation: string;
    spectrum_count: number;
  }>;
  statistics: {
    total_spectra: number;
    categories: Record<string, number>;
  };
  spectra: CatalogRecord[];
}

export interface LicenseEntry {
  name: string;
  version: string;
  url: string;
  license: string;
  license_url: string | null;
  citation: string;
  citation_doi: string | null;
}

export interface LicensesFile {
  openspeclib_version: string;
  generated_at: string;
  notice: string;
  sources: Record<string, LicenseEntry>;
}

/** Full spectrum data including wavelength/value arrays (from Parquet) */
export interface SpectrumFull {
  id: string;
  name: string;
  material_name: string;
  material_category: MaterialCategory;
  material_formula: string | null;
  measurement_technique: MeasurementTechnique;
  wavelength_unit: WavelengthUnit;
  wavelengths: number[];
  values: number[];
  source_library: string;
}
