import { getWavelengthGrid, query, SOURCES } from './duckdb';
import type { SpectrumFull } from '../types/catalog';

/** Fetch full spectral data for a list of spectrum IDs. */
export async function fetchSpectraByIds(ids: string[]): Promise<SpectrumFull[]> {
  if (ids.length === 0) return [];

  const inList = ids.map((id) => `'${id.replace(/'/g, "''")}'`).join(',');
  // Wavelengths are stored once per unique grid in wavelengths.parquet and
  // referenced by an int32 grid_id from each spectrum row, so the per-spectrum
  // SELECT pulls only the small id reference. The full axis is rehydrated
  // client-side from the registry materialised at initDuckDB.
  const columns = `
      id,
      name,
      "material.name" AS material_name,
      "material.category" AS material_category,
      "material.formula" AS material_formula,
      "measurement.technique" AS measurement_technique,
      "spectral_data.wavelength_unit" AS wavelength_unit,
      "spectral_data.wavelength_grid_id" AS wavelength_grid_id,
      "spectral_data.values" AS "values",
      "source.library" AS source_library`;
  const sql = SOURCES.map(
    (s) => `SELECT ${columns} FROM ${s} WHERE id IN (${inList})`,
  ).join('\nUNION ALL\n');

  const result = await query(sql);
  const spectra: SpectrumFull[] = [];

  for (let i = 0; i < result.numRows; i++) {
    const row = result.get(i);
    if (!row) continue;
    const gridId = row.wavelength_grid_id as number;
    const wavelengths = Array.from(getWavelengthGrid(gridId));
    spectra.push({
      id: row.id as string,
      name: row.name as string,
      material_name: row.material_name as string,
      material_category: row.material_category as string as SpectrumFull['material_category'],
      material_formula: row.material_formula as string | null,
      measurement_technique: row.measurement_technique as string as SpectrumFull['measurement_technique'],
      wavelength_unit: row.wavelength_unit as string as SpectrumFull['wavelength_unit'],
      wavelengths,
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
