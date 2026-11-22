from __future__ import annotations

from pathlib import Path

import pytest

from argus_survey import crs
from argus_survey.models import CommandRecord, SurveyError

DATA = Path(__file__).parent / "data"


def test_parse_projinfo_three_candidates_in_order() -> None:
    ops = crs.parse_projinfo_operations((DATA / "projinfo_gda94_gda2020.txt").read_text())
    assert [o.accuracy_m for o in ops] == [0.01, 0.05, 0.05]
    assert "GDA94 to GDA2020 (1)" in ops[0].name and ops[0].grids == []
    assert ops[1].grids == ["au_icsm_GDA94_GDA2020_conformal.tif"]
    # a +grids= value wrapped onto its own line by projinfo is still found
    assert ops[2].grids == ["au_icsm_GDA94_GDA2020_conformal_and_distortion.tif"]
    assert ops[0].proj.startswith("+proj=pipeline +step +inv +proj=utm")
    assert "\n" not in ops[0].proj and "+ry=-0.0327221" in ops[0].proj


def test_parse_optional_grid_marker_is_treated_as_required() -> None:
    text = (
        "Operation No. 1:\n\nunknown id, X to Y, unknown accuracy, World\n\nPROJ string:\n"
        "+proj=pipeline +step +proj=vgridshift +grids=@a.tif,@b.tif\n"
    )
    (op,) = crs.parse_projinfo_operations(text)
    assert op.grids == ["a.tif", "b.tif"] and op.accuracy_m is None


def test_parse_empty() -> None:
    assert crs.parse_projinfo_operations("Candidate operations found: 0\n") == []


@pytest.mark.tools
def test_select_gda2020_to_mga50_ahd_uses_ausgeoid2020() -> None:
    records: list[CommandRecord] = []
    op = crs.select_operation("EPSG:7843", "EPSG:7850+5711", sink=records.append)
    assert op.grids == ["au_ga_AUSGeoid2020_20180201.tif"]
    assert "vgridshift" in op.proj and op.accuracy_m == 0.15
    assert records and records[0].tool == "projinfo"


@pytest.mark.tools
def test_select_can_pin_the_icsm_distortion_grid() -> None:
    op = crs.select_operation("EPSG:28350", "EPSG:7850", bbox_lonlat=(115.7, -32.1, 116.0, -31.8), prefer="(2)")
    assert op.chosen_by == "user"
    assert op.grids == ["au_icsm_GDA94_GDA2020_conformal_and_distortion.tif"]
    assert len(op.candidates) >= 2


@pytest.mark.tools
def test_select_refuses_when_only_grid_operations_lack_grids() -> None:
    # NZGD2000 -> NZVD2016 needs the NZ geoid, which the image deliberately does not ship.
    with pytest.raises(SurveyError, match="refusing"):
        crs.select_operation("EPSG:4959", "EPSG:2193+7839")


@pytest.mark.tools
def test_select_unknown_preference() -> None:
    with pytest.raises(SurveyError, match="matches"):
        crs.select_operation("EPSG:7843", "EPSG:7850+5711", prefer="no such op")


def test_pinned_substring_takes_the_first_match_whose_grids_are_on_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    text = "".join(
        f"Operation No. {n}:\n\nunknown id, A to B ({n}), 0.05 m, World\n\nPROJ string:\n"
        f"+proj=pipeline +step +proj=hgridshift +grids={grid}\n\n"
        for n, grid in ((1, "absent.tif"), (2, "present.tif"))
    )
    monkeypatch.setattr(crs, "run", lambda *a, **k: SimpleNamespace(stdout=text))
    monkeypatch.setattr(crs, "find_grid", lambda g: Path(g) if g == "present.tif" else None)
    op = crs.select_operation("EPSG:1", "EPSG:2", prefer="a to b")  # matches both; the first lacks its grid
    assert op.chosen_by == "user" and op.grids == ["present.tif"]
    with pytest.raises(SurveyError, match="'A to B \\(1\\)' needs grid"):  # only an unusable match: refused
        crs.select_operation("EPSG:1", "EPSG:2", prefer="(1)")
    # an operation named in full is pinned: refused when its grid is absent, never swapped for another match
    with pytest.raises(SurveyError, match="'A to B \\(1\\)' needs grid"):
        crs.select_operation("EPSG:1", "EPSG:2", prefer="A to B (1)")


def _two_ops(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    from types import SimpleNamespace

    text = "".join(
        f"Operation No. {n}:\n\nunknown id, A to B ({n}), 0.05 m, World\n\nPROJ string:\n"
        f"+proj=pipeline +step +proj=hgridshift +grids=g{n}.tif\n\n"
        for n in (1, 2)
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(crs, "run", lambda argv, **k: calls.append(list(argv)) or SimpleNamespace(stdout=text))
    monkeypatch.setattr(crs, "find_grid", lambda g: Path(g))
    return calls


def test_candidates_must_cover_the_whole_extent(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a bbox, projinfo is asked for operations whose area of use contains it, not ones that only overlap."""
    calls = _two_ops(monkeypatch)
    crs.select_operation("EPSG:1", "EPSG:2", bbox_lonlat=(115.8, -32.0, 115.9, -31.9))
    argv = calls[0]
    assert argv[argv.index("--spatial-test") + 1] == "contains" and "intersects" not in argv
    assert argv[argv.index("--bbox") + 1] == "115.800000,-32.000000,115.900000,-31.900000"
    # without an extent the test is stated too: projinfo's default (contains, against the CRSs' own areas)
    # drops operations narrower than their CRS, such as a regional geoid
    crs.select_operation("EPSG:1", "EPSG:2")
    argv = calls[1]
    assert argv[argv.index("--spatial-test") + 1] == "intersects" and "--bbox" not in argv


def test_a_pin_with_surrounding_whitespace_still_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_ops(monkeypatch)
    assert crs.select_operation("EPSG:1", "EPSG:2", prefer=" A to B (2) ").name == "A to B (2)"
    assert crs.select_operation("EPSG:1", "EPSG:2", prefer="(2)\n").name == "A to B (2)"


def test_lonlat_bbox_skips_points_gdaltransform_could_not_transform(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from argus_survey.models import Bounds3D

    out = "115.8 -32.0\ntransformation failed.\n115.9 -31.9\ninf inf\n"
    monkeypatch.setattr(crs, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    b = Bounds3D(minx=0, miny=0, minz=0, maxx=10, maxy=10, maxz=1)
    assert crs.lonlat_bbox("EPSG:7850", b) == (115.8, -32.0, 115.9, -31.9)
    monkeypatch.setattr(crs, "run", lambda *a, **k: SimpleNamespace(stdout="transformation failed.\n" * 9))
    with pytest.raises(SurveyError, match="could not place the data's extent"):
        crs.lonlat_bbox("EPSG:7850", b)
