"""Raster facts and snapped grids (pure Python; GDAL's JSON is stubbed)."""

from __future__ import annotations

import math
import sys
from array import array
from pathlib import Path
from typing import Any

import pytest

from argus_survey import grid as grid_mod
from argus_survey import rasters
from argus_survey.models import GridSpec, SurveyError


def _info(monkeypatch: pytest.MonkeyPatch, meta: dict[str, Any]) -> None:
    monkeypatch.setattr(rasters, "run_json", lambda *a, **k: meta)


def _stats_meta(md: dict[str, str], size: tuple[int, int] = (100, 100)) -> dict[str, Any]:
    return {"size": list(size), "bands": [{"metadata": {"": md}}]}


def test_stats_reads_gdal_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    md = {
        "STATISTICS_MINIMUM": "1",
        "STATISTICS_MAXIMUM": "9",
        "STATISTICS_MEAN": "4.5",
        "STATISTICS_STDDEV": "2",
        "STATISTICS_VALID_PERCENT": "95.24",
    }
    _info(monkeypatch, _stats_meta(md))
    s = rasters.stats(Path("x.tif"))
    assert (s.valid_cells, s.total_cells, s.minimum, s.maximum, s.mean, s.stddev) == (9524, 10000, 1, 9, 4.5, 2)


def test_stats_all_nodata_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # what GDAL 3.13 writes when no cell is valid
    _info(monkeypatch, _stats_meta({"STATISTICS_VALID_PERCENT": "0"}))
    s = rasters.stats(Path("x.tif"))
    assert s.valid_cells == 0 and s.minimum is None


def test_stats_tiny_valid_fraction_keeps_its_statistics(monkeypatch: pytest.MonkeyPatch) -> None:
    md = {k: "5" for k in ("STATISTICS_MINIMUM", "STATISTICS_MAXIMUM", "STATISTICS_MEAN", "STATISTICS_STDDEV")}
    _info(monkeypatch, _stats_meta({**md, "STATISTICS_VALID_PERCENT": "0"}, size=(100_000, 100_000)))
    s = rasters.stats(Path("x.tif"))
    assert s.valid_cells == 1 and s.minimum == 5


def test_stats_without_valid_percent_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    md = {k: "5" for k in ("STATISTICS_MINIMUM", "STATISTICS_MAXIMUM", "STATISTICS_MEAN", "STATISTICS_STDDEV")}
    _info(monkeypatch, _stats_meta(md))
    with pytest.raises(SurveyError, match="valid-cell percentage"):
        rasters.stats(Path("x.tif"))
    _info(monkeypatch, _stats_meta({}))
    with pytest.raises(SurveyError, match="no statistics"):
        rasters.stats(Path("x.tif"))


@pytest.mark.parametrize(
    ("gt", "ok"),
    [
        ([100.0, 0.5, 0.0, 200.0, 0.0, -0.5], True),
        ([100.0, -0.5, 0.0, 200.0, 0.0, 0.5], False),  # 180° flip: the sum is still 0
        ([100.0, 0.5, 0.0, 200.0, 0.0, 0.5], False),  # south-up
        ([100.0, 0.5, 0.1, 200.0, 0.0, -0.5], False),  # rotated
        ([100.0, 0.5, 0.0, 200.0, 0.0, -0.25], False),  # non-square
    ],
)
def test_grid_of_needs_north_up_square_cells(monkeypatch: pytest.MonkeyPatch, gt: list[float], ok: bool) -> None:
    _info(monkeypatch, {"geoTransform": gt, "size": [4, 2]})
    if ok:
        g = rasters.grid_of(Path("x.tif"))
        assert (g.origin_x, g.origin_y, g.res, g.width, g.height) == (100.0, 199.0, 0.5, 4, 2)
    else:
        with pytest.raises(SurveyError, match="north-up"):
            rasters.grid_of(Path("x.tif"))


def test_nodata_value_reads_gdal_json_strings() -> None:
    assert math.isnan(rasters.nodata_value("NaN"))  # type: ignore[arg-type]
    assert rasters.nodata_value("-Infinity") == -math.inf
    assert rasters.nodata_value(-9999) == -9999.0
    assert rasters.nodata_value(None) is None
    assert rasters.nodata_value("not a number") is None


