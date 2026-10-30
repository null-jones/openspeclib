/** `nm`, `um` or `cm-1` (vocabulary `wavelength_unit`). */
export type WavelengthUnit = string;

/**
 * Whether a spectral value marks a missing band. Format v1 uses NaN only;
 * the magnitude test also guards against sentinels in files that do not
 * conform (spec rule F9).
 */
export function isMissingValue(v: number): boolean {
  return !Number.isFinite(v) || Math.abs(v) > 1e10;
}

/** One wavelength in µm: nm / 1000, µm as is, and 10⁴ / ν for wavenumbers in cm⁻¹. */
function toMicrometer(w: number, unit: WavelengthUnit): number {
  switch (unit) {
    case 'nm':
      return w / 1000;
    case 'cm-1':
      return 10_000 / w;
    default:
      return w;
  }
}

/**
 * Convert an axis to µm and sort the (wavelength, value) pairs ascending.
 * Axes may be stored descending (common for wavenumbers, and for some
 * wavelength axes, e.g. 1,772 ECOSTRESS spectra); every consumer here
 * (plot, downsampling, ENVI interpolation) expects ascending wavelengths.
 */
export function toMicrometersWithValues(
  wavelengths: number[],
  values: number[],
  unit: WavelengthUnit,
): { wavelengths: number[]; values: number[] } {
  const n = Math.min(wavelengths.length, values.length);
  const um = new Array<number>(n);
  let ascending = true;
  for (let i = 0; i < n; i++) {
    um[i] = toMicrometer(wavelengths[i], unit);
    if (i > 0 && !(um[i] >= um[i - 1])) ascending = false;
  }
  if (ascending) return { wavelengths: um, values: values.slice(0, n) };
  const order = Array.from({ length: n }, (_, i) => i).sort((a, b) => um[a] - um[b]);
  return { wavelengths: order.map((i) => um[i]), values: order.map((i) => values[i]) };
}
