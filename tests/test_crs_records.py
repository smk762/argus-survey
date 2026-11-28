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


def test_survey_projection_refuses_geographic_and_mercator() -> None:
    crs.require_survey_projection(crs.record_from_projjson(load("mga50_ahd")), "x")
    with pytest.raises(SurveyError, match="geographic"):
        crs.require_survey_projection(crs.record_from_projjson(load("gda2020_3d")), "x")
    merc = CrsRecord(
        definition="EPSG:3857",
        name="WGS 84 / Pseudo-Mercator",
        horizontal=CrsRef(name="WGS 84 / Pseudo-Mercator", epsg=3857, kind="ProjectedCRS"),
    )
    with pytest.raises(SurveyError, match="Mercator"):
        crs.require_survey_projection(merc, "x")


def test_oblique_mercator_is_a_survey_projection() -> None:
    # NAD83 / Michigan Oblique Mercator (Hotine): conformal, ground-scale metres; only (Web) Mercator is refused
    r = crs.record_from_projjson(load("michigan_oblique"))
    assert "Oblique Mercator" in r.horizontal.name
    crs.require_survey_projection(r, "x")


def test_geocentric_is_refused() -> None:
    with pytest.raises(SurveyError, match="unsupported CRS type 'GeodeticCRS'"):
        crs.record_from_projjson(load("wgs84_geocentric"))
    ecef = CrsRecord(definition="EPSG:7842", name="GDA2020", horizontal=CrsRef(name="GDA2020", kind="GeodeticCRS"))
    with pytest.raises(SurveyError, match="geocentric"):
        crs.require_survey_projection(ecef, "x")


def test_coordinate_epoch_is_unwrapped_and_kept() -> None:
    pj = load("itrf2014_epoch")  # projinfo -o PROJJSON EPSG:7912@2020.5
    assert pj["type"] == "CoordinateMetadata"
    r = crs.record_from_projjson(pj)
    assert r.epoch == 2020.5 and r.horizontal.epsg == 7912 and r.height_reference == "ellipsoidal"
    assert r.definition == "EPSG:7912@2020.5"
    assert crs.record_from_projjson(pj, definition="EPSG:7912@2020.5").definition == "EPSG:7912@2020.5"
    pj["crs"].pop("id")
    assert json.loads(crs.record_from_projjson(pj).definition)["coordinateEpoch"] == 2020.5
    pj["crs"]["id"] = {"authority": "EPSG", "code": 7912}
    pj["coordinateEpoch"] = 2020.12345  # every digit reaches PROJ, not :g's six
    assert crs.record_from_projjson(pj).definition == "EPSG:7912@2020.12345"
    pj["coordinateEpoch"] = True  # not a number
    assert crs.record_from_projjson(pj).epoch is None


@pytest.mark.parametrize(
    "bad",
    [
        "--searchpaths",
        "-s",
        " -o",
        "@/etc/passwd",
        "@job.json",
        # GDAL (gdaltransform -s_srs) opens what it doesn't recognise as a file, and fetches URLs
        "/etc/passwd",
        "../../secrets/site.prj",
        "/proc/self/environ",
        "site.prj",
        "http://169.254.169.254/latest/meta-data",
        "https://example.com/crs.wkt",
        "+init=/etc/passwd:x",
        "+proj=utm +zone=50 +init=../epsg:7850",
        # every PROJ parameter that opens a file or, with PROJ networking on, a URL
        "+proj=longlat +nadgrids=/etc/shadow",
        "+proj=longlat +ellps=GRS80 +nadgrids=@null,../../secrets/x.gsb",
        "+proj=longlat +geoidgrids=./site.gtx",
        "+proj=pipeline +step +proj=vgridshift +grids=http://169.254.169.254/x",
        "+proj=pipeline +step +proj=hgridshift +grids=@C:\\grids\\x.tif",
        "+proj=pipeline +step +proj=tinshift +file=/proc/self/environ",
        "+proj=longlat +NADGRIDS=/etc/shadow",
        'PROJCS["x",EXTENSION["PROJ4","+proj=longlat +nadgrids=/etc/shadow"]]',
        'GEOGCS["x",EXTENSION["PROJ4_GRIDS","/etc/shadow"]]',
        "",
    ],
)
def test_option_or_file_definitions_are_refused(bad: str) -> None:
    with pytest.raises(SurveyError, match="not a CRS definition"):
        crs.describe(bad)
    with pytest.raises(SurveyError, match="not a CRS definition"):
        crs.select_operation(bad, "EPSG:7850")
    with pytest.raises(SurveyError, match="not a CRS definition"):
        crs.promote_3d(bad)


@pytest.mark.parametrize(
    "ok",
    [
        "EPSG:7850+5711",
        "EPSG:7850+EPSG:5711",
        "epsg:7850",
        "OGC:CRS84",
        "IGNF:LAMB93",
        "urn:ogc:def:crs:EPSG::7850",
        "+proj=utm +zone=50 +south",
        "+init=epsg:7850",
        "+proj=longlat +ellps=GRS80 +nadgrids=@null",
        "+proj=longlat +geoidgrids=au_ga_AUSGeoid2020_20180201.tif",
        "+proj=pipeline +step +proj=hgridshift +grids=@ntv2_0.gsb,conus",
        'GEOGCS["x",EXTENSION["PROJ4_GRIDS","@conus,@alaska"]]',
        '{"type": "ProjectedCRS"}',
        "PROJCRS[]",
        'COMPD_CS["GDA94 / MGA zone 50 + AHD height",PROJCS["a/b"],VERT_CS["AHD height"]]',
        ' GEOGCRS["GDA2020",\n  DATUM["x"]]',
    ],
)
def test_real_definitions_pass_the_check(ok: str) -> None:
    assert crs.check_definition(ok) == ok


def test_lon_extent_across_the_antimeridian() -> None:
    assert crs.lon_extent([115.8, 116.1, 115.9]) == (115.8, 116.1)
    assert crs.lon_extent([179.9, -179.8, 179.95]) == pytest.approx((179.9, -179.8))  # west > east
    assert crs.lon_extent([-170.0, -60.0, 60.0, 170.0]) == (-170.0, 170.0)  # genuinely wide: left alone


def test_lonlat_bbox_across_the_antimeridian(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from argus_survey.models import Bounds3D

    out = "179.95 -17.0\n-179.95 -17.0\n179.98 -16.9\n"
    monkeypatch.setattr(crs, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    b = Bounds3D(minx=0, miny=0, minz=0, maxx=1, maxy=1, maxz=1)
    assert crs.lonlat_bbox("EPSG:3460", b) == pytest.approx((179.95, -17.0, -179.95, -16.9))
