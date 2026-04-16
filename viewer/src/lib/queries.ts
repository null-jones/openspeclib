import { query } from './duckdb';
import type { SpectrumFull } from '../types/catalog';

/** Fetch full spectral data for a list of spectrum IDs. */
export async function fetchSpectraByIds(ids: string[]): Promise<SpectrumFull[]> {
  if (ids.length === 0) return [];

  const inList = ids.map((id) => `'${id.replace(/'/g, "''")}'`).join(',');
  const sql = `
    SELECT
      id,
      name,
      "material.name" AS material_name,
      "material.category" AS material_category,
      "material.formula" AS material_formula,
      "measurement.technique" AS measurement_technique,
      "spectral_data.wavelength_unit" AS wavelength_unit,
      "spectral_data.wavelengths" AS wavelengths,
      "spectral_data.values" AS "values",
      "source.library" AS source_library
    FROM usgs_splib07
    WHERE id IN (${inList})
  `;

  const result = await query(sql);
  const spectra: SpectrumFull[] = [];

  for (let i = 0; i < result.numRows; i++) {
    const row = result.get(i);
    if (!row) continue;
    spectra.push({
      id: row.id as string,
      name: row.name as string,
      material_name: row.material_name as string,
      material_category: row.material_category as string as SpectrumFull['material_category'],
      material_formula: row.material_formula as string | null,
      measurement_technique: row.measurement_technique as string as SpectrumFull['measurement_technique'],
      wavelength_unit: row.wavelength_unit as string as SpectrumFull['wavelength_unit'],
      wavelengths: arrowListToArray(row.wavelengths),
      values: arrowListToArray(row.values),
      source_library: row.source_library as string,
    });
  }

  return spectra;
}

/** Convert Arrow list/vector to plain JS number array. */
function arrowListToArray(val: unknown): number[] {
  if (Array.isArray(val)) return val;
  if (val && typeof val === 'object' && 'toArray' in val) {
    return Array.from((val as { toArray(): number[] }).toArray());
  }
  return [];
}
