# RFC NNNN: Title

- **Status:** Draft <!-- Draft | Accepted | Rejected | Withdrawn | Implemented -->
- **Author(s):** Name (@github-handle)
- **Created:** YYYY-MM-DD
- **Discussion:** link to the pull request
- **Spec version:** the version this targets, e.g. 1.1.0 (minor) or 2.0.0 (major)
- **Amends / supersedes:** RFC numbers, or "none"

## Summary

One paragraph: what changes, for whom.

## Motivation

What problem does this solve? Who has it? Give concrete examples: a source library that cannot be represented, a query that is slow or impossible, a value that is ambiguous. Numbers help.

## Proposal

The change itself, precise enough that someone else could implement it. Include:

- the new or changed text for `docs/spec/format-v1.md`;
- the changes to `schemas/format-v1/` (columns, vocabularies, footer schema, rules);
- new or changed conformance rules, with their identifiers;
- an example file, footer or query if it helps.

## Compatibility

- Do files that conform today still conform?
- Can a reader that only knows the current version read files written under this RFC?
- Which version bump does this need (patch, minor, major), and why?
- What must producers and readers outside OpenSpecLib do?

## Alternatives considered

What else was considered, including doing nothing, an `x_` column, or a key in `extra`, and why this proposal is better.

## Implementation

What changes in the `openspeclib` package, the conformance checker, converters, the viewer and the tests. Note anything that needs a data re-release.

## Unresolved questions

Open points to settle during discussion.

## Decision

Filled in by a maintainer: date, outcome, and a short rationale.
