"""Volume maths on exactly known rasters: a cone (πr²h/3) on a tilted plane, and step designs for cut/fill."""

from __future__ import annotations

import json
import math
from array import array
from pathlib import Path

import pytest

from argus_survey import crs as crs_mod
from argus_survey import rasters
from argus_survey.jobs import Job, JobStore
from argus_survey.models import GridSpec, SurfacesReport, SurveyError
from argus_survey.volume import cutfill, volume

pytestmark = pytest.mark.tools

E0, N0 = 391000.0, 6464000.0
SX, SY = 0.03, -0.015
CX, CY, R, H = E0 + 30, N0 + 30, 8.0, 4.0
CONE = math.pi * R * R * H / 3


def plane(x: float, y: float) -> float:
    return 20 + SX * (x - E0) + SY * (y - N0)


def cone(x: float, y: float) -> float:
    d = math.hypot(x - CX, y - CY)
    return plane(x, y) + (H * (1 - d / R) if d < R else 0.0)


def make_raster(grid: GridSpec, f, path: Path, crs: str = "EPSG:7850+5711") -> Path:  # type: ignore[no-untyped-def]
    top = grid.origin_y + grid.height * grid.res
    vals = array("f")
    for j in range(grid.height):
        y = top - (j + 0.5) * grid.res
        for i in range(grid.width):
            v = f(grid.origin_x + (i + 0.5) * grid.res, y)
            vals.append(rasters.NODATA if v is None else v)
    return rasters.write_raster(vals, grid, path, crs=crs, sink=None, step="test")


def analytic_job(root: Path, res: float, f=cone) -> Job:  # type: ignore[no-untyped-def]
    job = JobStore(root).open(f"cone-{res:g}".replace(".", "p"), create=True)
    job.sidecar.crs = crs_mod.describe("EPSG:7850+5711")
    grid = GridSpec(origin_x=E0, origin_y=N0, res=res, width=round(60 / res), height=round(60 / res))
    make_raster(grid, f, job.path("dtm.tif"))
    job.sidecar.surfaces = SurfacesReport(grid=grid)
    job.add_product("dtm", "dtm.tif", step="test")
    return job


def square(path: Path, x0: float, y0: float, x1: float, y1: float, crs: str | None = "EPSG:7850") -> Path:
    fc: dict = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "pile"},
                "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]},
            }
        ],
    }
    if crs:
        fc["crs"] = {"type": "name", "properties": {"name": crs}}
    path.write_text(json.dumps(fc))
    return path


@pytest.mark.parametrize("res", [0.25, 0.5, 1.0])
def test_cone_on_tilted_plane(tmp_path: Path, res: float) -> None:
    job = analytic_job(tmp_path, res)
    poly = square(tmp_path / "p.geojson", CX - 12.3, CY - 11.7, CX + 12.1, CY + 12.4)  # off-grid edges
    r = volume(job, poly, base="plane")
    z = r.zones[0]
    tol = 0.002 + 0.01 * res  # midpoint-rule error at the apex/rim kinks grows with the cell size
    assert z.above_m3 == pytest.approx(CONE, rel=tol), (res, z.above_m3, CONE)
    assert z.below_m3 == pytest.approx(0, abs=0.5 * res)
    assert z.base_fit_rms_m == pytest.approx(0, abs=1e-3)  # the boundary lies on the plane
    assert z.area_m2 == pytest.approx(24.4 * 24.1, rel=1e-6)  # fractional cells: exact polygon area
    assert z.void_percent == 0 and "plane" in z.base


def test_lowest_perimeter_adds_the_wedge(tmp_path: Path) -> None:
    job = analytic_job(tmp_path, 0.5)
    L = 24.0
    poly = square(tmp_path / "p.geojson", CX - L / 2, CY - L / 2, CX + L / 2, CY + L / 2)
    r = volume(job, poly, base="lowest-perimeter")
    wedge = L * L * (abs(SX) + abs(SY)) * L / 2  # mean of (plane − its lowest corner) over the square
    assert r.zones[0].above_m3 == pytest.approx(CONE + wedge, rel=0.007)


