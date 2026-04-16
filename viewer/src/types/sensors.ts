export interface SensorBand {
  name: string;
  centerWavelength: number; // micrometers
  fwhm: number; // micrometers
}

export type SensorGroup =
  | 'Public Multispectral'
  | 'Commercial Multispectral'
  | 'Public Hyperspectral'
  | 'Commercial Hyperspectral';

export interface SensorDefinition {
  id: string;
  name: string;
  description: string;
  group: SensorGroup;
  source: string; // citation or reference for the band parameters
  sourceUrl: string; // URL to the source document
  bands: SensorBand[];
  wavelengthMin: number; // micrometers — min band edge
  wavelengthMax: number; // micrometers — max band edge
}
