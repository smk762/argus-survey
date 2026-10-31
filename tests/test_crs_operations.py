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
