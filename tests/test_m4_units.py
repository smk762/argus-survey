"""Pure-Python pieces of volumes, accuracy and profiles."""

from __future__ import annotations

import math
import random
import subprocess

import pytest

from argus_survey import accuracy
from argus_survey.accuracy import group_of, group_stats, read_checks, statement_for
from argus_survey.change import overlap
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
    # without a class each residual is screened against the RMSE of the others (leave-one-out)
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
    # 8 cm residuals are "beyond 3 × 2 cm" for an NVA class, but a VVA has no target, so its own residuals set the bar
    res = [0.08, -0.08] * 15
    assert group_stats(_rows(res, "VVA"), "VVA", None, target_class_cm=2).blunders == []
    assert len(group_stats(_rows(res), "NVA", None, target_class_cm=2).blunders) == 30


def test_blunders_without_a_class_are_screened_leave_one_out() -> None:
    # the group's own RMSE (2.89 m here) would hide a 5 m error: max |e| <= sqrt(n) * RMSE
    assert group_stats(_rows([0.01, 0.01, 5.0], "VVA"), "VVA", None).blunders == ["p2"]
    assert group_stats(_rows([0.01, -0.02, 0.015, 5.0]), "NVA", None).blunders == ["p3"]
    assert group_stats(_rows([0.01, 5.0], "VVA"), "VVA", None).blunders == []  # too few to screen


def test_nva_pass_fail_matches_the_printed_rmse() -> None:
    at_limit = group_stats(_rows([0.07, -0.07] * 15), "NVA", None)  # 100 * 0.07 == 7.000000000000001
    s = statement_for(at_limit, 7)
    assert "RMSEV = 7.0 (cm)" in s and "does NOT meet" not in s
    assert accuracy.meets_class(0.0704, 7) and not accuracy.meets_class(0.0706, 7)


def _csv(tmp_path, text: str):
    p = tmp_path / "checks.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_read_checks_refuses_bad_rows(tmp_path) -> None:
    cols = ("id", "x", "y", "z")
    ok = read_checks(_csv(tmp_path, "id,x,y,z\np1,1,2,3\n"), cols)
    assert ok == [{"id": "p1", "x": 1.0, "y": 2.0, "z": 3.0, "description": ""}]
    with pytest.raises(SurveyError, match="row 3 is short"):
        read_checks(_csv(tmp_path, "id,x,y,z\np1,1,2,3\np2,1,2\n"), cols)
    for bad in ("nan", "inf", "-inf"):
        with pytest.raises(SurveyError, match="non-finite coordinate in row 2"):
            read_checks(_csv(tmp_path, f"id,x,y,z\np1,1,2,{bad}\n"), cols)
    with pytest.raises(SurveyError, match="bad number in row 2"):
        read_checks(_csv(tmp_path, "id,x,y,z\np1,1,two,3\n"), cols)
    with pytest.raises(SurveyError, match="not unique"):
        read_checks(_csv(tmp_path, "id,x,y,z\np1,1,2,3\np1,4,5,6\n"), cols)


def test_cct_error_lines_refused_as_survey_error(monkeypatch) -> None:
    from types import SimpleNamespace

    from argus_survey import crs as crs_mod

    src = SimpleNamespace(has_height_datum=True, definition="EPSG:7844+5711")
    job = SimpleNamespace(sidecar=SimpleNamespace(crs=SimpleNamespace(definition="EPSG:7850+5711")), sink=None)
    monkeypatch.setattr(crs_mod, "describe", lambda *a, **k: src)
    monkeypatch.setattr(crs_mod, "same_crs", lambda a, b: False)
    monkeypatch.setattr(crs_mod, "lonlat_bbox", lambda *a, **k: (0, 0, 1, 1))
    monkeypatch.setattr(crs_mod, "select_operation", lambda *a, **k: "op")
    monkeypatch.setattr(crs_mod, "pdal_coord_op", lambda *a, **k: "+proj=noop")
    out = "400000.0 6400000.0 10.0 0\n# Record 1 TRANSFORMATION ERROR: 200 9 9 (Point outside of projection domain)\n"
    monkeypatch.setattr(accuracy, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=out, stderr=""))
    pts = [{"x": "1", "y": "2", "z": "3"}, {"x": "200", "y": "9", "z": "9"}]
    with pytest.raises(SurveyError, match="outside the transformation's area or grid"):
        accuracy.transform_points(job, pts, "EPSG:7844+5711")  # type: ignore[arg-type]


def test_change_overlap_needs_a_whole_cell() -> None:
    a = GridSpec(origin_x=0, origin_y=0, res=1, width=10, height=10)
    assert overlap(a, GridSpec(origin_x=5, origin_y=5, res=1, width=10, height=10)).width == 5
    with pytest.raises(SurveyError, match="do not overlap"):
        overlap(a, GridSpec(origin_x=10, origin_y=0, res=1, width=10, height=10))  # shares an edge only
    with pytest.raises(SurveyError, match="do not overlap"):
        overlap(a, GridSpec(origin_x=9.6, origin_y=0, res=1, width=10, height=10))  # 0.4 of a cell


