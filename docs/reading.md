# Reading OpenSpecLib files

An OpenSpecLib collection file is an ordinary Parquet file. You can read it with the `openspeclib` package, with any Parquet library, or with SQL. This page shows each way. The layout of the files is in the [specification](spec/format-v1.md); [data-structure.md](data-structure.md) gives a short overview.

The examples assume the release files are in the working directory: `usgs_splib07.parquet`, `ecostress.parquet`, `ecosis.parquet` and `openspeclib-index.parquet`. A release's files are also at `https://github.com/null-jones/openspeclib/releases/download/v<version>/<file>`.

## Python: the `openspeclib` reader

```bash
pip install "openspeclib[remote] @ git+https://github.com/null-jones/openspeclib.git"
```

`openspeclib.open(path_or_url)` opens one collection file. `openspeclib.open_index(path_or_url)` opens the index, which covers every collection in a release. Both have `search` and `get`.

<!-- readme-test: python-reader -->
```python
import openspeclib

lib = openspeclib.open("usgs_splib07.parquet")
print(lib.metadata["collection"]["title"], lib.metadata["format_version"])

# search() returns a pyarrow table of the non-array columns.
hits = lib.search(material="olivine", category="mineral", wavelength_range_nm=(400, 2500))
spectra = lib.get_many(hits["id"].to_pylist()[:3])
for s in spectra:
    print(s.id, s.wavelength_unit, s.values.dtype, s.metadata["geometry"])

# A whole collection without the arrays, as pandas (needs pandas):
# df = lib.to_pandas()
```

`search` takes these criteria, combined with AND:

| Argument | Matches |
|---|---|
| `material` | Case-insensitive substring of `material_name` or `name` |
| `category`, `quantity`, `geometry`, `dataset_id`, `collection` | A value or a list of values of that column |
| `wavelength_range_nm=(lo, hi)` | Spectra whose axis covers the whole range; either end may be `None` |

`get(id)` returns a `Spectrum`:

| Attribute | Content |
|---|---|
| `id` | The spectrum id |
| `wavelengths` | numpy float64, in `wavelength_unit` |
| `values` | numpy float32; NaN marks a missing band |
| `wavelength_unit` | `nm`, `um` or `cm-1` |
| `wavelengths_nm()` | The axis in nanometres (a `cm-1` axis comes back in reverse wavelength order) |
| `metadata` | Every other column, with `extra` parsed into a dict |
| `bandpass_fwhm`, `values_stddev` | numpy arrays, when the file has them |

A local `get` reads only the row group whose `id` statistics cover the id. Files are sorted by `id` in row groups of 100 spectra, so this takes milliseconds.

With the index, `index.get(id)` opens the right collection file for you, and `index.collections` gives each collection's footer, keyed by collection id:

<!-- readme-test: python-index -->
```python
import openspeclib

index = openspeclib.open_index("openspeclib-index.parquet")
table = index.search(category=["vegetation", "npv"], quantity="reflectance")
print(table.num_rows, "vegetation and NPV reflectance spectra")
print(table.group_by("collection").aggregate([("id", "count")]).to_pylist())

for collection_id, footer in index.collections.items():
    print(collection_id, footer["collection"]["license"], len(footer["datasets"]), "datasets")
```

### Remote files

Both functions accept an `http(s)://` URL. The reader then uses DuckDB (the `remote` extra), which fetches the footer and only the row groups a query needs, with HTTP Range requests:

```python
import openspeclib

base = "https://github.com/null-jones/openspeclib/releases/download/v0.2.0"
index = openspeclib.open_index(f"{base}/openspeclib-index.parquet")
spectrum = index.get(index.search(material="kaolinite")["id"][0].as_py())
```

The index is downloaded once (it is under 1 MB). Each `get` then fetches one row group of one collection file.

## Python: pyarrow

The footer is a JSON string under the key `openspeclib`:

<!-- readme-test: python-pyarrow-footer -->
```python
import json

import pyarrow.compute as pc
import pyarrow.parquet as pq

pf = pq.ParquetFile("usgs_splib07.parquet")
footer = json.loads(pf.metadata.metadata[b"openspeclib"])
print(footer["collection"]["citation"])
print(footer["extensions"])  # optional column groups present in this file

# Read one spectrum by id. The filter uses the id statistics to skip row groups.
spectrum_id = pq.read_table("usgs_splib07.parquet", columns=["id"])["id"][0].as_py()
row = pq.read_table(
    "usgs_splib07.parquet", filters=pc.field("id") == spectrum_id
).to_pylist()[0]
print(row["id"], row["wavelength_unit"], row["wavelengths"][:3], row["values"][:3])
```

Columns are flat: `material_name`, `quantity`, `wavelengths`, `values`. The optional `extra` column is a JSON string; parse it with `json.loads`.

## SQL: DuckDB

