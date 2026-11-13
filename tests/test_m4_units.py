"""Pure-Python pieces of volumes, accuracy and profiles."""

from __future__ import annotations

import math
import random

import pytest

from argus_survey.accuracy import group_of, group_stats, statement_for
from argus_survey.models import CheckResidual, GridSpec, SurveyError
from argus_survey.profile import nice_ticks, svg
from argus_survey.vectors import densify, wkt_bbox, wkt_rings
from argus_survey.volume import fit_plane, plane_raster


def test_fit_plane_recovers_exact_plane() -> None:
    rnd = random.Random(1)
    pts = [(x, y, 5 + 0.03 * x - 0.02 * y) for x, y in ((rnd.uniform(0, 50), rnd.uniform(0, 50)) for _ in range(40))]
    a, b, c, mx, my, rms = fit_plane(pts)
    assert b == pytest.approx(0.03) and c == pytest.approx(-0.02) and rms == pytest.approx(0, abs=1e-12)
    assert a + b * (10 - mx) + c * (20 - my) == pytest.approx(5 + 0.3 - 0.4)


def test_fit_plane_degenerate() -> None:
    with pytest.raises(SurveyError):
        fit_plane([(0, 0, 1), (1, 1, 2)])
    with pytest.raises(SurveyError, match="straight line"):
        fit_plane([(i, i, i) for i in range(10)])


def test_plane_raster_cell_centres() -> None:
    g = GridSpec(origin_x=0, origin_y=0, res=1, width=2, height=2)
    vals = plane_raster(g, 0, 1, 10, 0, 0)  # z = x + 10 y, top row first
    assert list(vals) == [0.5 + 15, 1.5 + 15, 0.5 + 5, 1.5 + 5]


def test_wkt_parsing() -> None:
    wkt = "POLYGON ((0 0,10 0,10 5,0 5,0 0),(2 2,3 2,3 3,2 2))"
    rings = wkt_rings(wkt)
    assert len(rings) == 2 and rings[0][2] == (10.0, 5.0)
    assert wkt_bbox(wkt) == (0, 0, 10, 5)
    assert wkt_rings("MULTIPOLYGON (((0 0,1 0,1 1,0 0)),((5 5,6 5,6 6,5 5)))")[1][0] == (5.0, 5.0)
    assert wkt_rings("LINESTRING (1e3 -2.5,4 5)") == [[(1000.0, -2.5), (4.0, 5.0)]]


def test_densify_includes_vertices_and_chainage() -> None:
    pts = densify([(0, 0), (3, 0), (3, 4)], 1.0)
    assert pts[0] == (0, 0, 0) and pts[-1] == (7.0, 3, 4)
    assert (3.0, 3, 0) in pts
    assert all(b[0] > a[0] for a, b in zip(pts, pts[1:], strict=False))


def test_nice_ticks() -> None:
    assert nice_ticks(0, 100) == [0, 20, 40, 60, 80, 100]
    t = nice_ticks(19.3, 31.7)
    assert t[0] >= 19.3 and t[-1] <= 31.7 and len(t) >= 4


def test_svg_breaks_at_voids() -> None:
    out = svg("t", [0, 1, 2, 3, 4], {"dtm": [1, 2, None, 3, 4]}, "Height")
    assert out.count("<polyline") == 2 and "vertical exaggeration" in out


@pytest.mark.parametrize(
    ("d", "g"),
    [("open", "NVA"), ("", "NVA"), ("Vegetated", "VVA"), ("veg - scrub", "VVA"), ("VVA", "VVA"), ("road", "NVA")],
)
def test_groups(d: str, g: str) -> None:
    assert group_of(d) == g


def _r(e: float | None, group: str = "NVA") -> CheckResidual:
    return CheckResidual(id="x", x=0, y=0, z_check=0, z_surface=None if e is None else e, residual=e, group=group)  # type: ignore[arg-type]


