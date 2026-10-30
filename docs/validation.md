# Validation

`openspeclib validate` checks OpenSpecLib Format v1 files. It has two profiles:

- **`format`** (the default) checks conformance to the [specification](spec/format-v1.md): the must-rules F1–F13 for collection files and I1 for index files, plus the recommendations R1–R5 as warnings. A file that passes is conformant. Use this for any file, whoever produced it.
- **`openspeclib`** checks conformance plus the OpenSpecLib quality profile (Q1–Q4, spec section 12), with the allowlist. The OpenSpecLib release build uses this profile.

The rules are defined in the specification and in [`schemas/format-v1/rules.json`](../schemas/format-v1/rules.json). The code is `src/openspeclib/conformance.py` (conformance) and `src/openspeclib/validate.py` (profiles, quality, allowlist, report).

## Running it

```bash
openspeclib validate my_library.parquet                    # one collection file
openspeclib validate openspeclib-index.parquet             # an index and every file it lists
openspeclib validate release/ --profile openspeclib        # a directory
```

`PATH` can be a collection file, an index file, or a directory. A directory is searched recursively for `*.parquet`. Index files are checked first, together with the collection files they list; other collection files are then checked on their own. When a directory holds an index, a collection file that no index lists gets an `unindexed_file` warning.

| Option | Effect |
|---|---|
| `--profile format\|openspeclib` | The profile (default `format`) |
| `--allowlist FILE` | Load another allowlist file. Repeatable. Adds to the shipped allowlist; on equal specificity its entries win. Used by the `openspeclib` profile only. |
| `--no-default-allowlist` | Do not load the shipped allowlist |
| `--details` | Print every issue instead of the summary grouped by check and collection |
| `--max-examples N` | Example spectra per group in the summary (default 5) |
| `--report FILE` | Write a JSON report with every issue |

| Exit code | Meaning |
|---|---|
| 0 | No errors (warnings are allowed) |
| 1 | At least one error |
| 2 | Usage error (for example a missing `PATH` or an unknown option), or an allowlist file that is unreadable or malformed |

The writer in `openspeclib.storage` runs the same conformance checker on every file it writes and refuses to keep a file that fails a must-rule. `openspeclib convert` checks its output too.

## Conformance rules

Errors. Conformance rules can never be waived: a file follows the format or it does not. Each issue is reported under its rule id.

| Rule | Checks |
|---|---|
| F1 | Valid Parquet (a column name that is not UTF-8 makes the schema unreadable, so it fails here); footer key `openspeclib` holds JSON valid against the footer schema, with format major version 1, a semantic `format_version` (no leading zeros: `1.01.0` fails) and a real `created` date; no `openspeclib_index` key |
| F2 | Every core column, with its type; no nulls in non-nullable columns; no empty or whitespace-only strings in `id`, `collection`, `original_id`, `name`, `material_name`, `license`, `dataset_id`; no null elements in the `list<string>` columns (`material_keywords`, `measurement_processing`); text in core columns is valid UTF-8 |
| F3 | No unknown columns (only core, declared extension, `extra`, and flat `x_` columns); text in `x_` columns is valid UTF-8 |
| F4 | Extension columns and `extra` have their types and hold valid UTF-8; each non-null `extra` value is a JSON object (not an array, string, number, `true`/`false` or `null`; `NaN` and `Infinity` are not JSON) |
| F5 | `id` unique and matching `^{collection}:[A-Za-z0-9._~-]{1,200}$` |
| F6 | `collection` equals the footer's `collection.id` |
| F7 | `material_category`, `quantity`, `geometry`, `wavelength_unit` from their vocabularies |
| F8 | `wavelengths`: at least 2 values, finite, positive, strictly monotonic |
| F9 | `values`: same length as `wavelengths`; finite or NaN; no value with magnitude above 1e10 (a sentinel) |
| F10 | `wavelength_min_nm`, `wavelength_max_nm` match the axis |
| F11 | Licences are SPDX expressions, defined `LicenseRef-` ids, or `NOASSERTION` on its own. Operators, `NOASSERTION` and the `LicenseRef-` prefix are case-sensitive (`noassertion` fails) |
| F12 | Every `dataset_id` is in the footer `datasets`, every dataset is used, and `spectra_included` matches |
| F13 | `bandpass_fwhm`, `values_stddev`: same length as `wavelengths`, non-negative or NaN |
| I1 | An index file meets spec section 7: its columns, its footer, and exact agreement with every collection file it lists |

### Newer minor versions

A file whose `format_version` names a newer 1.x minor version than the checker implements (1.0.0) may use things added since (spec section 11). The checker does not report them as errors but as warnings, under the rule that would otherwise fail: unknown footer members and values such as a new extension (F1), unknown columns (F3) and unknown vocabulary values (F7). Everything else is still checked, so a type error, a missing required field or a non-flat column (unknown columns must be flat too) still fails. A rule that holds but carries such warnings has the status `warned`: it counts as passed, so the file is conformant, and `validate` reports each warning under the rule id.

For an index file (I1), the index's own `format_version` governs its footer members and its columns. Each `collections[].metadata` object is judged by its own `format_version`, as the collection file itself is, so a 1.0.0 index can list a 1.1.0 collection file. An index column unknown to the checker is a warning when the index declares a newer minor version, or when a listed collection file that declares one has that column (its values are then compared like any shared column); otherwise it is an error.

A `format_version` with leading zeros (`1.01.0`) is not a semantic version: it fails F1 (or I1) and is not treated as a newer version.

### Recommendations

Warnings:

