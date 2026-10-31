"""Pure-Python parts of ingest: stats parsing/merging, source type, geoid detection."""

from __future__ import annotations

import math

import pytest

from argus_survey.ingest import detect_source_type, geoid_grids, merge_stats, parse_stats
from argus_survey.models import DimStats, TransformOp


def test_parse_stats_with_counts() -> None:
    meta = {
        "stages": {
            "filters.stats": {
                "statistic": [
                    {
                        "name": "Classification",
                        "count": 5,
                        "minimum": 1,
                        "maximum": 2,
                        "average": 1.4,
                        "stddev": 0.49,
                        "counts": ["1.000000/3", "2.000000/2"],
                    },
                ]
            }
        }
    }
    (s,) = parse_stats(meta)
    assert s.counts == {"1": 3, "2": 2} and s.count == 5


def test_merge_stats_pooled_exactly() -> None:
    a = [1.0, 2.0, 3.0]
    b = [10.0, 20.0]

    def stat(xs: list[float]) -> DimStats:
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
        return DimStats(
            name="Z", count=len(xs), minimum=min(xs), maximum=max(xs), average=m, stddev=sd, counts={"1": len(xs)}
        )

    (m,) = merge_stats([[stat(a)], [stat(b)]])
    allx = a + b
    mean = sum(allx) / len(allx)
    sd = math.sqrt(sum((x - mean) ** 2 for x in allx) / len(allx))
    assert m.count == 5 and m.minimum == 1 and m.maximum == 20
    assert m.average == pytest.approx(mean) and m.stddev == pytest.approx(sd)
    assert m.counts == {"1": 5}


def _s(name: str, mx: float) -> DimStats:
    return DimStats(name=name, count=1, minimum=0, maximum=mx, average=0, stddev=0)


def test_source_type() -> None:
    assert detect_source_type([_s("ReturnNumber", 0)], [])[0] == "photogrammetry"
    assert detect_source_type([_s("ReturnNumber", 2), _s("NumberOfReturns", 3)], [])[0] == "lidar"
    assert detect_source_type([_s("ReturnNumber", 1), _s("NumberOfReturns", 1)], ["Red", "Green", "Blue"])[0] == (
        "photogrammetry"
    )
    assert detect_source_type([_s("ReturnNumber", 1), _s("NumberOfReturns", 1)], [])[0] == "unknown"


def test_geoid_grids_only_from_vgridshift() -> None:
    op = TransformOp(
        source="a",
        target="b",
        name="x",
        accuracy_m=0.1,
        proj="+proj=pipeline +step +proj=hgridshift +grids=h.tif +step +inv +proj=vgridshift +grids=@g.tif +multiplier=1",
    )
    assert geoid_grids(op) == ["g.tif"]
