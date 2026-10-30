"""Write tests/quality/expected.json: the quality-profile (Q1-Q4) results of
the Python validator (`openspeclib validate --profile openspeclib`, with the
shipped allowlist) for the files quality.test.ts checks with the viewer.

Run from the repository root after changing the validator, the bounds in
schemas/format-v1/rules.json, or the allowlist:

    python viewer/tests/quality/generate_expected.py
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from openspeclib import validate as v  # noqa: E402

# Corpus fixtures, plus the release collection that has an allowlisted spectrum.
FILES = sorted((ROOT / "tests/conformance/fixtures").glob("*.parquet")) + [
    ROOT / "viewer/public/data/usgs_splib07.parquet",
]


def rules(issues, severity=None):
    """Quality rule ids of some issues."""
    found = set()
    for issue in issues:
        if severity is not None and issue.severity != severity:
            continue
        rule = issue.rule or v.QUALITY_RULES.get(issue.check)
        if rule and rule.startswith("Q"):
            found.add(rule)
    return sorted(found)


def main():
    out = {}
    for path in FILES:
        if not path.exists():
            continue
        result = v.validate(path, profile="openspeclib")
        entry = {
            "failed": rules(result.issues, "error"),
            "warned": rules(result.issues, "warning"),
            "waived": rules(result.waived),
            "skipped": any(i.check == "quality_skipped" for i in result.issues),
        }
        if "public/data" in str(path):
            # Release data is redeployed: the test skips it when the file changed.
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        out[str(path.relative_to(ROOT))] = entry
    target = Path(__file__).with_name("expected.json")
    target.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(out)} entries to {target}")


if __name__ == "__main__":
    main()
