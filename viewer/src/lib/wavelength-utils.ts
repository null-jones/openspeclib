import type { WavelengthUnit } from '../types/catalog';

/** Convert wavelengths to micrometers. Returns a new sorted ascending array. */
export function toMicrometers(
  wavelengths: number[],
  unit: WavelengthUnit,
): number[] {
  switch (unit) {
    case 'um':
      return wavelengths;
    case 'nm':
      return wavelengths.map((w) => w / 1000);
    case 'cm-1': {
      // cm-1 → um: λ(μm) = 10000 / ν(cm⁻¹). Reverses order.
      const converted = wavelengths.map((w) => 10000 / w);
      converted.reverse();
      return converted;
    }
  }
}

/** Also reorder the values array to match reversed wavenumber conversion. */
export function toMicrometersWithValues(
  wavelengths: number[],
  values: number[],
  unit: WavelengthUnit,
): { wavelengths: number[]; values: number[] } {
  if (unit === 'cm-1') {
    const converted = wavelengths.map((w) => 10000 / w);
    const reordered = [...values].reverse();
    converted.reverse();
    return { wavelengths: converted, values: reordered };
  }
  return { wavelengths: toMicrometers(wavelengths, unit), values };
}

export function unitLabel(unit: WavelengthUnit): string {
  switch (unit) {
    case 'um':
      return 'Wavelength (μm)';
    case 'nm':
      return 'Wavelength (nm)';
    case 'cm-1':
      return 'Wavenumber (cm⁻¹)';
  }
}
