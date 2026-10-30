"""Execute the marked code blocks of README.md and docs/reading.md against a tiny release.

Only fenced blocks preceded by an explicit marker comment are run:

    <!-- readme-test: python -->
    ```python
    ...
    ```

Tags starting with ``python`` are executed as Python; tags starting with
``sql`` are run with DuckDB (skipped when DuckDB is not installed). The
release is built with ``openspeclib combine``'s API (``build_library``),
which writes the same flat layout as a GitHub release, plus the
``validation-report.json`` the release workflow adds. Every block runs with
the release directory as the working directory.
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import make_collection_info, make_record

from openspeclib.cli import main
from openspeclib.combine import build_library
from openspeclib.models import (
    CollectionMetadata,
    DatasetInfo,
    Inclusion,
    MaterialCategory,
    WavelengthUnit,
)

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = [ROOT / "README.md", ROOT / "docs" / "reading.md"]

_MARKED_BLOCK = re.compile(
    r"<!-- readme-test: (?P<tag>[\w-]+) -->\s*\n```(?P<lang>\w+)\n(?P<code>.*?)\n```",
    re.DOTALL,
)

AXIS_UM = [0.35, 0.5, 1.0, 1.5, 2.0, 2.5]
AXIS_NM = [350.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0]


def _blocks(path: Path) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in _MARKED_BLOCK.finditer(path.read_text(encoding="utf-8")):
        tag = match.group("tag")
        assert tag not in blocks, f"{path.name}: duplicate readme-test tag {tag!r}"
        blocks[tag] = match.group("code")
    return blocks


def _all_blocks(prefix: str) -> list[tuple[str, str]]:
    found = [
        (f"{path.name}:{tag}", code)
        for path in DOCUMENTS
        for tag, code in _blocks(path).items()
        if tag.startswith(prefix)
    ]
    return found


def _usgs_records() -> list:
    return [
        make_record(
            "splib07a_Olivine_GDS70",
            wavelengths=AXIS_UM,
            values=[0.04, 0.06, 0.2, 0.3, 0.35, float("nan")],
        ),
        make_record(
            "splib07a_Quartz_GDS31",
            wavelengths=AXIS_UM,
            values=[0.5, 0.6, 0.7, 0.7, 0.65, 0.6],
        ).model_copy(
            update={"material": make_record().material.model_copy(update={"name": "Quartz"})}
        ),
    ]


def _ecosis_records() -> list:
    record = make_record(
        "5b7c365aa78fc80b50ffbbd1",
        collection="ecosis",
        wavelengths=AXIS_NM,
        values=[0.03, 0.05, 0.45, 0.35, 0.2, 0.1],
        unit=WavelengthUnit.NANOMETERS,
        category=MaterialCategory.VEGETATION,
        license="CC-BY-4.0",
        dataset_id="pkg1",
    )
    return [
        record.model_copy(
            update={"material": record.material.model_copy(update={"name": "olivine-rich leaf"})}
        )
    ]


def _ecosis_meta() -> CollectionMetadata:
    return CollectionMetadata(
        collection=make_collection_info(
            "ecosis",
            license="NOASSERTION",
            license_refs={},
            inclusion=Inclusion.SUBSET,
            inclusion_notes="Curated selection.",
        ),
        datasets={
            "pkg1": DatasetInfo(
                id="pkg1",
                title="Test package",
                license="CC-BY-4.0",
                citation="Test package citation.",
            )
        },
    )


@pytest.fixture(scope="module")
def release(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("release")
    build_library(
        {
            "usgs_splib07": (
                _usgs_records(),
                CollectionMetadata(collection=make_collection_info()),
            ),
            "ecosis": (_ecosis_records(), _ecosis_meta()),
        },
        out,
        release="0.2.0",
    )
    result = CliRunner().invoke(
        main,
        [
            "validate",
            str(out),
            "--profile",
            "openspeclib",
            "--report",
            str(out / "validation-report.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    return out


def _run(code: str, release: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.chdir(release)
    namespace: dict = {"__name__": "__main__"}
    exec(compile(code, "readme-block", "exec"), namespace)
    return namespace


def test_release_layout(release: Path) -> None:
    assert sorted(p.name for p in release.iterdir()) == [
        "checksums.txt",
        "ecosis.parquet",
        "id-migration.csv",
        "openspeclib-index.parquet",
        "usgs_splib07.parquet",
        "validation-report.json",
    ]
    assert json.loads((release / "validation-report.json").read_text())["valid"] is True


def test_readme_has_the_marked_blocks() -> None:
    assert {"python", "python-pyarrow", "sql", "sql-footer"} <= set(_blocks(ROOT / "README.md"))


def test_readme_quickstart(release: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    ns = _run(_blocks(ROOT / "README.md")["python"], release, monkeypatch)
    out = capsys.readouterr().out

    assert "3 spectra in ['ecosis', 'usgs_splib07']" in out
    assert "2 olivine reflectance spectra cover 400-2500 nm" in out
    assert "6 bands, 350 nm and up" in out
    assert "License: " in out and "Cite: " in out
    assert ns["spectrum"].id in {
        "ecosis:5b7c365aa78fc80b50ffbbd1",
        "usgs_splib07:splib07a_Olivine_GDS70",
    }


def test_readme_licence_lookup_for_a_dataset(
    release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _blocks(ROOT / "README.md")["python"].replace(
        'hits["id"][0].as_py()', '"ecosis:5b7c365aa78fc80b50ffbbd1"'
    )
    ns = _run(code, release, monkeypatch)
    assert ns["terms"] == "CC-BY-4.0"
    assert ns["cite"]["citation"] == "Test package citation."


def test_readme_licence_lookup_for_a_licenseref(
    release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _blocks(ROOT / "README.md")["python"].replace(
        'hits["id"][0].as_py()', '"usgs_splib07:splib07a_Olivine_GDS70"'
    )
    ns = _run(code, release, monkeypatch)
    assert ns["terms"] == "U.S. Government work, public domain"
    assert ns["cite"]["citation"] == "Test citation."
    assert len(ns["spectrum"].values) == 6


def test_readme_pyarrow_block(release: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run(_blocks(ROOT / "README.md")["python-pyarrow"], release, monkeypatch)
    out = capsys.readouterr().out
    assert "Test collection usgs_splib07 - 2 mineral reflectance spectra" in out
    assert "usgs_splib07:splib07a_Olivine_GDS70 um 6 bands" in out


@pytest.mark.parametrize("name,code", _all_blocks("python"), ids=lambda v: str(v)[:40])
def test_every_python_block_runs(
    name: str, code: str, release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(code, release, monkeypatch)


@pytest.mark.parametrize("name,code", _all_blocks("sql"), ids=lambda v: str(v)[:40])
def test_every_sql_block_runs(
    name: str, code: str, release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.chdir(release)
    rows = duckdb.sql(code).fetchall()
    assert rows, f"{name} returned no rows"
    assert all(value is not None for value in rows[0]), f"{name} returned nulls: {rows[0]}"


def test_readme_sql_reads_the_axis(release: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.chdir(release)
    rows = duckdb.sql(_blocks(ROOT / "README.md")["sql"]).fetchall()
    assert [r[0] for r in rows] == [
        "usgs_splib07:splib07a_Olivine_GDS70",
        "usgs_splib07:splib07a_Quartz_GDS31",
    ]
    _, _, quantity, geometry, unit, wavelengths, values = rows[0]
    assert (quantity, geometry, unit) == ("reflectance", "unknown", "um")
    assert wavelengths == AXIS_UM
    assert values[0] == pytest.approx(0.04)


def test_readme_sql_footer(release: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.chdir(release)
    (value,) = duckdb.sql(_blocks(ROOT / "README.md")["sql-footer"]).fetchone()
    footer = json.loads(value)
    assert footer["collection"]["id"] == "usgs_splib07"
    assert footer["format_version"].startswith("1.")
