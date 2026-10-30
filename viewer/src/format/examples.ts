/**
 * Worked examples for the Format reference: an example footer and reading /
 * writing snippets. The footer is validated against footer.schema.json and
 * the SQL and JavaScript snippets are run against a fixture in the tests
 * (tests/examples.test.ts), so they cannot drift from the spec unnoticed.
 */

export const EXAMPLE_FILE = 'usgs_splib07.parquet';
export const EXAMPLE_ID = 'usgs_splib07:splib07a_Olivine_GDS70.a_Fo89_165um_BECKb_AREF';

export const EXAMPLE_FOOTER = {
  format_version: '1.0.0',
  collection: {
    id: 'usgs_splib07',
    title: 'USGS Spectral Library Version 7',
    version: '7a',
    url: 'https://doi.org/10.5066/F7RR1WDJ',
    doi: '10.5066/F7RR1WDJ',
    license: 'LicenseRef-USGS-PublicDomain',
    license_refs: {
      'LicenseRef-USGS-PublicDomain': {
        name: 'U.S. Government work, public domain',
        url: 'https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits',
      },
    },
    citation:
      'Kokaly, R.F., Clark, R.N., Swayze, G.A., Livo, K.E., Hoefen, T.M., Pearson, N.C., Wise, R.A., Benzel, W.M., Lowers, H.A., Driscoll, R.L., and Klein, A.J., 2017, USGS Spectral Library Version 7: U.S. Geological Survey Data Series 1035, 61 p., https://doi.org/10.3133/ds1035.',
    inclusion: 'subset',
    // As in the published file (tests/examples.test.ts compares them).
    inclusion_notes:
      'The measured spectra of splib07a only. The oversampled splib07b copies, the spectra convolved or resampled to other instruments (the cv* and rs* directories) and the per-band error-bar files are derived from the same measurements and are not included.',
  },
  datasets: {},
  extensions: ['material', 'sample', 'measurement', 'bandpass'],
  created: '2026-10-29T12:00:00Z',
  generator: 'openspeclib 0.2.0',
};

export interface Snippet {
  id: string;
  label: string;
  language: string;
  /** Shown as a caveat next to the label. */
  note?: string;
  code: (file: string, id: string) => string;
}

export const READING_SNIPPETS: Snippet[] = [
  {
    id: 'sql',
    label: 'DuckDB SQL',
    language: 'sql',
    code: (file, id) => `-- Collection metadata (the footer JSON)
SELECT decode(value) AS footer
FROM parquet_kv_metadata('${file}')
WHERE decode(key) = 'openspeclib';

-- Find spectra: every reflectance spectrum covering 400–2500 nm
SELECT id, name, material_name, geometry, wavelength_unit
FROM '${file}'
WHERE quantity = 'reflectance'
  AND wavelength_min_nm <= 400 AND wavelength_max_nm >= 2500;

-- One spectrum as (wavelength, value) rows. "values" is quoted: it is an SQL keyword.
SELECT unnest(wavelengths) AS wavelength, unnest("values") AS value
FROM '${file}'
WHERE id = '${id}';`,
  },
  {
    id: 'python',
    label: 'Python (pyarrow)',
    language: 'python',
    code: (file, id) => `import json
import pyarrow.parquet as pq

pf = pq.ParquetFile("${file}")
footer = json.loads(pf.metadata.metadata[b"openspeclib"])
print(footer["collection"]["title"], footer["collection"]["license"])

table = pq.read_table(
    "${file}",
    columns=["id", "quantity", "geometry", "wavelength_unit", "wavelengths", "values"],
    filters=[("id", "=", "${id}")],
)
row = table.to_pylist()[0]
wavelengths = row["wavelengths"]  # native unit: row["wavelength_unit"]
values = row["values"]            # NaN marks a missing band`,
  },
  {
    id: 'js',
    label: 'JavaScript (DuckDB-WASM)',
    language: 'javascript',
    code: (file, id) => `// \`db\` is an instantiated AsyncDuckDB (see the DuckDB-WASM docs).
// DuckDB-WASM reads a URL with HTTP range requests: only the footer and
// the row group holding the spectrum are downloaded.
const url = 'https://example.org/${file}';
const conn = await db.connect();

const meta = await conn.query(
  \`SELECT decode(value) AS footer FROM parquet_kv_metadata('\${url}')
   WHERE decode(key) = 'openspeclib'\`);
const footer = JSON.parse(meta.get(0).footer);

const result = await conn.query(
  \`SELECT wavelength_unit, wavelengths, "values" FROM '\${url}'
   WHERE id = '${id}'\`);
const row = result.get(0);
const wavelengths = row.wavelengths.toArray(); // Float64Array
const values = row.values.toArray();           // Float32Array, NaN = missing`,
  },
  {
    id: 'r',
    label: 'R (arrow)',
    language: 'r',
    note: 'untested example',
    code: (file, id) => `library(arrow)
library(dplyr)

tab <- read_parquet("${file}", as_data_frame = FALSE)
footer <- jsonlite::fromJSON(tab$metadata$openspeclib)

spectrum <- tab |>
  filter(id == "${id}") |>
  select(id, wavelength_unit, wavelengths, values) |>
  collect()
wavelengths <- spectrum$wavelengths[[1]]
values <- spectrum$values[[1]]  # NaN marks a missing band`,
  },
];

/** Writing a conformant file with pyarrow (publishing guide). */
export const WRITE_PYARROW = `import json
import pyarrow as pa
import pyarrow.parquet as pq

# One row per spectrum; types as in the core column table.
schema = pa.schema([
    ("id", pa.string()), ("collection", pa.string()), ("original_id", pa.string()),
    ("name", pa.string()), ("material_name", pa.string()),
    ("material_category", pa.string()), ("quantity", pa.string()),
    ("geometry", pa.string()), ("wavelength_unit", pa.string()),
    ("wavelengths", pa.list_(pa.float64())), ("values", pa.list_(pa.float32())),
    ("wavelength_min_nm", pa.float64()), ("wavelength_max_nm", pa.float64()),
    ("license", pa.string()), ("dataset_id", pa.string()),
])
table = pa.Table.from_pylist(rows, schema=schema).sort_by("id")
table = table.replace_schema_metadata({"openspeclib": json.dumps(footer)})

pq.write_table(
    table, "my_lab_library.parquet",
    row_group_size=250,              # R2
    compression="zstd",              # R3
    use_dictionary=[c for c in table.column_names if c not in ("wavelengths", "values")],
    # BYTE_STREAM_SPLIT must name the list's leaf column (R3).
    column_encoding={"wavelengths.list.element": "BYTE_STREAM_SPLIT",
                     "values.list.element": "BYTE_STREAM_SPLIT"},
)`;
