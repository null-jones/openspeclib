import type { SensorDefinition } from '../types/sensors';

export const SENTINEL2: SensorDefinition = {
  id: 'sentinel2',
  name: 'Sentinel-2 MSI',
  description: '13-band multispectral imager (10–60 m)',
  bands: [
    { name: 'B1',  centerWavelength: 0.4430, fwhm: 0.0200 },
    { name: 'B2',  centerWavelength: 0.4900, fwhm: 0.0650 },
    { name: 'B3',  centerWavelength: 0.5600, fwhm: 0.0350 },
    { name: 'B4',  centerWavelength: 0.6650, fwhm: 0.0300 },
    { name: 'B5',  centerWavelength: 0.7050, fwhm: 0.0150 },
    { name: 'B6',  centerWavelength: 0.7400, fwhm: 0.0150 },
    { name: 'B7',  centerWavelength: 0.7830, fwhm: 0.0200 },
    { name: 'B8',  centerWavelength: 0.8330, fwhm: 0.1150 },
    { name: 'B8a', centerWavelength: 0.8650, fwhm: 0.0200 },
    { name: 'B9',  centerWavelength: 0.9450, fwhm: 0.0200 },
    { name: 'B10', centerWavelength: 1.3750, fwhm: 0.0300 },
    { name: 'B11', centerWavelength: 1.6100, fwhm: 0.0900 },
    { name: 'B12', centerWavelength: 2.1900, fwhm: 0.1800 },
  ],
  wavelengthMin: 0.433,
  wavelengthMax: 2.280,
};

export const LANDSAT8: SensorDefinition = {
  id: 'landsat8',
  name: 'Landsat 8/9 OLI',
  description: '9-band multispectral imager (15–30 m)',
  bands: [
    { name: 'B1', centerWavelength: 0.4430, fwhm: 0.0200 },
    { name: 'B2', centerWavelength: 0.4820, fwhm: 0.0600 },
    { name: 'B3', centerWavelength: 0.5615, fwhm: 0.0575 },
    { name: 'B4', centerWavelength: 0.6545, fwhm: 0.0375 },
    { name: 'B5', centerWavelength: 0.8650, fwhm: 0.0280 },
    { name: 'B6', centerWavelength: 1.6090, fwhm: 0.0850 },
    { name: 'B7', centerWavelength: 2.2010, fwhm: 0.1870 },
    { name: 'B8', centerWavelength: 0.5920, fwhm: 0.1720 },
    { name: 'B9', centerWavelength: 1.3730, fwhm: 0.0205 },
  ],
  wavelengthMin: 0.433,
  wavelengthMax: 2.295,
};

/** Wyvern Dragonette-002/003 — 32 VNIR bands */
export const WYVERN: SensorDefinition = {
  id: 'wyvern',
  name: 'Wyvern Dragonette',
  description: '32-band hyperspectral VNIR (5.3 m)',
  bands: [
    { name: 'B1',  centerWavelength: 0.4450, fwhm: 0.0180 },
    { name: 'B2',  centerWavelength: 0.4590, fwhm: 0.0185 },
    { name: 'B3',  centerWavelength: 0.4730, fwhm: 0.0190 },
    { name: 'B4',  centerWavelength: 0.4870, fwhm: 0.0195 },
    { name: 'B5',  centerWavelength: 0.5010, fwhm: 0.0200 },
    { name: 'B6',  centerWavelength: 0.5150, fwhm: 0.0205 },
    { name: 'B7',  centerWavelength: 0.5290, fwhm: 0.0210 },
    { name: 'B8',  centerWavelength: 0.5430, fwhm: 0.0215 },
    { name: 'B9',  centerWavelength: 0.5570, fwhm: 0.0220 },
    { name: 'B10', centerWavelength: 0.5710, fwhm: 0.0225 },
    { name: 'B11', centerWavelength: 0.5850, fwhm: 0.0230 },
    { name: 'B12', centerWavelength: 0.5990, fwhm: 0.0235 },
    { name: 'B13', centerWavelength: 0.6130, fwhm: 0.0240 },
    { name: 'B14', centerWavelength: 0.6270, fwhm: 0.0245 },
    { name: 'B15', centerWavelength: 0.6410, fwhm: 0.0250 },
    { name: 'B16', centerWavelength: 0.6550, fwhm: 0.0255 },
    { name: 'B17', centerWavelength: 0.6690, fwhm: 0.0260 },
    { name: 'B18', centerWavelength: 0.6830, fwhm: 0.0265 },
    { name: 'B19', centerWavelength: 0.6970, fwhm: 0.0270 },
    { name: 'B20', centerWavelength: 0.7110, fwhm: 0.0275 },
    { name: 'B21', centerWavelength: 0.7250, fwhm: 0.0280 },
    { name: 'B22', centerWavelength: 0.7390, fwhm: 0.0285 },
    { name: 'B23', centerWavelength: 0.7530, fwhm: 0.0290 },
    { name: 'B24', centerWavelength: 0.7670, fwhm: 0.0295 },
    { name: 'B25', centerWavelength: 0.7810, fwhm: 0.0300 },
    { name: 'B26', centerWavelength: 0.7950, fwhm: 0.0310 },
    { name: 'B27', centerWavelength: 0.8090, fwhm: 0.0320 },
    { name: 'B28', centerWavelength: 0.8230, fwhm: 0.0330 },
    { name: 'B29', centerWavelength: 0.8370, fwhm: 0.0335 },
    { name: 'B30', centerWavelength: 0.8510, fwhm: 0.0340 },
    { name: 'B31', centerWavelength: 0.8650, fwhm: 0.0345 },
    { name: 'B32', centerWavelength: 0.8800, fwhm: 0.0350 },
  ],
  wavelengthMin: 0.436,
  wavelengthMax: 0.898,
};

/** EnMAP — ~228 hyperspectral bands, programmatically generated */
function generateEnmapBands() {
  const bands: { name: string; centerWavelength: number; fwhm: number }[] = [];
  const fwhm = 0.012; // 12 nm

  // VNIR: 420–1000 nm, 6.5 nm spacing
  let idx = 1;
  for (let wl = 0.42; wl <= 1.0; wl += 0.0065) {
    bands.push({ name: `B${idx}`, centerWavelength: Math.round(wl * 10000) / 10000, fwhm });
    idx++;
  }

  // SWIR: 900–2450 nm, 10 nm spacing (skip overlap with VNIR above 1000nm)
  for (let wl = 1.01; wl <= 2.45; wl += 0.01) {
    bands.push({ name: `B${idx}`, centerWavelength: Math.round(wl * 10000) / 10000, fwhm });
    idx++;
  }

  return bands;
}

const enmapBands = generateEnmapBands();

export const ENMAP: SensorDefinition = {
  id: 'enmap',
  name: 'EnMAP',
  description: `~${enmapBands.length}-band hyperspectral (30 m)`,
  bands: enmapBands,
  wavelengthMin: 0.414,
  wavelengthMax: 2.456,
};

export const ALL_SENSORS: SensorDefinition[] = [
  SENTINEL2,
  LANDSAT8,
  WYVERN,
  ENMAP,
];
