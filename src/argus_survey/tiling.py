"""Buffered tiles for the PDAL filters that can't stream (outlier, SMRF/CSF, hag_nn).

A drone survey can be hundreds of millions of points; SMRF on all of them at
once would need tens of GB. So the cloud is cut into square tiles of about
``TARGET_POINTS`` each. Every tile is read *with a buffer* (so a filter sees
the neighbourhood it needs at the tile edge), processed, and then trimmed to its
**core** with a half-open test (``X >= x0 && X < x1``): the cores partition the
plane, so every point is written exactly once. That invariant is checked — the
parts must add up to the source count — before untwine merges them into one COPC.
"""

from __future__ import annotations

import math
import os
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argus_survey.jobs import Job
from argus_survey.models import Bounds3D, SurveyError
from argus_survey.tools import pdal_pipeline, run, run_json

TARGET_POINTS = 8_000_000
MIN_TILE = 50.0  # metres; smaller tiles mean more buffer overhead than they save


def default_workers() -> int:
    env = os.environ.get("SURVEY_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, min(4, os.cpu_count() or 1))


@dataclass(frozen=True)
class Tile:
    ix: int
    iy: int
    core: tuple[float, float, float, float]  # minx, miny, maxx, maxy (half-open on max)
    buffer: float

    @property
    def name(self) -> str:
        return f"tile-{self.ix:03d}-{self.iy:03d}"

    def read_bounds(self) -> str:
        x0, y0, x1, y1 = self.core
        b = self.buffer
        return f"([{x0 - b!r}, {x1 + b!r}], [{y0 - b!r}, {y1 + b!r}])"

    def core_expression(self) -> str:
        x0, y0, x1, y1 = self.core
        return f"X >= {x0!r} && X < {x1!r} && Y >= {y0!r} && Y < {y1!r}"


@dataclass(frozen=True)
class TilePlan:
    tiles: list[Tile]
    size: float
    buffer: float


def plan(bounds: Bounds3D, count: int, *, buffer: float, target_points: int | None = None) -> TilePlan:
    """Square tiles of ~*target_points* (default ``TARGET_POINTS``) each over *bounds* (one tile when small)."""
    target_points = target_points or TARGET_POINTS
    w = max(bounds.maxx - bounds.minx, 1e-3)
    h = max(bounds.maxy - bounds.miny, 1e-3)
    if count <= target_points:
        size = max(w, h) + 1e-3
    else:
        size = max(MIN_TILE, math.sqrt(w * h * target_points / count))
    nx = max(1, math.ceil((w + 1e-3) / size))
    ny = max(1, math.ceil((h + 1e-3) / size))
    tiles = []
    for ix in range(nx):
        for iy in range(ny):
            x0 = bounds.minx + ix * size
            y0 = bounds.miny + iy * size
            # the last row/column reaches just past the max so the half-open test keeps the max point
            x1 = bounds.maxx + 1e-3 if ix == nx - 1 else x0 + size
            y1 = bounds.maxy + 1e-3 if iy == ny - 1 else y0 + size
            tiles.append(Tile(ix, iy, (x0, y0, x1, y1), buffer))
    return TilePlan(tiles=tiles, size=size, buffer=buffer)


MiddleStages = Callable[[Tile], list[dict[str, Any]]]


def run_tiles(
    job: Job,
    src: Path,
    tile_plan: TilePlan,
    middle: MiddleStages,
    *,
    step: str,
    out: Path | None,
    expected_count: int,
    workers: int | None = None,
    tail: list[dict[str, Any]] | None = None,
    parts_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Run ``readers.copc(buffered) → middle(tile) → core trim → [tail] → writers.las`` per tile.

    With *out*, the parts are merged by untwine into a COPC at *out* after checking they hold
    exactly *expected_count* points. With *parts_dir* (and no *out*), the checked parts are left
    there for the caller (who removes them). Returns each tile's pipeline metadata (for stats).
    """
    keep = parts_dir is not None and out is None
    parts_dir = parts_dir or job.path(f".{step}-parts")
    shutil.rmtree(parts_dir, ignore_errors=True)
    parts_dir.mkdir(parents=True)

    def one(tile: Tile) -> dict[str, Any]:
        stages: list[dict[str, Any]] = [{"type": "readers.copc", "filename": str(src), "bounds": tile.read_bounds()}]
        stages += middle(tile)
        stages.append({"type": "filters.expression", "expression": tile.core_expression()})
        stages += tail or []
        if out is not None or keep:
            stages.append(
                {
                    "type": "writers.las",
                    "filename": str(parts_dir / f"{tile.name}.laz"),
                    "compression": True,
                    "forward": "all",
                    "extra_dims": "all",
                }
            )
        return pdal_pipeline(stages, step=step, sink=job.sink)

    try:
        with ThreadPoolExecutor(max_workers=workers or default_workers()) as pool:
            metas = list(pool.map(one, tile_plan.tiles))
        if out is not None or keep:
            parts = sorted(parts_dir.glob("*.laz"))
            total = 0
            for part in parts:
                header = run_json(["pdal", "info", "--summary", str(part)], step=step, sink=job.sink)
                total += int(header["summary"]["num_points"])
            if total != expected_count:
                raise SurveyError(f"{step}: tiles hold {total} points but the source has {expected_count}")
        if out is not None:
            out.unlink(missing_ok=True)
            tmp = job.path(f".{step}-untwine")
            shutil.rmtree(tmp, ignore_errors=True)
            run(["untwine", "-i", str(parts_dir), "-o", str(out), "--temp_dir", str(tmp)], step=step, sink=job.sink)
            shutil.rmtree(tmp, ignore_errors=True)
    except BaseException:
        shutil.rmtree(parts_dir, ignore_errors=True)
        raise
    if not keep:
        shutil.rmtree(parts_dir, ignore_errors=True)
    return metas
