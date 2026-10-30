/**
 * The controlled vocabularies alone, for the explorer (the full spec module
 * stays in the lazily loaded Format area).
 */
import vocabulariesDoc from '@spec/vocabularies.json';

export const QUANTITIES: Record<string, string> = vocabulariesDoc.quantity;
export const GEOMETRIES: Record<string, string> = vocabulariesDoc.geometry;

/** "directional_hemispherical" → "directional hemispherical". */
export function vocabLabel(value: string): string {
  return value.replace(/_/g, ' ');
}

/** "reflectance" → "Reflectance". */
export function capitalise(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
