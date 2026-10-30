# Contributing to OpenSpecLib

Thank you for helping. OpenSpecLib has three parts, and each takes contributions in a different way:

| You want to change | Where | Process |
|---|---|---|
| The Python toolkit, viewer or docs | `src/`, `viewer/`, `docs/` | A pull request (this page) |
| The data: add a source library | `src/openspeclib/loaders/` | A pull request, plus a collection id (see [Adding a source loader](#adding-a-source-loader)) |
| The OpenSpecLib Format specification | `docs/spec/`, `schemas/format-v1/` | An RFC (see [Changing the specification](#changing-the-specification)) |

Bug reports and questions go in [GitHub issues](https://github.com/null-jones/openspeclib/issues). For a data problem (a wrong value, label or licence), say which collection, which spectrum `id`, and which release.

By contributing you agree that your code is released under the project's [MIT licence](LICENSE). Data keeps the licence of its source; do not submit data you are not allowed to redistribute.

## Development setup

You need Python 3.11 or 3.12.

```bash
git clone https://github.com/null-jones/openspeclib.git
cd openspeclib
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The viewer is a separate Node project in `viewer/`; see [`viewer/README.md`](viewer/README.md).

## Checks

CI (`.github/workflows/ci.yml`) runs these on every push and pull request to `main`. All must pass before a pull request is merged, so run them locally first:

```bash
pytest tests/ -v                     # tests
black --check src/ tests/ scripts/   # formatting (run without --check to fix)
ruff check src/ tests/               # lint: pycodestyle, pyflakes, isort, docstrings, annotations
mypy src/openspeclib/                # strict type checking
```

If you change the Pydantic models or the column definitions in `openspeclib.storage`, check that they still match the normative schemas in `schemas/format-v1/` (the script exits non-zero on any drift and writes nothing):

```bash
python scripts/generate_schemas.py
```

## Code standards

[`CLAUDE.md`](CLAUDE.md) is the reference; in short:

- **Formatting:** Black, line length 100.
- **Docstrings:** Google style on every public and private function, with `Args:`, `Returns:`, `Yields:` and `Raises:` as needed. Tests and scripts are exempt.
- **Type hints:** every argument and return value. Use `X | None` outside Pydantic models; Pydantic models use `Optional[X]` with `Field()`.
- **Storage:** all collection-file I/O goes through `openspeclib.storage`. Do not read or write the Parquet files directly elsewhere.
- **Data conventions:** wavelengths stay in the source's native unit; missing values are NaN, never a sentinel; reflectance, transmittance and emissivity are fractions (0–1); source-specific metadata goes in `extra`, not new columns.
- **Tests:** new behaviour needs a test. Loader tests use small fixture files in `tests/fixtures/`.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep each pull request to one change. Describe what changed and why; link the issue or RFC if there is one.
3. Run the checks above.
4. If users will notice the change, add a line to the topmost section of [`CHANGELOG.md`](CHANGELOG.md): `## [Unreleased]`, or the upcoming version's `## [x.y.z] - Unreleased` before its release. Use the Keep a Changelog groups (`### Added`, `### Changed`, `### Removed`, `### Fixed`).
5. A maintainer reviews and merges. Changes to released data (values, labels, inclusion) need a short written justification in the pull request, because they change what people have already downloaded.

## Adding a source loader

A loader turns one source library into one OpenSpecLib collection. The step-by-step guide is [`docs/adding-sources.md`](docs/adding-sources.md). Before writing code:

1. **Check the licence.** The source's licence must allow redistribution. Record it as an SPDX identifier, or as a `LicenseRef-` identifier with its name and URL when there is no SPDX id.
2. **Claim a collection id.** Add a row to [`docs/spec/collections.md`](docs/spec/collections.md) in your pull request (or in a small pull request of its own, to reserve it early). Ids are first come, first served.
3. **Work out what was measured.** Every spectrum needs a `quantity` (reflectance, transmittance, emissivity or absorbance) and a `geometry`. Use `unknown` for geometry when the source does not document it; do not guess.

The loader must:

- yield records from a generator, so large sources do not need to fit in memory;
- read the source's scale from its own metadata where it exists (for example a units header) and convert percentages or scaled integers to fractions;
- turn the source's missing-value markers into NaN;
- keep wavelengths in the source's unit;
- keep the upstream identifier in `original_id` and build an `id` that matches the Format v1 grammar (rule F5);
- fail loudly on files it cannot interpret rather than skip them silently.

Include fixture files (a few spectra, small enough to commit) and tests that check parsed values, units, scale and labels. Then run `openspeclib validate` on a full ingest of the source and say in the pull request how many spectra it produced and whether any needed allowlist entries. Each allowlist entry needs a written reason.

A loader can be merged before its data is published. Its collection id is then listed as *reserved* in `docs/spec/collections.md` until a release includes it.

## Changing the specification

The OpenSpecLib Format specification ([`docs/spec/format-v1.md`](docs/spec/format-v1.md) and the files in `schemas/format-v1/`) is used by people outside this project, so it changes more slowly than the code.

- **Editorial fixes** (typos, clearer wording, broken links, examples) that do not change what is conformant: open a pull request directly.
- **Anything else** (a new column, extension or vocabulary value; a changed rule; a new file type): write an RFC first. The process is in [`docs/rfcs/README.md`](docs/rfcs/README.md); copy [`docs/rfcs/0000-template.md`](docs/rfcs/0000-template.md) to start.

If you are not sure which applies, open an issue and ask.

The specification is versioned independently of the Python package and of data releases, following semantic versioning (section 11 of the spec).
