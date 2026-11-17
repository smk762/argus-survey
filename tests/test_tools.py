from __future__ import annotations

import sys

import pytest

from argus_survey import tools
from argus_survey.models import CommandRecord


def test_run_records_and_returns_output() -> None:
    records: list[CommandRecord] = []
    proc = tools.run([sys.executable, "-c", "print('hi')"], step="t", sink=records.append)
    assert proc.stdout.strip() == "hi"
    assert len(records) == 1
    rec = records[0]
    assert rec.step == "t" and rec.returncode == 0 and rec.argv[1:] == ["-c", "print('hi')"]
    assert rec.seconds >= 0


def test_run_failure_is_recorded_and_raised_with_stderr_tail() -> None:
    records: list[CommandRecord] = []
    code = "import sys; sys.stderr.write('first\\nboom happened\\n'); sys.exit(3)"
    with pytest.raises(tools.ToolError) as info:
        tools.run([sys.executable, "-c", code], step="t", sink=records.append)
    assert info.value.returncode == 3
    assert "boom happened" in str(info.value)
    assert "first" in info.value.stderr_tail
    assert records and records[0].returncode == 3


def test_run_missing_tool() -> None:
    with pytest.raises(tools.ToolMissing):
        tools.run(["definitely-not-a-tool-xyz"], step="t")


def test_run_forces_proj_network_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJ_NETWORK", "ON")
    proc = tools.run([sys.executable, "-c", "import os; print(os.environ['PROJ_NETWORK'])"], step="t")
    assert proc.stdout.strip() == "OFF"


def test_run_json_parses() -> None:
    assert tools.run_json([sys.executable, "-c", "print('{\"a\": 1}')"], step="t") == {"a": 1}


def test_run_json_rejects_non_json() -> None:
    with pytest.raises(tools.ToolError):
        tools.run_json([sys.executable, "-c", "print('nope')"], step="t")


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        (tools._PDAL_RE, "------\npdal 2.10.2 (git-version: 21d285)\n------", "2.10.2"),
        (tools._GDAL_RE, 'GDAL 3.13.3 "Iowa City", released 2026/08/13', "3.13.3"),
        (tools._GDAL_RE, "GDAL 3.8.4, released 2024/02/08", "3.8.4"),
        (tools._PROJ_RE, "Rel. 9.9.0, September 15th, 2026\nusage: proj", "9.9.0"),
        (tools._PROJ_RE, "garbage", None),
    ],
)
def test_parse_version(pattern, text: str, expected: str | None) -> None:  # type: ignore[no-untyped-def]
    assert tools.parse_version(pattern, text) == expected


def test_read_grid_manifest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    a, b, c, d = "a" * 64, "B" * 64, "c" * 64, "d" * 64
    p = tmp_path / "m.sha256"
    lines = [f"{a}  one.tif", "# comment", f"{b} *two.tif", "", f"{c}  my tile.tif", f"\\{d}  back\\\\slash\\nnl.tif"]
    p.write_text("\n".join(lines + ["not a manifest line", "abc  short.tif"]) + "\n")
    assert tools.read_grid_manifest(p) == {
        "one.tif": a,
        "two.tif": "b" * 64,
        "my tile.tif": c,
        "back\\slash\nnl.tif": d,
    }


@pytest.mark.tools
def test_versions_detected() -> None:
    v = tools.versions()
    assert v.pdal and v.gdal and v.proj


@pytest.mark.tools
def test_pdal_pipeline_records_stages(tmp_path) -> None:  # type: ignore[no-untyped-def]
    src = tmp_path / "p.csv"
    src.write_text("X,Y,Z\n1,2,3\n4,5,6\n")
    out = tmp_path / "p.las"
    records: list[CommandRecord] = []
    stages = [{"type": "readers.text", "filename": str(src)}, {"type": "writers.las", "filename": str(out)}]
    tools.pdal_pipeline(stages, step="t", sink=records.append)
    assert out.is_file()
    assert records[0].pipeline == stages and records[0].tool == "pdal"


@pytest.mark.tools
def test_doctor_runs() -> None:
    report = tools.doctor(verify_grid_digests=False)
    names = {c.name for c in report.checks}
    assert "exe:pdal" in names and "pdal:drivers" in names
    failing = [c for c in report.checks if c.name.startswith(("exe:", "pdal:", "gdal:", "version:")) and not c.ok]
    assert not failing, failing


def test_fatal_stderr_turns_exit_zero_into_failure() -> None:
    code = "import sys; sys.stderr.write('(filters.projpipeline Error) Coordinate to transform falls outside grid\\n')"
    records: list[CommandRecord] = []
    with pytest.raises(tools.ToolError, match="refused"):
        tools.run([sys.executable, "-c", code], step="t", sink=records.append, fatal_stderr=("falls outside grid",))
    assert len(records) == 1 and records[0].returncode == tools.RC_REFUSED  # the log must not read as a success


def test_run_missing_tool_is_recorded() -> None:
    records: list[CommandRecord] = []
    with pytest.raises(tools.ToolMissing):
        tools.run(["definitely-not-a-tool-xyz"], step="t", sink=records.append)
    assert [r.returncode for r in records] == [tools.RC_MISSING]


def test_run_timeout_is_recorded_with_stderr() -> None:
    records: list[CommandRecord] = []
    code = "import sys, time; sys.stderr.write('stuck here\\n'); sys.stderr.flush(); time.sleep(30)"
    with pytest.raises(tools.ToolError, match="timed out") as info:
        tools.run([sys.executable, "-c", code], step="t", sink=records.append, timeout=1.5)
    assert info.value.returncode == tools.RC_TIMEOUT and "stuck here" in info.value.stderr_tail
    assert [r.returncode for r in records] == [tools.RC_TIMEOUT] and records[0].seconds >= 1


def test_gdal_algorithm_probe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda _name: "/usr/bin/gdal")

    def hang(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise tools.subprocess.TimeoutExpired(["gdal"], 30)

    monkeypatch.setattr(tools.subprocess, "run", hang)
    assert tools._gdal_algorithm_ok("calc") is False

    def broken(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise PermissionError("not executable")

    monkeypatch.setattr(tools.subprocess, "run", broken)
    assert tools._gdal_algorithm_ok("calc") is False