def test_group_stats_with_checkpoint_accuracy() -> None:
    rows = [_r(0.03), _r(-0.04), _r(None)]
    g = group_stats(rows, "NVA", 0.02)
    rmse = math.sqrt((0.03**2 + 0.04**2) / 2)
    assert g.n == 2 and g.excluded == 1
    assert g.rmse_data_m == pytest.approx(rmse, abs=1e-4)
    assert g.rmse_m == pytest.approx(math.sqrt(rmse**2 + 0.02**2), abs=1e-4)
    assert not g.meets_minimum_count and g.pass_fail_applies
    assert group_stats(rows, "VVA", None).n == 0


def _rows(residuals: list[float], group: str = "NVA") -> list[CheckResidual]:
    return [
        CheckResidual(id=f"p{i}", x=0, y=0, z_check=0, z_surface=e, residual=e, group=group)  # type: ignore[arg-type]
        for i, e in enumerate(residuals)
    ]


def test_group_stats_section_7_16_figures_and_blunders() -> None:
    res = [0.02, -0.01, 0.03, 0.0, -0.02] * 6 + [0.40]  # 31 points, one 40 cm blunder
    g = group_stats(_rows(res), "NVA", None, target_class_cm=10)
    assert g.n == 31 and g.meets_minimum_count
    assert (g.min_m, g.max_m, g.median_m) == (-0.02, 0.4, 0.0)
    assert g.blunders == ["p30"]  # > 3 × 10 cm (§7.2)
    # without a class the data RMSE is the yardstick (§C.2)
    assert group_stats(_rows(res), "NVA", None).blunders == ["p30"]


def test_statements_follow_section_7_16_1() -> None:
    ok = group_stats(_rows([0.05, -0.05] * 15), "NVA", None)  # RMSE 5 cm, n = 30
    s = statement_for(ok, 10)
    assert s.startswith("This data set was tested to meet ASPRS Positional Accuracy Standards for Digital Geospatial")
    assert "Edition 2, Version 2 (2024) for a 10 (cm) RMSEV Vertical Accuracy Class." in s
    assert s.endswith("The Non-Vegetated Vertical Accuracy (NVA) was found to be RMSEV = 5.0 (cm).")
    assert "does NOT meet" in statement_for(ok, 4)
    assert "tested as required by" in statement_for(ok, None)

    few = group_stats(_rows([0.05, -0.05] * 5, "VVA"), "VVA", None)  # n = 10
    s = statement_for(few, 10)
    assert "Although the Standards call for a minimum of thirty (30) checkpoints" in s
    assert "ONLY 10 checkpoints" in s and "produced to meet a 10 (cm) RMSEV Vertical Positional Accuracy Class" in s
    assert s.endswith("using the reduced number of checkpoints in the VVA tested area.")
    assert "produced to meet" not in statement_for(few, None)
    assert "does NOT meet" not in s  # VVA has no pass/fail

    # a failed NVA with too few checkpoints still says it failed
    few_nva = group_stats(_rows([0.05, -0.05] * 5), "NVA", None)  # RMSE 5 cm, n = 10
    assert "does NOT meet the 4 (cm)" in statement_for(few_nva, 4)
    assert "does NOT meet" not in statement_for(few_nva, 10)

    # a VVA is "tested to meet" a class only when the NVA met it
    vva = group_stats(_rows([0.05, -0.05] * 15, "VVA"), "VVA", None)
    assert statement_for(vva, 10, nva_meets=True).startswith("This data set was tested to meet")
    assert statement_for(vva, 10, nva_meets=False).startswith("This data set was tested as required by")


def test_vva_blunders_use_their_own_rmse_not_the_nva_class() -> None:
    # 8 cm residuals are "beyond 3 × 2 cm" for an NVA class, but a VVA has no target: only its own RMSE counts
    res = [0.08, -0.08] * 15
    assert group_stats(_rows(res, "VVA"), "VVA", None, target_class_cm=2).blunders == []
    assert len(group_stats(_rows(res), "NVA", None, target_class_cm=2).blunders) == 30


def test_ground_filter_params_refused_without_a_job() -> None:
    from argus_survey.ground import filter_params

    assert filter_params("smrf", preset="stockpile")["slope"] == 1.0
    with pytest.raises(SurveyError, match="unknown ground method"):
        filter_params("pmf")
    with pytest.raises(SurveyError, match="only, not csf"):
        filter_params("csf", preset="stockpile")
