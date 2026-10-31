"""Pure-Python CRS record parsing, against PROJJSON captured from projinfo (tests/data)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_survey import crs
from argus_survey.models import CrsRecord, CrsRef, SurveyError, TransformOp

DATA = Path(__file__).parent / "data"


def load(name: str) -> dict:
    return json.loads((DATA / f"projjson_{name}.json").read_text())


def test_compound_mga_ahd() -> None:
    r = crs.record_from_projjson(load("mga50_ahd"))
    assert r.definition == "EPSG:7850+5711"
    assert r.horizontal.epsg == 7850 and r.horizontal.units == "metre" and not r.horizontal.north_first
    assert r.vertical is not None and r.vertical.epsg == 5711 and r.vertical.name == "AHD height"
    assert r.height_reference == "gravity" and r.has_height_datum


def test_geographic_3d_is_ellipsoidal_and_lat_first() -> None:
    r = crs.record_from_projjson(load("gda2020_3d"))
    assert r.definition == "EPSG:7843"
    assert r.horizontal.kind == "GeographicCRS" and r.horizontal.north_first
    assert r.height_reference == "ellipsoidal" and r.vertical is not None and r.vertical.units == "metre"


def test_pcg2020_projected_without_vertical() -> None:
    r = crs.record_from_projjson(load("pcg2020"))
    assert r.horizontal.epsg == 8031 and "PCG2020" in r.name
    assert r.vertical is None and r.height_reference == "none" and not r.has_height_datum


def test_foot_units_are_refused() -> None:
    r = crs.record_from_projjson(load("oregon_ft"))
    assert r.horizontal.units == "foot" and r.horizontal.unit_factor == pytest.approx(0.3048)
    with pytest.raises(SurveyError, match="metres"):
        crs.require_metric(r, "the input CRS")


def test_metric_passes() -> None:
    crs.require_metric(crs.record_from_projjson(load("mga50_ahd")), "x")


def test_vertical_only_is_refused() -> None:
    pj = load("mga50_ahd")["components"][1]
    with pytest.raises(SurveyError, match="vertical CRS only"):
        crs.record_from_projjson(pj)


def test_boundcrs_is_unwrapped() -> None:
    inner = load("pcg2020")
    r = crs.record_from_projjson({"type": "BoundCRS", "source_crs": inner, "target_crs": {}, "transformation": {}})
    assert r.horizontal.epsg == 8031


def test_compound_without_top_id_composes_codes() -> None:
    pj = load("mga50_ahd")
    pj.pop("id", None)
    assert crs.record_from_projjson(pj).definition == "EPSG:7850+5711"


def test_no_ids_falls_back_to_projjson() -> None:
    pj = load("pcg2020")
    pj.pop("id")
    r = crs.record_from_projjson(pj)
    assert json.loads(r.definition)["name"] == pj["name"]


def test_pdal_srs_without_crs() -> None:
    assert crs.record_from_pdal_srs({"json": {}, "wkt": ""}) is None


def _rec(north_first: bool) -> CrsRecord:
    return CrsRecord(
        definition="X", name="X", horizontal=CrsRef(name="X", kind="GeographicCRS", north_first=north_first)
    )


def _op(proj: str) -> TransformOp:
    return TransformOp(source="a", target="b", name="op", accuracy_m=0.1, proj=proj)


def test_coord_op_swaps_lat_first_source() -> None:
    op = _op("+proj=pipeline +step +proj=axisswap +order=2,1 +step +proj=utm +zone=50 +south +ellps=GRS80")
    out = crs.pdal_coord_op(op, _rec(True), _rec(False))
    assert out.startswith("+proj=pipeline +step +proj=axisswap +order=2,1 +step +proj=axisswap +order=2,1")


def test_coord_op_keeps_global_params_before_first_step() -> None:
    op = _op("+proj=pipeline +ellps=GRS80 +step +proj=utm +zone=50 +south")
    assert crs.pdal_coord_op(op, _rec(True), _rec(True)) == (
        "+proj=pipeline +ellps=GRS80 +step +proj=axisswap +order=2,1 +step +proj=utm +zone=50 +south"
        " +step +proj=axisswap +order=2,1"
    )


def test_coord_op_wraps_single_operation() -> None:
    assert crs.pdal_coord_op(_op("+proj=utm +zone=50"), _rec(False), _rec(False)) == (
        "+proj=pipeline +step +proj=utm +zone=50"
    )


def test_same_crs() -> None:
    a = crs.record_from_projjson(load("mga50_ahd"))
    b = crs.record_from_projjson(load("mga50_ahd"), definition="something else")
    assert crs.same_crs(a, b)
    assert not crs.same_crs(a, crs.record_from_projjson(load("pcg2020")))
