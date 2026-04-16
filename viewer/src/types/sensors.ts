export interface SensorBand {
  name: string;
  centerWavelength: number; // micrometers
  fwhm: number; // micrometers
}

export interface SensorDefinition {
  id: string;
  name: string;
  description: string;
  bands: SensorBand[];
  wavelengthMin: number; // micrometers — min band edge
  wavelengthMax: number; // micrometers — max band edge
}