Columns need no quoting except `values`, which is an SQL keyword.

<!-- readme-test: sql-duckdb-one -->
```sql
SELECT id, material_name, quantity, geometry, wavelength_unit, wavelengths, "values"
FROM 'usgs_splib07.parquet'
WHERE id = (SELECT min(id) FROM 'usgs_splib07.parquet');
```

The collection files have different optional columns, so read several at once with `union_by_name`:

<!-- readme-test: sql-duckdb-union -->
```sql
SELECT collection, quantity, geometry, count(*) AS spectra
FROM read_parquet(['usgs_splib07.parquet', 'ecosis.parquet'], union_by_name = true)
GROUP BY ALL
ORDER BY ALL;
```

To search everything without reading arrays, query the index. Its `file` column names the collection file that holds each spectrum:

<!-- readme-test: sql-duckdb-index -->
```sql
SELECT id, material_name, quantity, geometry, wavelength_min_nm, wavelength_max_nm, file
FROM 'openspeclib-index.parquet'
WHERE quantity = 'reflectance' AND wavelength_min_nm <= 400 AND wavelength_max_nm >= 2500
ORDER BY id
LIMIT 20;
```

Footer fields, with DuckDB's JSON functions (`parquet_kv_metadata` returns BLOBs, so `decode` them first):

<!-- readme-test: sql-duckdb-footer -->
```sql
SELECT decode(value)::JSON ->> '$.collection.title' AS title,
       decode(value)::JSON ->> '$.collection.license' AS license,
       decode(value)::JSON ->> '$.collection.citation' AS citation
FROM parquet_kv_metadata('usgs_splib07.parquet')
WHERE key = 'openspeclib';
```

### Over HTTP

```sql
INSTALL httpfs; LOAD httpfs;
SELECT id, wavelength_unit, wavelengths, "values"
FROM 'https://github.com/null-jones/openspeclib/releases/download/v0.2.0/usgs_splib07.parquet'
WHERE id = 'usgs_splib07:splib07a_Kaolinite_CM9_BECKb_AREF';
```

**Ask for one id per query.** DuckDB uses the `id` statistics to skip row groups when the `WHERE` clause is a single `id = '…'`, so one query downloads one row group (well under 1 MB). An `id IN ('…', '…')` list does not skip row groups (as of DuckDB 1.5) and reads the whole file. For several spectra, run several queries, or combine them with `UNION ALL`:

```sql
SELECT id, "values" FROM 'https://…/usgs_splib07.parquet' WHERE id = 'usgs_splib07:a'
UNION ALL
SELECT id, "values" FROM 'https://…/usgs_splib07.parquet' WHERE id = 'usgs_splib07:b';
```

The `openspeclib` reader does this for you.

## R: arrow

```r
library(arrow)
library(dplyr)
library(jsonlite)

lib <- open_dataset("usgs_splib07.parquet")
minerals <- lib |>
  filter(material_category == "mineral", quantity == "reflectance") |>
  select(id, material_name, wavelength_unit, wavelengths, values) |>
  collect()
minerals$values[[1]]   # numeric vector; NA_real_ is not used, missing bands are NaN

footer <- fromJSON(ParquetFileReader$create("usgs_splib07.parquet")$GetSchema()$metadata$openspeclib)
footer$collection$citation
```

## JavaScript: DuckDB-WASM

The [viewer](https://null-jones.github.io/openspeclib/) reads the release this way. In a browser:

```js
import * as duckdb from '@duckdb/duckdb-wasm';

const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
const workerUrl = URL.createObjectURL(
  new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' }),
);
const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), new Worker(workerUrl));
await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
const conn = await db.connect();

const url = 'https://null-jones.github.io/openspeclib/data/usgs_splib07.parquet';
const id = 'usgs_splib07:splib07a_Kaolinite_CM9_BECKb_AREF';
const result = await conn.query(
  `SELECT wavelength_unit, wavelengths, "values" FROM '${url}' WHERE id = '${id}'`,
);
const row = result.get(0);
const wavelengths = row.wavelengths.toArray();   // Float64Array
const values = row.values.toArray();             // Float32Array; NaN = missing
```

The file must be served with CORS headers that allow your page's origin. GitHub release downloads do not send them, so the viewer serves a copy of the release from GitHub Pages.

## Units and missing values

- `wavelengths` is in `wavelength_unit`. Use `wavelength_min_nm` and `wavelength_max_nm` to filter across collections. To convert an axis: µm × 1000 = nm, and nm = 10⁷ / cm⁻¹.
- `values` is a fraction (0 to 1 nominal) for reflectance, transmittance and emissivity, and decadic absorbance for absorbance.
- NaN is the only missing-value marker. Most numeric libraries propagate NaN, so use NaN-aware functions (`numpy.nanmean`) or drop NaN bands before fitting.
