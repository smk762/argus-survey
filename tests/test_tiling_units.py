"""Tile plans (pure Python): the half-open cores must cover every point the cloud can hold."""

from __future__ import annotations

import pytest

from argus_survey import tiling
from argus_survey.models import Bounds3D


def _inside(tile: tiling.Tile, x: float, y: float) -> bool:
    x0, y0, x1, y1 = tile.core
    return x0 <= x < x1 and y0 <= y < y1


def test_rounded_bounds_keep_the_minimum_point() -> None:
    """Floreat f1 2022: PDAL recorded minx 382260.0003, but three points sit at X = 382260.00025.

    The cores started exactly at the recorded min, so ground lost them and refused the job
    ("tiles hold 11860659 points but the source has 11860662").
    """
    b = Bounds3D(minx=382260.0003, miny=6466330.406, minz=-0.738, maxx=382446.6873, maxy=6466764.875, maxz=16.086)
    for count in (11_860_662, 1_000):  # many tiles, and one
        p = tiling.plan(b, count, buffer=5.0, target_points=500_000)
        for x, y in [
            (382260.00025, 6466764.4065),  # the real stragglers: below the recorded minx
            (b.minx - 0.0005, b.miny - 0.0005),  # worst case of rounding to ~10 significant digits
            (b.maxx + 0.0005, b.maxy + 0.0005),
            (b.minx, b.maxy),
        ]:
            assert sum(_inside(t, x, y) for t in p.tiles) == 1, (count, x, y)


def test_cores_partition_the_padded_extent() -> None:
    b = Bounds3D(minx=0.0, miny=0.0, minz=0.0, maxx=300.0, maxy=200.0, maxz=1.0)
    p = tiling.plan(b, 6_000_000, buffer=10.0, target_points=1_000_000)
    assert len(p.tiles) > 4
    xs = sorted({t.core[0] for t in p.tiles} | {t.core[2] for t in p.tiles})
    ys = sorted({t.core[1] for t in p.tiles} | {t.core[3] for t in p.tiles})
    assert xs[0] == pytest.approx(-tiling.EDGE) and xs[-1] == pytest.approx(300.0 + tiling.EDGE)
    assert ys[0] == pytest.approx(-tiling.EDGE) and ys[-1] == pytest.approx(200.0 + tiling.EDGE)
    # every grid sample lands in exactly one core
    for i in range(0, 301, 7):
        for j in range(0, 201, 7):
            assert sum(_inside(t, float(i), float(j)) for t in p.tiles) == 1
