# Collection id registry

Every OpenSpecLib Format file names its collection with a collection id (footer `collection.id`), and every spectrum id starts with it (`{collection}:{local_id}`). Ids must not collide, or two files would claim the same spectrum ids. This page lists the ids that are known, so that new publishers can pick one that is free.

The registry is informative: a file does not have to be listed here to conform to the [specification](format-v1.md). Listing it is how you avoid a clash.

## Rules for claiming an id

1. **Grammar.** An id matches `^[a-z0-9][a-z0-9_]{1,62}$` (the footer schema's `collection.id` pattern, checked by rule F1): 2 to 63 characters, lowercase letters, digits and underscores, starting with a letter or digit. No hyphens, dots or capitals.
2. **Make it specific.** Name the library, not the topic: `usgs_splib07` rather than `minerals`. Include a version or edition when the source has several that may be published separately.
3. **Claim it with a pull request.** Add a row to the table below and open a pull request against `main`. You do not need to have written a loader or a file yet; a reserved id is fine.
4. **First come, first served.** The first merged pull request gets the id. Maintainers only refuse a claim that breaks the grammar, duplicates an existing entry for the same source, or uses a name that would mislead (for example, someone else's library name for unrelated data).
5. **Ids are permanent.** Once a file with an id has been published, the id is never reassigned, even if the collection is withdrawn. Mark it *retired* instead.

## Status values

| Status | Meaning |
|---|---|
| released | Files with this id are published. |
| reserved | Claimed; nothing published under it yet. |
| retired | No longer published; the id is not reused. |

## Registry

| Collection id | Title | Maintainer / publisher | Status | Notes |
|---|---|---|---|---|
| `usgs_splib07` | USGS Spectral Library Version 7 | OpenSpecLib | released | From release 0.2.0 in Format v1. Source: U.S. Geological Survey, [doi:10.5066/F7RR1WDJ](https://doi.org/10.5066/F7RR1WDJ). |
| `ecostress` | ECOSTRESS Spectral Library | OpenSpecLib | released | From release 0.2.0 in Format v1. Source: NASA JPL, <https://speclib.jpl.nasa.gov>. |
| `ecosis` | EcoSIS (OpenSpecLib collection) | OpenSpecLib | released | An aggregated collection: a curated selection of [EcoSIS](https://ecosis.org) packages, each a dataset with its own licence and citation. See [EcoSIS curation](../ecosis-curation.md). |
| `relab` | RELAB Spectral Database | OpenSpecLib | reserved | Loader exists; not yet released. |
| `asu_tes` | ASU Thermal Emission Spectral Library | OpenSpecLib | reserved | Loader exists; not yet released. |
| `bishop` | Bishop Spectral Library | OpenSpecLib | reserved | Loader exists; not yet released. |
| `ossl` | Open Soil Spectral Library | OpenSpecLib | reserved | Loader exists; not yet released. |

"OpenSpecLib" in the maintainer column means the file is produced by this project's toolkit from the upstream source. The upstream organisation owns the data; the id names the OpenSpecLib packaging of it. If an upstream publisher wants to publish its own Format v1 files under one of these ids, open an issue.