def test_surface_base_must_align(tmp_path: Path) -> None:
    job = analytic_job(tmp_path, 0.5)
    poly = square(tmp_path / "p.geojson", CX - 12, CY - 12, CX + 12, CY + 12)
    g = job.sidecar.surfaces.grid
    base = make_raster(g, plane, tmp_path / "base.tif")
    r = volume(job, poly, base=f"surface:{base}")
    assert r.zones[0].above_m3 == pytest.approx(CONE, rel=0.007)
    shifted = GridSpec(origin_x=E0 + 0.2, origin_y=N0, res=0.5, width=g.width, height=g.height)
    off = make_raster(shifted, plane, tmp_path / "off.tif")
    with pytest.raises(SurveyError, match="does not align"):
        volume(job, poly, base=f"surface:{off}")
    r2 = volume(job, poly, base=f"surface:{off}", resample=True)
    assert r2.resampled and r2.zones[0].above_m3 == pytest.approx(CONE, rel=0.01)


def test_voids_refused_unless_allowed(tmp_path: Path) -> None:
    def holed(x: float, y: float) -> float | None:
        return None if abs(x - CX) < 3 and abs(y - CY) < 3 else cone(x, y)

    job = analytic_job(tmp_path, 0.5, holed)
    poly = square(tmp_path / "p.geojson", CX - 12, CY - 12, CX + 12, CY + 12)
    with pytest.raises(SurveyError, match="no surface value"):
        volume(job, poly)
    r = volume(job, poly, allow_voids=True)
    assert r.zones[0].void_percent == pytest.approx(100 * 36 / 576, rel=1e-3)
    assert r.zones[0].above_m3 < CONE


def test_cutfill_sign_convention(tmp_path: Path) -> None:
    job = analytic_job(tmp_path, 0.5, plane)
    g = job.sidecar.surfaces.grid
    split = E0 + 30.0  # a cell boundary

    def design(x: float, y: float) -> float:
        return plane(x, y) + (1.0 if x < split else -0.5)  # west: design above ground → FILL; east: CUT

    d = make_raster(g, design, tmp_path / "design.tif")
    poly = square(tmp_path / "p.geojson", E0 + 20, N0 + 20, E0 + 40, N0 + 40)
    r = cutfill(job, d, polygon=poly)
    z = r.zones[0]
    assert z.above_m3 == pytest.approx(1.0 * 200, rel=1e-4)  # fill
    assert z.below_m3 == pytest.approx(0.5 * 200, rel=1e-4)  # cut
    assert z.net_m3 == pytest.approx(100, rel=1e-4)
    assert r.sign_convention.startswith("above = FILL")


def test_cutfill_design_extent_and_vertical_datum(tmp_path: Path) -> None:
    job = analytic_job(tmp_path, 0.5, plane)
    g = job.sidecar.surfaces.grid

    def pad(x: float, y: float) -> float | None:  # a 10 x 10 m pad 0.3 m above ground, nothing elsewhere
        return plane(x, y) + 0.3 if E0 + 10 <= x < E0 + 20 and N0 + 10 <= y < N0 + 20 else None

    no_vertical = make_raster(g, pad, tmp_path / "pad.tif", crs="EPSG:7850")
    with pytest.raises(SurveyError, match="no vertical datum"):
        cutfill(job, no_vertical)
    r = cutfill(job, no_vertical, design_vertical="EPSG:5711")
    assert sum(z.above_m3 for z in r.zones) == pytest.approx(30.0, rel=1e-4)
    assert any("stated EPSG:5711" in w for w in job.sidecar.warnings)
    with pytest.raises(SurveyError, match="not supported"):
        cutfill(job, no_vertical, design_vertical="EPSG:5703")
