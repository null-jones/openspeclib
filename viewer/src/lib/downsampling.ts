import type { SensorBand } from '../types/sensors';

export interface DownsampledResult {
  bandName: string;
  centerWavelength: number; // um
  value: number | null;
}

/**
 * Downsample a high-resolution spectrum to sensor bands using Gaussian SRF convolution.
 *
 * SRF(λ) = exp(-4·ln(2)·((λ − λ_c) / FWHM)²)
 * R_band = ∫R(λ)·SRF(λ)dλ / ∫SRF(λ)dλ
 *
 * @param wavelengths Sorted ascending, in micrometers
 * @param values Reflectance values corresponding to wavelengths
 * @param bands Sensor band definitions (center + FWHM in micrometers)
 */
export function downsampleSpectrum(
  wavelengths: number[],
  values: number[],
  bands: SensorBand[],
): DownsampledResult[] {
  const results: DownsampledResult[] = [];

  for (const band of bands) {
    const weights: number[] = new Array(wavelengths.length);
    const weightedValues: number[] = new Array(wavelengths.length);

    let hasOverlap = false;
    for (let i = 0; i < wavelengths.length; i++) {
      const dl = (wavelengths[i] - band.centerWavelength) / band.fwhm;
      const srf = Math.exp(-4 * Math.LN2 * dl * dl);
      weights[i] = srf;
      weightedValues[i] = values[i] * srf;
      if (srf > 0.01) hasOverlap = true;
    }

    if (!hasOverlap) {
      results.push({
        bandName: band.name,
        centerWavelength: band.centerWavelength,
        value: null,
      });
      continue;
    }

    const numerator = trapz(wavelengths, weightedValues);
    const denominator = trapz(wavelengths, weights);

    results.push({
      bandName: band.name,
      centerWavelength: band.centerWavelength,
      value: denominator > 1e-10 ? numerator / denominator : null,
    });
  }

  return results;
}

function trapz(x: number[], y: number[]): number {
  let sum = 0;
  for (let i = 1; i < x.length; i++) {
    sum += 0.5 * (y[i] + y[i - 1]) * (x[i] - x[i - 1]);
  }
  return sum;
}
