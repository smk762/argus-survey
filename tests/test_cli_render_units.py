"""CLI printing of data-derived strings: literal text, never Rich markup (no tools needed)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rich.console import Console

from argus_survey import cli, tools
from argus_survey.models import CrsRecord, VolumeReport


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> Console:
    c = Console(record=True, width=300)
    monkeypatch.setattr(cli, "console", c)
    return c


def test_volume_zone_names_print_literally(console: Console) -> None:
    z = SimpleNamespace(
        name="Pad [A] cut[/]",
        area_m2=1.0,
        above_m3=2.0,
        below_m3=0.0,
        net_m3=2.0,
        void_percent=0.0,
        ground_area_m2=1.0,
        ground_net_m3=2.0,
        base="plane [bold]",
        base_fit_rms_m=None,
    )
    r = VolumeReport.model_construct(
        kind="stockpile", surface="dsm", res=0.5, zones=[z], combined_scale_factor=1.0, sign_convention="x [/] y"
    )
    cli._print_volumes(r)
    text = console.export_text()
    assert text.count("Pad [A] cut[/]") == 3  # table, ground line, base line
    assert "plane [bold]" in text and "x [/] y" in text


def test_crs_height_reference_is_shown(console: Console) -> None:
    rec = CrsRecord.model_construct(
        name="GDA2020 [x]",
        definition="EPSG:7850+5711",
        horizontal=SimpleNamespace(name="MGA [/z]", epsg=7850, units="metre"),
        vertical=SimpleNamespace(name="AHD height", epsg=5711, units="metre"),
        height_reference="gravity",
        geoid="AUSGeoid2020 [g]",
        vertical_assumed="assumed [/]",
        epoch=None,
    )
    cli._print_crs(rec)
    text = console.export_text()
    for s in ("GDA2020 [x]", "MGA [/z]", "metre [gravity]", "AUSGeoid2020 [g]", "assumed [/]"):
        assert s in text, s


def test_doctor_details_print_literally(console: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    report = SimpleNamespace(
        versions=SimpleNamespace(pdal="2.8 [x]", gdal="3.11", proj="9.6"),
        checks=[SimpleNamespace(name="grid [proj]", ok=False, required=True, detail="missing [/opt/geo] [/]")],
        ok=False,
    )
    monkeypatch.setattr(tools, "doctor", lambda **_kw: report)
    cli.doctor(as_json=False, strict=False, no_digests=True)
    text = console.export_text()
    assert "2.8 [x]" in text and "grid [proj]" in text and "missing [/opt/geo] [/]" in text
