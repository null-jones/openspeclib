# OpenSpecLib RFCs

An RFC (request for comments) is a short design document that proposes a change to the [OpenSpecLib Format specification](../spec/format-v1.md). RFCs give people who publish or read files in the format a chance to comment before the change is made, and leave a record of why the format is the way it is.

## When an RFC is needed

The specification means [`docs/spec/format-v1.md`](../spec/format-v1.md) and the machine-readable files it names in `schemas/format-v1/` (`columns.json`, `vocabularies.json`, `footer.schema.json`, `rules.json`).

| Change | RFC? |
|---|---|
| Typos, clearer wording, extra examples, broken links, with no effect on what is conformant | No. Open a pull request. |
| Fixing a disagreement between the spec text and the schema files (the spec's introduction says the schema files win) | No, unless the fix changes which files conform. Then yes. |
| A new vocabulary value, extension, optional column, recommendation (R-rule) or file type | Yes |
| Any change to a conformance rule (F- or I-rule), a core column, a type, or the footer schema | Yes |
| Changes to the OpenSpecLib quality profile (section 12) | Yes |
| Claiming a collection id | No. Follow [`docs/spec/collections.md`](../spec/collections.md). |
| Changes to the Python package, the viewer, or which data OpenSpecLib publishes | No. These are ordinary pull requests (see [CONTRIBUTING.md](../../CONTRIBUTING.md)). |

## Lifecycle

```
draft → discussion → accepted → implemented
                   ↘ rejected
                   ↘ withdrawn
```

1. **Draft.** Copy [`0000-template.md`](0000-template.md) to `docs/rfcs/NNNN-short-title.md` and fill it in. Open a pull request containing only the RFC. Set `Status: Draft`.
2. **Discussion.** The pull request is the discussion. Anyone may comment. The author revises the RFC in the same pull request; the history stays in the pull request. The discussion stays open for at least 14 days, so people outside the project have time to see it. The author or a maintainer may also open a GitHub issue or discussion to point people at it.
3. **Decision.** A maintainer records the decision in the RFC (`Status: Accepted` or `Status: Rejected`, with a dated one-paragraph rationale under "Decision") and merges the pull request. Rejected RFCs are merged too, so the reasoning is kept. An author can set `Status: Withdrawn` at any point.
4. **Implemented.** The specification change lands in a separate pull request that links the RFC, updates the spec text, the schema files, the conformance checker and the tests together, and bumps the specification version. When it is released, set `Status: Implemented` and fill in the spec version.

An accepted RFC is not edited afterwards except to update its status. A later change to the same feature needs a new RFC that says which RFC it amends or supersedes.

## Numbering

RFCs are numbered with four digits, in order. Take the next unused number when you open the pull request; if two pull requests collide, the one merged second renumbers. `0000` is the template. File names are `NNNN-short-title.md` with a lowercase, hyphenated title.

## Who decides

The project maintainers decide, aiming for consensus among those who commented. Today the maintainer is the project lead, Ellie Jones (GitHub: null-jones); this section will list more maintainers as they join. A maintainer who wrote an RFC should not be its only approver once there is more than one maintainer.

When deciding, maintainers weigh: whether existing files stay conformant; whether readers that only know an older minor version can still read new files; cost for producers outside OpenSpecLib; and whether the change could be an `x_` column or an `extra` key instead.

## Specification versions

The specification follows [semantic versioning](https://semver.org), as section 11 of the spec says. The version is independent of the `openspeclib` Python package and of data releases.

| Bump | When | Examples |
|---|---|---|
| Patch (1.0.x) | Editorial changes that do not change which files conform. No RFC. | Wording, examples, fixing a schema file to match unambiguous spec text |
| Minor (1.x.0) | Additions that leave every conformant 1.x file conformant and let a 1.0 reader read new files by ignoring what it does not know. | New vocabulary values, extensions, optional columns, recommendations |
| Major (x.0.0) | Anything that removes or redefines something, or makes a conformant file non-conformant. | Removing or renaming a column, changing a type, tightening an F-rule |

Readers must reject a file whose major `format_version` they do not support, so a major version is expensive for everyone. An RFC that needs one should say why a minor change cannot do the job.

## Index

| RFC | Title | Status | Spec version |
|---|---|---|---|
| [0001](0001-format-v1.md) | OpenSpecLib Format v1 | Implemented | 1.0.0 |