def test_is_nodata() -> None:
    assert rasters.is_nodata(math.nan, None)
    assert rasters.is_nodata(math.nan, -9999.0)
    assert rasters.is_nodata(5.0, math.nan) is False
    assert rasters.is_nodata(-9999.0, -9999.0)
    assert not rasters.is_nodata(-9998.9, -9999.0)
    assert rasters.is_nodata(-math.inf, -math.inf)
    # a Float32 cell holding nodata given in decimal
    f32 = array("f", [-3.4028235e38])[0]
    assert f32 != -3.4028235e38 and rasters.is_nodata(f32, -3.4028235e38)
    assert not rasters.is_nodata(0.0, None)


def test_read_values_swaps_only_on_a_byte_order_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _info(monkeypatch, {"bands": [{"noDataValue": "NaN"}]})
    other = "big" if sys.byteorder == "little" else "little"

    for order in (sys.byteorder, other):

        def fake_run(argv: list[str], order: str = order, **_: Any) -> None:
            raw = Path(argv[-1])
            data = array("f", [1.5, -2.25])
            if order != sys.byteorder:
                data.byteswap()
            raw.write_bytes(data.tobytes())
            raw.with_suffix(".hdr").write_text(f"ENVI\nbyte order = {1 if order == 'big' else 0}\n")

        monkeypatch.setattr(rasters, "run", fake_run)
        values, nodata = rasters.read_values(Path("x.tif"))
        assert list(values) == [1.5, -2.25], order
        assert nodata is not None and math.isnan(nodata)


def test_snap_origin_never_exceeds_the_minimum() -> None:
    # float noise on a grid line keeps the line (0.3 / 0.1 == 2.9999999999999996)
    g = grid_mod.snap((0.3, 0.7, 1.0, 1.0), 0.1)
    assert (g.origin_x, g.origin_y) == (0.3, 0.7)
    # a minimum genuinely just below a line gets the cell below it
    g = grid_mod.snap((1.0 - 1e-10, 2.0 - 1e-10, 3.0, 4.0), 1.0)
    assert (g.origin_x, g.origin_y) == (0.0, 1.0)
    assert g.origin_x <= 1.0 - 1e-10 and g.origin_y <= 2.0 - 1e-10
    assert g.origin_x + g.width * g.res > 3.0 and g.origin_y + g.height * g.res > 4.0
    # MGA-sized coordinates
    g = grid_mod.snap((391_234.56, 6_456_789.01, 391_300.0, 6_456_800.0), 0.25)
    assert (g.origin_x, g.origin_y) == (391_234.5, 6_456_789.0)


@pytest.mark.parametrize(
    "bounds",
    [
        (10.0, 0.0, 5.0, 1.0),
        (0.0, 10.0, 1.0, 5.0),
        (math.inf, math.inf, -math.inf, -math.inf),
        (math.nan, 0.0, 1.0, 1.0),
    ],
)
def test_snap_refuses_degenerate_bounds(bounds: tuple[float, float, float, float]) -> None:
    with pytest.raises(SurveyError, match="degenerate bounds"):
        grid_mod.snap(bounds, 1.0)


def test_snap_single_point_gets_one_cell() -> None:
    g = grid_mod.snap((5.0, 5.0, 5.0, 5.0), 1.0)
    assert (g.width, g.height) == (1, 1)


def test_aligned_tolerance_is_in_ground_units() -> None:
    a = GridSpec(origin_x=0.0, origin_y=0.0, res=0.5, width=4, height=4)
    b = GridSpec(origin_x=10.0005, origin_y=-2.0, res=0.5, width=4, height=4)
    assert not grid_mod.aligned(a, b)
    assert grid_mod.aligned(a, b, tol=1e-3)  # origins 0.5 mm off the lattice, within a 1 mm tolerance
    assert grid_mod.aligned(a, GridSpec(origin_x=10.0, origin_y=-2.0, res=0.5, width=1, height=1))
    assert not grid_mod.aligned(a, GridSpec(origin_x=10.25, origin_y=-2.0, res=0.5, width=1, height=1))
