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
