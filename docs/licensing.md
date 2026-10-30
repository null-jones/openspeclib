# Licensing

> **The spectra keep their source's licence.** Licences differ between collections and, for EcoSIS, between datasets. Check the licence of every spectrum you use.

The OpenSpecLib code, specification and documentation are released under the [MIT License](../LICENSE). That licence does not cover the data.

## Where licences are recorded

In OpenSpecLib Format v1, licences are machine-readable in three places in every collection file:

| Where | What |
|---|---|
| `license` column | The licence of each spectrum. Authoritative. |
| Footer `collection.license` | The licence of the collection as a whole, or `NOASSERTION` when rows differ |
| Footer `datasets.<id>.license` | The licence of each contributing dataset (aggregated collections) |

Each value is one of (rule F11):

- an **SPDX licence expression**, such as `CC-BY-4.0` or `ODbL-1.0`;
- a **`LicenseRef-…` identifier**, for terms that have no SPDX id or a licence named without a version. Every `LicenseRef-` used in a file is defined in the footer's `collection.license_refs`, with a `name`, and where known a `url` and the terms as `text`;
- **`NOASSERTION`**, when the source does not say. The footer's `license_note` then explains.

There is no separate licence file. Releases up to v0.0.7 had none either (their licences were in the catalog JSON; `licenses.json` was only written by `openspeclib combine`). The footers and the index, whose footer copies every collection footer, hold this information now.

## Looking up a spectrum's licence

```python
import openspeclib

lib = openspeclib.open("ecosis.parquet")
spectrum = lib.get(lib.search(category="water")["id"][0].as_py())
license_id = spectrum.metadata["license"]
definition = lib.metadata["collection"].get("license_refs", {}).get(license_id)
dataset = lib.metadata["datasets"].get(spectrum.metadata["dataset_id"] or "", {})
print(license_id, definition, dataset.get("citation"))
```

The README quickstart does the same through the index. With SQL, `license` is an ordinary column, and the footer is in `parquet_kv_metadata` (see [reading.md](reading.md)).

## Per-collection licences

| Collection | Licence id | Meaning |
|---|---|---|
| `usgs_splib07` | `LicenseRef-USGS-PublicDomain` | U.S. Government work, public domain. The USGS asks users who modify the data to describe their changes, and appreciates acknowledgement. |
| `ecostress` | `LicenseRef-ECOSTRESS-Terms` | No standard licence. JPL is operated by Caltech for NASA, so its works are not automatically U.S. Government public domain; the provider's terms at <https://speclib.jpl.nasa.gov> apply. Cite Meerdink et al. (2019). |
| `ecosis` | `NOASSERTION` at collection level | Varies by dataset; see below |
| `relab` (not released) | `LicenseRef-RELAB-ResearchUse` | Free for research use with citation |
| `asu_tes` (not released) | `LicenseRef-ASU-TES-ResearchUse` | Free for research use with citation |
| `bishop` (not released) | `LicenseRef-Bishop-NonCommercial` | Non-commercial use only, with citation |
| `ossl` (not released) | `CC-BY-4.0` | Creative Commons Attribution 4.0 |

## EcoSIS: per-dataset licences

Each EcoSIS package states its own licence. OpenSpecLib maps the EcoSIS label to an identifier:

| EcoSIS label | Identifier |
|---|---|
| Open Data Commons Open Database License (ODbL) | `ODbL-1.0` |
| Open Data Commons Attribution License | `ODC-By-1.0` |
| Creative Commons Attribution | `LicenseRef-EcoSIS-CC-BY` (no version stated) |
| Creative Commons Attribution Share-Alike | `LicenseRef-EcoSIS-CC-BY-SA` (no version stated) |
| Creative Commons Non-Commercial (Any) | `LicenseRef-EcoSIS-CC-NC` |
| Other (Open) | `LicenseRef-EcoSIS-Other-Open` (an open licence the package does not name; check the package page) |
| anything else or none | `NOASSERTION`, with the original label in the dataset's `license_note` |

The Creative Commons labels become `LicenseRef-` identifiers rather than, for example, `CC-BY-4.0`, because EcoSIS does not say which version applies, and the specification asks producers not to guess (spec section 3).

The `ecosis` collection's footer is an **umbrella entry**: its `license` is `NOASSERTION` with a note that the per-row `license` and the dataset licences are authoritative, and its `citation` points to the per-dataset citations. Never apply the umbrella to a spectrum; use the row's `license` and its dataset's `citation`.

Some EcoSIS datasets are share-alike (`LicenseRef-EcoSIS-CC-BY-SA`) or non-commercial (`LicenseRef-EcoSIS-CC-NC`). Filter on `license` if your use needs it:

```sql
SELECT license, count(*) AS spectra FROM 'ecosis.parquet' GROUP BY license ORDER BY spectra DESC;
```

## Exported data

`openspeclib export` keeps the licence with the data: CSV rows carry `license` and an `attribution` column; ENVI headers carry the licence; and every export writes a `NAME.README.md` listing the licences and citations of the exported spectra. Keep it with the data when you share it. See [converting.md](converting.md).

## Attribution

Cite the source of each spectrum, and OpenSpecLib if you used its format or tools. See [provenance.md](provenance.md#what-to-cite).
