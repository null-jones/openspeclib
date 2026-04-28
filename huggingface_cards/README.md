# OpenSpecLib HuggingFace Dataset Cards

This directory contains [HuggingFace dataset cards](https://huggingface.co/docs/hub/datasets-cards)
for the source spectral libraries that OpenSpecLib re-publishes on the
HuggingFace Hub. Each card is a self-contained `README.md` with the YAML
frontmatter that the Hub expects, and is the canonical place to document
provenance, licensing, and structure for the corresponding HF dataset.

## Layout

```
huggingface_cards/
├── README.md              ← this file
├── ecostress/
│   └── README.md          ← card for the OpenSpecLib re-upload of ECOSTRESS
└── usgs_splib07/
    └── README.md          ← card for the OpenSpecLib re-upload of USGS Speclib07
```

When publishing or updating an HF dataset, copy the matching card from this
directory into the dataset's repository root as `README.md`. Keep the
in-repo copy as the source of truth — version it alongside the loader
that produces the dataset so the two can never drift.

## What's covered (and what's not)

The cards in this directory cover **only** the subset of OpenSpecLib
sources that we re-publish to HuggingFace as standalone datasets. They
are *re-uploads* of public-domain or open-licence sources, distributed
in OpenSpecLib's unified Parquet schema for easier downstream use.
Provenance (DOI, citation, original URL) and license terms of each
underlying source are preserved verbatim — the HF dataset is a
re-distribution, not a relicensing.

For the combined master library (with all sources, the catalog, and the
shared wavelength registry), use the OpenSpecLib GitHub releases
directly: <https://github.com/null-jones/openspeclib/releases>.