def test_nice_ticks_degenerate_ranges() -> None:
    assert nice_ticks(float("nan"), 1) == [] and nice_ticks(0, float("inf")) == []
    t = nice_ticks(100.0, 100.00000000000001)  # used to loop forever: step below the float spacing
    assert t and len(t) == len(set(t)) <= 62
    t = nice_ticks(5e6, 5e6 + 1e-10)
    assert t and len(t) == len(set(t))
    assert nice_ticks(3, 3) == nice_ticks(3, 4)


def test_svg_degenerate_chainage() -> None:
    assert "vertical exaggeration" in svg("t", [5.0], {"dtm": [1.0]}, "Height")  # c0 == c1 != 0
    with pytest.raises(SurveyError):
        svg("t", [], {"dtm": [1.0]}, "Height")


def test_ground_filter_params_refused_without_a_job() -> None:
    from argus_survey.ground import filter_params

    assert filter_params("smrf", preset="stockpile")["slope"] == 1.0
    with pytest.raises(SurveyError, match="unknown ground method"):
        filter_params("pmf")
    with pytest.raises(SurveyError, match="only, not csf"):
        filter_params("csf", preset="stockpile")


def test_an_empty_group_has_no_statistics() -> None:
    """No usable residuals (every VVA point outside coverage, say): every statistic is None and it still validates."""
    from argus_survey.models import AccuracyGroup

    g = group_stats([_r(None, "VVA"), _r(0.01, "NVA")], "VVA", checkpoint_rmse=0.01)
    assert (g.n, g.excluded) == (0, 1) and g.rmse_data_m is None and g.rmse_m is None and g.mean_m is None
    assert AccuracyGroup.model_validate(g.model_dump()) == g


def test_ticks_stay_distinct_at_any_magnitude() -> None:
    for lo, hi in ((1e-12, 2e-12), (0.0, 3e-11), (5.0, 5.4), (0.0, 1e9)):
        ticks = nice_ticks(lo, hi)
        assert len(ticks) >= 3 and len(set(ticks)) == len(ticks), (lo, hi, ticks)
    assert nice_ticks(0.0, 1.0) == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]  # still round numbers, no float noise


def test_an_isolated_sample_is_drawn_as_a_marker() -> None:
    """A valid height with a void on both sides has no line to belong to; it must not vanish from the plot."""
    out = svg("t", [0.0, 1.0, 2.0, 3.0, 4.0], {"dtm": [None, 21.0, None, 22.0, None]}, "Height (m)")
    assert out.count("<circle") == 2 and "<polyline" not in out
    assert svg("t", [0.0], {"dtm": [21.0]}, "Height (m)").count("<circle") == 1  # a one-sample profile
    joined = svg("t", [0.0, 1.0, 2.0], {"dtm": [21.0, 21.5, None]}, "Height (m)")
    assert joined.count("<polyline") == 1 and "<circle" not in joined


def test_check_points_keep_their_precision_into_a_geographic_job_crs(monkeypatch: pytest.MonkeyPatch) -> None:
    """cct's -d applies to every column: 6 decimals of a degree is ~0.1 m, the size of the errors under test."""
    from types import SimpleNamespace

    job = SimpleNamespace(sidecar=SimpleNamespace(crs=SimpleNamespace(definition="EPSG:7843")), sink=None)
    src = SimpleNamespace(definition="EPSG:7850+5711", has_height_datum=True)
    lon, lat, h = 115.8123456789, -31.9123456789, 12.345

    def fake_cct(argv, **_kw):  # type: ignore[no-untyped-def]
        d = int(argv[argv.index("-d") + 1])
        return SimpleNamespace(stdout=f"{lon:.{d}f} {lat:.{d}f} {h:.{d}f} inf\n")

    monkeypatch.setattr(accuracy.crs_mod, "describe", lambda *a, **k: src)
    monkeypatch.setattr(accuracy.crs_mod, "same_crs", lambda *a, **k: False)
    monkeypatch.setattr(accuracy.crs_mod, "lonlat_bbox", lambda *a, **k: (115.8, -32.0, 115.9, -31.9))
    monkeypatch.setattr(accuracy.crs_mod, "select_operation", lambda *a, **k: object())
    monkeypatch.setattr(accuracy.crs_mod, "pdal_coord_op", lambda *a, **k: "+proj=noop")
    monkeypatch.setattr(accuracy, "run", fake_cct)
    moved, _ = accuracy.transform_points(job, [{"x": "391000", "y": "6464000", "z": "12.345"}], "EPSG:7850+5711")
    assert moved[0][0] == pytest.approx(lon, abs=1e-9) and moved[0][1] == pytest.approx(lat, abs=1e-9)  # < 0.2 mm
