"""Web views of a job's products for the browser viewer: Web Mercator PNGs and WGS 84 GeoJSON.

The survey products stay in the job CRS (MGA, PCG2020, …); a web map needs EPSG:3857
images placed by their lon/lat corners and lon/lat vectors. These are *derived views*,
cached under ``<job>/.web/`` and rebuilt when the product's SHA256 changes. Their commands are not
recorded in the sidecar (a GET must not change job.json), and they are never
deliverables and never feed a computation. Raster views are coloured with a fixed ramp
(heights by the product's own min/max; DoD diverging around 0) so they read at a glance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from argus_survey import rasters
from argus_survey.jobs import Job
from argus_survey.models import SurveyError
from argus_survey.tools import run, run_json

RASTER_KINDS = ("hillshade", "dtm", "dsm", "chm", "density", "dod", "dod_lod")
VECTOR_KINDS = ("contours_geojson", "boundary", "gaps")
TERRAIN = [
    (0.0, (43, 131, 186)),
    (0.25, (171, 221, 164)),
    (0.5, (255, 255, 191)),
    (0.75, (253, 174, 97)),
    (1.0, (215, 25, 28)),
]
WEB_MAX_PX = 2048
VIEW_VERSION = 2  # bump when the rendering changes, so cached views are rebuilt (2: transparent DoD zero)


def _cache(job: Job, kind: str, suffix: str) -> tuple[Path, Path]:
    product = job.sidecar.products.get(kind)
    if product is None:
        raise SurveyError(f"job {job.id!r} has no {kind!r} product")
    web = job.path(".web")
    web.mkdir(exist_ok=True)
    return web / f"{kind}.{product.sha256[:16]}.v{VIEW_VERSION}{suffix}", job.path(product.path)


def raster_overlay(job: Job, kind: str) -> dict[str, Any]:
    """PNG path + the four lon/lat corners MapLibre's image source wants (TL, TR, BR, BL)."""
    if kind not in RASTER_KINDS:
        raise SurveyError(f"{kind!r} has no map view (one of {', '.join(RASTER_KINDS)})")
    png, src = _cache(job, kind, ".png")
    meta_path = png.with_suffix(".json")
    if png.is_file() and meta_path.is_file():
        return json.loads(meta_path.read_text())
    work = png.with_suffix(".tmp")
    work.mkdir(exist_ok=True)
    try:
        coloured = src
        value_range: list[float] | None = None
        ramp_name = "hillshade"
        if kind != "hillshade":
            s = rasters.stats(src, sink=None, step="web")
            lo = 0.0 if s.minimum is None else s.minimum
            hi = 1.0 if s.maximum is None else s.maximum
            if kind.startswith("dod"):
                m = max(abs(lo), abs(hi), 0.1)
                # transparent at 0 so "no change" shows the map beneath; opaque at the ends
                ramp = [(-m, (33, 102, 172, 255)), (0.0, (247, 247, 247, 0)), (m, (178, 24, 43, 255))]
                value_range, ramp_name = [-m, m], "diverging"
            else:
                ramp = [(lo + f * (hi - lo), (*c, 255)) for f, c in TERRAIN]
                value_range, ramp_name = [lo, hi], "terrain"
            table = work / "ramp.txt"
            table.write_text("".join(f"{v!r} {r} {g} {b} {a}\n" for v, (r, g, b, a) in ramp) + "nv 0 0 0 0\n")
            coloured = work / "c.tif"
            run(["gdaldem", "color-relief", "-q", "-alpha", str(src), str(table), str(coloured)], step="web", sink=None)
        merc = work / "m.tif"
        run(
            [
                "gdalwarp",
                "-q",
                "-overwrite",
                "-t_srs",
                "EPSG:3857",
                "-r",
                "bilinear",
                "-dstalpha",
                "-ts",
                str(WEB_MAX_PX),
                "0",
                str(coloured),
                str(merc),
            ],
            step="web",
            sink=None,
        )
        run(["gdal_translate", "-q", "-of", "PNG", str(merc), str(png)], step="web", sink=None)
        info = run_json(["gdalinfo", "-json", str(merc)], step="web", sink=None)
        corners = info["wgs84Extent"]["coordinates"][0]  # ring: TL, BL, BR, TR, TL
        tl, bl, br, tr = corners[0], corners[1], corners[2], corners[3]
        meta = {
            "kind": kind,
            "png": png.name,
            "coordinates": [tl, tr, br, bl],
            "range": value_range,  # the values at the ramp's ends (None for hillshade)
            "ramp": ramp_name,
        }
        meta_path.write_text(json.dumps(meta))
        return meta
    finally:
        for f in work.glob("*"):
            f.unlink()
        work.rmdir()


def vector_wgs84(job: Job, kind: str) -> Path:
    """A product's GeoJSON reprojected to WGS 84 (RFC 7946) for the web map."""
    if kind not in VECTOR_KINDS:
        raise SurveyError(f"{kind!r} has no map view (one of {', '.join(VECTOR_KINDS)})")
    out, src = _cache(job, kind, ".4326.geojson")
    if not out.is_file():
        run(
            ["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-lco", "RFC7946=YES", str(out), str(src)],
            step="web",
            sink=None,
        )
    return out
