# Licensing and Citations

## Important Notice

**Licensing terms differ between source spectral libraries included in OpenSpecLib.** Each source library has its own license governing how its data may be used, redistributed, or cited. Users must review and comply with the specific license for each source library from which they use data.

OpenSpecLib code is released under the [MIT License](../LICENSE). However, the spectral data aggregated by OpenSpecLib retains the original licensing terms of each source library.

---

## `licenses.json`

Every OpenSpecLib release includes a `licenses.json` file alongside `catalog.json`. This file provides a machine-readable index of licensing and citation information for each source library, keyed by the same source identifiers used in the catalog and Parquet files.

### Structure

```json
{
  "openspeclib_version": "0.1.0",
  "generated_at": "2026-04-16T00:00:00Z",
  "notice": "Licensing terms differ between source spectral libraries...",
  "sources": {
    "usgs_splib07": {
      "name": "usgs_splib07",
      "version": "7a",
      "url": "https://doi.org/10.5066/F7RR1WDJ",
      "license": "Public Domain",
      "license_url": null,
      "citation": "Kokaly, R.F., Clark, R.N., ...",
      "citation_doi": "10.3133/ds1035"
    }
  }
}
```

### Keys

The keys in `sources` match the `source.library` column in each Parquet file and the `source.library` field in each catalog record. This allows consumers to look up the license for any spectrum:

1. Read the `source.library` value from a spectrum (e.g. `"usgs_splib07"`)
2. Use it as a key into `licenses.json` `sources`
3. Display or check the `license` and `citation` fields

### Fields

| Field | Description |
|---|---|
| `name` | Source library identifier |
| `version` | Version of the source library |
| `url` | DOI or URL for the source library |
| `license` | License terms summary |
| `license_url` | URL to full license text (if available) |
| `citation` | Recommended citation string |
| `citation_doi` | DOI for the citation (if available) |

---

## Per-Source Licensing Summary

| Source | License | Citation DOI |
|---|---|---|
| USGS Spectral Library Version 7 | Public Domain | [10.3133/ds1035](https://doi.org/10.3133/ds1035) |
| ECOSTRESS Spectral Library | Public Domain | [10.1016/j.rse.2019.05.015](https://doi.org/10.1016/j.rse.2019.05.015) |
| RELAB Spectral Database | Public Domain | -- |
| ASU Thermal Emission Spectral Library | Public Domain | [10.1029/1999JE001138](https://doi.org/10.1029/1999JE001138) |
| Bishop Spectral Library | Public Domain (non-commercial use with citation) | [10.1180/claymin.2008.043.1.03](https://doi.org/10.1180/claymin.2008.043.1.03) |

Note that while most sources are designated "Public Domain", the Bishop Spectral Library restricts use to non-commercial purposes and requires citation. Always check the specific terms for your use case.

---

## Attribution Requirements

When publishing results derived from OpenSpecLib data:

1. **Cite the OpenSpecLib project.**
2. **Cite the specific source library or libraries** from which the data originated (identifiable via the `source.library` field in each spectrum record).

The `citation` field in `licenses.json` (and in each spectrum's `source.citation`) provides the appropriate citation string. If a `citation_doi` is available, use it to construct a persistent link.

---

## Further Reading

- [Data Provenance](provenance.md) -- Full provenance details, recommended citations (BibTeX-ready), and descriptions for each source library
- [JSON Schema](../schemas/licenses.schema.json) -- Machine-readable schema for `licenses.json`
