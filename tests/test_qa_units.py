"""QA helpers (pure Python)."""

from __future__ import annotations

from argus_survey.models import DimStats
from argus_survey.qa import ground_only


def _cls(counts: dict[str, int]) -> DimStats:
    n = sum(counts.values())
    return DimStats(name="Classification", count=n, minimum=0, maximum=0, average=0, stddev=0, counts=counts)


def test_ground_only_delivery() -> None:
    # Floreat f1 2022 "lidar": every point class 2
    assert ground_only(_cls({"2": 11_860_662}))
    assert ground_only(_cls({"2.000000": 900, "7": 3, "18": 1}))  # noise allowed; PDAL may print floats
    assert not ground_only(_cls({"0": 13_566_698}))  # unclassified (Floreat 2021)
    assert not ground_only(_cls({"1": 10, "2": 90}))
    assert not ground_only(_cls({"7": 5}))
    assert not ground_only(_cls({"2": 0, "1": 4}))
    assert not ground_only(None)


def test_hexbin_edge_of_an_empty_cloud() -> None:
    from argus_survey.qa import hexbin_edge

    assert hexbin_edge(0, 100.0) > 0
    assert hexbin_edge(1000, 0.0) > 0
    assert hexbin_edge(1_000_000, 10_000.0) == 0.5  # 100 pts/m² → the 0.5 m minimum


def test_horizontal_definition_of_a_compound_projjson() -> None:
    import json

    from argus_survey.qa import horizontal_definition

    horiz = {"type": "ProjectedCRS", "name": "Site grid"}
    vert = {"type": "VerticalCRS", "name": "AHD height"}
    compound = json.dumps({"type": "CompoundCRS", "name": "Site grid + AHD", "components": [horiz, vert]})
    assert json.loads(horizontal_definition(compound)) == horiz
    bound = json.dumps({"type": "CompoundCRS", "components": [{"type": "BoundCRS", "source_crs": horiz}, vert]})
    assert json.loads(horizontal_definition(bound)) == horiz
    single = json.dumps(horiz)
    assert horizontal_definition(single) == single
    at_epoch = json.dumps({"type": "CoordinateMetadata", "crs": json.loads(compound), "coordinateEpoch": 2020.5})
    assert json.loads(horizontal_definition(at_epoch)) == {
        "type": "CoordinateMetadata",
        "crs": horiz,
        "coordinateEpoch": 2020.5,
    }
    assert horizontal_definition("EPSG:7850") == "EPSG:7850"
