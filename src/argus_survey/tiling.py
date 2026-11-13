"""Buffered tiles for the PDAL filters that can't stream (outlier, SMRF/CSF, hag_nn).

A drone survey can be hundreds of millions of points; SMRF on all of them at
once would need tens of GB. So the cloud is cut into square tiles of about
``TARGET_POINTS`` each. Every tile is read *with a buffer* (so a filter sees
the neighbourhood it needs at the tile edge), processed, and then trimmed to its
**core** with a half-open test (``X >= x0 && X < x1``): the cores partition the
plane, so every point is written exactly once. That invariant is checked — the
parts must add up to the source count — before untwine merges them into one COPC.

The outer edges reach ``EDGE`` beyond the bounds on every side: recorded bounds come from
PDAL's JSON, which keeps ~10 significant digits (±0.5 mm on an MGA northing), so a point
on the true minimum can lie just below the recorded one.
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
EDGE = 0.01  # metres past the recorded bounds; far more than their rounding, far less than any buffer


# Measured on a 102M-point drone LiDAR cloud (TERN WAACOO0001, 15 tiles of ~70 m + 27 m buffer):
# one SMRF tile peaked at 3.5 GiB RSS and untwine at 1.2 GiB; 4 workers ran ground 3.1x faster than 1.
WORKER_BYTES = 4 << 30  # per parallel tile at TARGET_POINTS, with headroom
RESERVE_BYTES = 2 << 30  # untwine, the Python process, page cache
MAX_WORKERS = 4


def memory_limit() -> int | None:
    """Bytes this process may use: the cgroup v2 limit if one is set (containers), else physical RAM."""
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw.isdigit():
            return int(raw)
    except OSError:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def _cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        return os.cpu_count() or 1


def default_workers(*, memory: int | None = None, cpus: int | None = None) -> int:
    """``SURVEY_WORKERS`` if set, else ≤ 4 tiles at once, as many as the CPUs and memory allow."""
    env = os.environ.get("SURVEY_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    n = min(MAX_WORKERS, cpus or _cpus())
    mem = memory if memory is not None else memory_limit()
    if mem is not None:
        n = min(n, (mem - RESERVE_BYTES) // WORKER_BYTES)
    return max(1, n)


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
    # the outer edges sit EDGE outside the (rounded) bounds, so the half-open cores cover every point
    minx, miny = bounds.minx - EDGE, bounds.miny - EDGE
    maxx, maxy = bounds.maxx + EDGE, bounds.maxy + EDGE
    w, h = maxx - minx, maxy - miny
    if count <= target_points:
        size = max(w, h)
    else:
        size = max(MIN_TILE, math.sqrt(w * h * target_points / count))
    nx = max(1, math.ceil(w / size))
    ny = max(1, math.ceil(h / size))
    tiles = []
    for ix in range(nx):
        for iy in range(ny):
            x0 = minx + ix * size
            y0 = miny + iy * size
            x1 = maxx if ix == nx - 1 else x0 + size
            y1 = maxy if iy == ny - 1 else y0 + size
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
