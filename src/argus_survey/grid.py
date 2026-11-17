"""Snapped raster grids.

Every raster the package writes sits on a grid whose origin is a multiple of its
resolution (``floor(min / res) * res``). Two surveys of the same site at the same
resolution therefore share cell boundaries exactly, so a DEM of difference or a
cut/fill never needs resampling (SPEC §6.4, §6.6).
"""

from __future__ import annotations

import math

from argus_survey.models import Bounds3D, GridSpec, SurveyError

_EPS = 1e-9


def snap(bounds: Bounds3D | tuple[float, float, float, float], res: float) -> GridSpec:
    """The smallest snapped grid at *res* that contains *bounds* (a point on the max edge gets its own cell).

    The origin is never greater than the minimum: *_EPS* absorbs float noise when the minimum sits
    on a grid line (``0.3 / 0.1 == 2.9999999999999996``), but a minimum genuinely just below a line
    gets the cell below it. Either way the origin is a multiple of *res*, so epochs share cells.
    """
    if res <= 0 or not math.isfinite(res):
        raise SurveyError(f"resolution must be a positive number of metres, got {res}")
    if isinstance(bounds, Bounds3D):
        minx, miny, maxx, maxy = bounds.minx, bounds.miny, bounds.maxx, bounds.maxy
    else:
        minx, miny, maxx, maxy = bounds
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)) or maxx < minx or maxy < miny:
        raise SurveyError(
            f"cannot grid the bounds ({minx}, {miny}) - ({maxx}, {maxy}): the input has no points or degenerate bounds"
        )
    # round the origin to the resolution's decimal precision so 0.1-m grids don't drift by 1e-12
    digits = max(0, -math.floor(math.log10(res)) + 6)
    ox = _origin(minx, res, digits)
    oy = _origin(miny, res, digits)
    width = math.floor((maxx - ox) / res + _EPS) + 1
    height = math.floor((maxy - oy) / res + _EPS) + 1
    return GridSpec(origin_x=ox, origin_y=oy, res=res, width=width, height=height)


def _origin(lo: float, res: float, digits: int) -> float:
    """The snapped origin at or below *lo*."""
    k = math.floor(lo / res + _EPS)
    origin = round(k * res, digits)
    if origin > lo:  # _EPS (or the rounding) lifted it past a minimum that is not on the grid line
        origin = round((k - 1) * res, digits)
    return origin


def same_grid(a: GridSpec, b: GridSpec, tol: float = 1e-6) -> bool:
    """Identical cell lattice and extent."""
    return (
        abs(a.res - b.res) < tol
        and abs(a.origin_x - b.origin_x) < tol
        and abs(a.origin_y - b.origin_y) < tol
        and a.width == b.width
        and a.height == b.height
    )


def aligned(a: GridSpec, b: GridSpec, tol: float = 1e-6) -> bool:
    """Same resolution and cell boundaries (extents may differ), both to within *tol* ground units."""
    if abs(a.res - b.res) >= tol:
        return False
    dx = (a.origin_x - b.origin_x) / a.res
    dy = (a.origin_y - b.origin_y) / a.res
    # the origins' offset from a whole number of cells, in ground units (as same_grid compares)
    return abs(dx - round(dx)) * a.res < tol and abs(dy - round(dy)) * a.res < tol


def writer_options(grid: GridSpec) -> dict[str, float | int]:
    """The writers.gdal options that pin a raster to *grid*."""
    return {
        "resolution": grid.res,
        "origin_x": grid.origin_x,
        "origin_y": grid.origin_y,
        "width": grid.width,
        "height": grid.height,
    }


def gdal_te_tr(grid: GridSpec) -> list[str]:
    """``-te xmin ymin xmax ymax -tr res res`` for gdal_rasterize/gdalwarp onto *grid*."""
    xmin, ymin, xmax, ymax = grid.extent
    return ["-te", *(repr(v) for v in (xmin, ymin, xmax, ymax)), "-tr", repr(grid.res), repr(grid.res)]
