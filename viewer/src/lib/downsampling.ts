import type { SensorBand } from '../types/sensors';
import { isMissingValue } from './wavelength-utils';

export interface DownsampledResult {
  bandName: string;
  centerWavelength: number; // um
  value: number | null;
}

/**
 * A band gets a value only when valid data carry at least this share of its
 * spectral response; otherwise (band outside the axis, or across a gap of
 * missing bands) it is null rather than an average of a few edge samples.
 */
export const MIN_VALID_SRF_SHARE = 0.9;

/** ∫ exp(-4 ln2 ((λ-c)/FWHM)²) dλ over all λ. */
function gaussianArea(fwhm: number): number {
  return fwhm * Math.sqrt(Math.PI / (4 * Math.LN2));
}

/**
 * Downsample a high-resolution spectrum to sensor bands using Gaussian SRF convolution.
 *
 * SRF(λ) = exp(-4·ln(2)·((λ − λ_c) / FWHM)²)
 * R_band = ∫R(λ)·SRF(λ)dλ / ∫SRF(λ)dλ, integrated over the valid parts of the axis
 *
 * @param wavelengths Sorted ascending, in micrometers
 * @param values Values at those wavelengths; NaN (or a sentinel) marks a missing band
 * @param bands Sensor band definitions (center + FWHM in micrometers)
 */
export function downsampleSpectrum(
  wavelengths: number[],
  values: number[],
  bands: SensorBand[],
  minShare = MIN_VALID_SRF_SHARE,
): DownsampledResult[] {
  const valid = values.map((v) => !isMissingValue(v));
  return bands.map((band) => {
    let num = 0;
    let den = 0;
    const srf = (w: number) => {
      const dl = (w - band.centerWavelength) / band.fwhm;
      return Math.exp(-4 * Math.LN2 * dl * dl);
    };
    for (let i = 1; i < wavelengths.length; i++) {
      // Trapezoids only between two valid samples: gaps contribute nothing.
      if (!valid[i] || !valid[i - 1]) continue;
      const dx = wavelengths[i] - wavelengths[i - 1];
      if (!(dx > 0)) continue;
      const s0 = srf(wavelengths[i - 1]);
      const s1 = srf(wavelengths[i]);
      den += 0.5 * (s0 + s1) * dx;
      num += 0.5 * (values[i - 1] * s0 + values[i] * s1) * dx;
    }
    const share = den / gaussianArea(band.fwhm);
    return {
      bandName: band.name,
      centerWavelength: band.centerWavelength,
      value: share >= minShare && den > 0 ? num / den : null,
    };
  });
}
