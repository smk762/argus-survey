"""Pure-Python checks of volume.py's CRS gate and vectors.py's ogr2ogr parsing (no tools needed)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus_survey import vectors, volume
from argus_survey.models import CrsRecord, CrsRef, SurveyError

MGA50 = CrsRef(name="GDA2020 / MGA zone 50", epsg=7850, kind="ProjectedCRS")


def rec(vertical: CrsRef | None, height: str = "gravity") -> CrsRecord:
    return CrsRecord(
        definition="x",
        name="x",
        horizontal=MGA50,
        vertical=vertical,
        height_reference=height if vertical else "none",  # type: ignore[arg-type]
    )


def gate(
    monkeypatch: pytest.MonkeyPatch, job_crs: CrsRecord, raster: CrsRecord, assume: str | None = None
) -> str | None:
    monkeypatch.setattr(volume, "raster_crs", lambda *_a, **_k: raster)
    job = SimpleNamespace(sidecar=SimpleNamespace(crs=job_crs), sink=None)
    return volume.check_raster_crs(job, Path("design.tif"), assume_vertical=assume, label="design", step="t")  # type: ignore[arg-type]


def test_unidentified_vertical_datums_are_compared_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    job = rec(CrsRef(name="Local datum A", kind="VerticalCRS"))
    assert gate(monkeypatch, job, rec(CrsRef(name="Local datum A", kind="VerticalCRS"))) is None
    with pytest.raises(SurveyError, match="Local datum B"):
        gate(monkeypatch, job, rec(CrsRef(name="Local datum B", kind="VerticalCRS")))


def test_epsg_codes_decide_when_both_have_one(monkeypatch: pytest.MonkeyPatch) -> None:
    job = rec(CrsRef(name="AHD height", epsg=5711, kind="VerticalCRS"))
    assert gate(monkeypatch, job, rec(CrsRef(name="AHD", epsg=5711, kind="VerticalCRS"))) is None
    with pytest.raises(SurveyError):
        gate(monkeypatch, job, rec(CrsRef(name="AHD height", epsg=9458, kind="VerticalCRS")))


def test_stated_vertical_against_a_job_without_epsg_is_refused_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    job = rec(CrsRef(name="ellipsoidal height", kind="EllipsoidalHeight"), "ellipsoidal")
    with pytest.raises(SurveyError, match="no EPSG code") as exc:
        gate(monkeypatch, job, rec(None), "EPSG:5711")
    assert "EPSG:None" not in str(exc.value)
    assert "ellipsoidal" in gate(monkeypatch, job, rec(None), "ellipsoidal")  # type: ignore[operator]
    with pytest.raises(SurveyError, match="ellipsoidal"):
        gate(monkeypatch, rec(CrsRef(name="AHD height", epsg=5711, kind="VerticalCRS")), rec(None), "ellipsoidal")


def test_stated_vertical_matching_the_job_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    job = rec(CrsRef(name="AHD height", epsg=5711, kind="VerticalCRS"))
    assert "EPSG:5711" in gate(monkeypatch, job, rec(None), "epsg:5711")  # type: ignore[operator]


def _fake_tools(monkeypatch: pytest.MonkeyPatch, layers: list[dict[str, object]], csv_out: str) -> None:
    monkeypatch.setattr(vectors, "run_json", lambda *_a, **_k: {"layers": layers})
    monkeypatch.setattr(
        vectors, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout=csv_out, stderr="")
    )


def test_huge_wkt_field_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    ring = ", ".join(f"{391000 + i * 0.001:.3f} {6464000 + (i % 7) * 0.001:.3f}" for i in range(20_000))
    wkt = f"POLYGON (({ring}, 391000.000 6464000.000))"
    assert len(wkt) > 131_072
    _fake_tools(monkeypatch, [{"name": "a"}], f'WKT,name\n"{wkt}",pile\n')
    feats = vectors.read_polygons(Path("p.gpkg"), "EPSG:7850", src_srs="EPSG:7850", sink=None, step="t")
    assert feats == [("pile", wkt)]


def test_multi_layer_source_is_refused_naming_the_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tools(monkeypatch, [{"name": "piles"}, {"name": "pads"}], "")
    with pytest.raises(SurveyError, match="'piles', 'pads'"):
        vectors.read_polygons(Path("p.gpkg"), "EPSG:7850", src_srs="EPSG:7850", sink=None, step="t")