| Rule | Recommends |
|---|---|
| R1 | Rows sorted by `id`, with `id` statistics |
| R2 | Row groups of at most 250 spectra |
| R3 | BYTE_STREAM_SPLIT and zstd on the array columns |
| R4 | At least one non-NaN value per spectrum |
| R5 | At most half the spectra with `geometry` = `unknown` |

## Quality profile

The `openspeclib` profile adds these checks. A *valid value* is finite and not a sentinel; NaN bands are ignored. Bounds depend on `quantity` and come from `qa_bounds` in `rules.json`:

| Quantity | Hard bounds (Q1, Q2) | Warning bounds (Q3) |
|---|---|---|
| reflectance | −0.2 to 1.5 | −0.05 to 1.2 |
| transmittance | −0.2 to 1.5 | −0.05 to 1.2 |
| emissivity | −0.2 to 1.5 | −0.05 to 1.05 |
| absorbance | −1.0 to 5.0 | −0.1 to 3.0 |

Bounds are compared as float32, and a value exactly at a bound passes. The median in Q1 is exact (the mean of the two middle values for an even count), so the Python validator and the viewer's checker agree on spectra near a bound.

| Check | Rule | Severity | Meaning | Waivable |
|---|---|---|---|---|
| `value_scale` | Q1 | error | The spectrum's median is outside the hard bounds. High: a missed scale conversion. Low: corrupt or non-spectral data. | By exact `id` only |
| `value_out_of_bounds` | Q2 | error | The median is in range but some values are outside the hard bounds (spikes, clipped or corrupt bands) | Yes |
| `value_range_suspect` | Q3 | warning | Values inside the hard bounds but outside the warning bounds | Yes |
| `wavelength_extent` | Q4 | error | `wavelength_min_nm` below 100 or `wavelength_max_nm` above 1,000,000 (100 nm to 1 mm). Almost always a mislabelled `wavelength_unit`. | By exact `id` only |
| `value_bounds_share` | Q2 (as reported) | warning | A collection or dataset with at least 10 spectra of one quantity, of which at least 10% fail Q1, Q2 or Q4. Points to a set-wide scale or unit problem. | No |

Each spectrum is reported under one of Q1, Q2 and Q3 at most: one that fails Q1 is not also reported under Q2, and one that fails Q2 is not also reported under Q3.

Other checks you may see: `quality_skipped` (warning: a file's columns could not be read for the quality checks), `unindexed_file` (warning, above), `no_files` (error: nothing to validate), `unreadable_path` (error: a path that cannot be read or checked: a directory named `*.parquet`, a broken symbolic link, a directory that cannot be listed, so files in it may go unchecked, or a file the checker failed on; the rest of the directory is still checked), `stale_allowlist_entry` (warning: an allowlist entry matched nothing in the collections checked).

## Allowlist

Some published spectra are genuinely unusual, for example a real reflectance above 1.5. An allowlist entry exempts them from a quality check, with a written reason. Conformance rules cannot be allowlisted.

The shipped allowlist is [`src/openspeclib/validation_allowlist.json`](../src/openspeclib/validation_allowlist.json). It is loaded by default; `--allowlist FILE` adds more files, and `--no-default-allowlist` leaves it out.

```json
{
  "format_version": 1,
  "description": "Optional free text.",
  "entries": [
    {
      "id": "usgs_splib07:splib07a_LawnGrass_GDS91b_-shiftd_3nm_BECKa_AREF",
      "checks": ["value_out_of_bounds"],
      "bounds": {"max": 2.1},
      "reason": "Why this spectrum is genuinely unusual, with the evidence."
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | one selector | An exact spectrum id |
| `id_prefix` | one selector | A prefix of spectrum ids. It must have at least one character after `{collection}:`, so an entry can never cover a whole collection. |
| `dataset` | one selector | A dataset, as `{collection}:{dataset_id}` |
| `quantity` | no | Apply only to spectra of this quantity |
| `checks` | yes | Checks to waive: `value_out_of_bounds` and `value_range_suspect` with any selector; `value_scale` (Q1) and `wavelength_extent` (Q4) with an exact `id` only |
| `bounds` | no | `{"min": …, "max": …}`, either or both, with `value_out_of_bounds` only. The spectrum is checked against these bounds instead of being exempted, so a later scale error still fails. |
| `reason` | yes | A non-empty explanation |

Each selector must start with a collection id and a colon. Unknown fields, unknown or non-waivable checks, and malformed entries make the file invalid, and `validate` exits 2.

When several entries match a spectrum and check, the most specific wins: exact `id`, then the longest `id_prefix`, then `dataset`. On a tie, the entry loaded last wins. An entry for `value_out_of_bounds` does not waive `value_range_suspect`; list both checks if both apply.

Add an entry only after checking the spectrum against its source, and fix the loader instead when the cause is a loader bug. Stale entries are reported, so the list shrinks as loaders improve.

## Report

`--report FILE` writes JSON with:

| Key | Content |
|---|---|
| `valid`, `profile` | The verdict and the profile used |
| `error_count`, `warning_count` | Totals |
| `files_checked`, `spectra_checked`, `elapsed_seconds` | What was checked |
| `allowlist` | Files loaded and entry count |
| `groups` | Issues grouped by severity, check and collection, with counts and examples |
| `issues` | Every issue: `severity`, `check`, `rule`, `file`, `source` (collection id), `spectrum_id`, `detail`, `message` |
| `waived` | Issues the allowlist waived, with the reason in the message |
| `conformance` | The full conformance report of each file checked, rule by rule |

Each OpenSpecLib release includes the report of its build as `validation-report.json`.
