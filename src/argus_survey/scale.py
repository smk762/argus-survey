"""Grid-to-ground: the combined scale factor at the site, so grid quantities can be stated at ground.

Areas and volumes are measured in the projection's grid. Two factors relate a grid
distance to the distance on the ground:

- the **point scale factor** *k* of the projection (MGA: 0.9996 on the central meridian
  to ~1.0006 at a zone edge at WA latitudes; the LSG2020 grids such as PCG2020 keep it
  near 1 by design), from ``proj -V`` at the site centroid, and at the extent's corners
  to show how much it varies across the site;
- the **height (elevation) factor** R / (R + h), with *h* the ellipsoidal height and *R*
  the Gaussian mean radius of the ellipsoid at the site latitude. A job on AHD gets *h*
  by transforming its centroid to the 3D geographic CRS of its datum through the same
  explicitly selected PROJ operation (and geoid grid) as everything else.

combined = k × R / (R + h);  ground distance = grid distance / combined;
ground area = grid area / combined²; volumes scale like areas (heights are not scaled).

One factor for the whole site is the usual surveying approximation; the corner spread of
*k* is recorded so the user can see when it isn't good enough.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from argus_survey import crs as crs_mod
from argus_survey.models import Bounds3D, CrsRecord, ScaleFactors, SurveyError
from argus_survey.tools import CommandSink, run, run_json

_K = re.compile(r"Parallel scale \(k\)\s*:\s*([0-9.]+)")
_H = re.compile(r"Meridian scale \(h\)\s*:\s*([0-9.]+)")
_CONV = re.compile(r"Convergence\s*:.*\[\s*(-?[0-9.]+)\s*\]")


def horizontal_projjson(crs: CrsRecord, *, sink: CommandSink | None, step: str) -> dict[str, Any]:
    """The projected (horizontal) component of *crs* as PROJJSON."""
    pj = run_json(["projinfo", "-o", "PROJJSON", "-q", crs.definition], step=step, sink=sink)
    if pj.get("type") == "BoundCRS":
        pj = pj["source_crs"]
    if pj.get("type") == "CompoundCRS":
        pj = pj["components"][0]
        if pj.get("type") == "BoundCRS":  # a WKT1 TOWGS84 wrapper on the horizontal component, as crs.py unwraps
            pj = pj["source_crs"]
    if pj.get("type") != "ProjectedCRS":
        raise SurveyError(f"{crs.name} is not a projected CRS; no grid scale factor applies")
    return pj


def ellipsoid_of(projected: dict[str, Any]) -> tuple[float, float]:
    """(semi-major axis, inverse flattening) of a projected CRS's base datum."""
    base = projected["base_crs"]
    datum = base.get("datum") or base.get("datum_ensemble") or {}
    ell = datum.get("ellipsoid") or {}
    a, b = (
        v.get("value") if isinstance(v, dict) else v for v in (ell.get("semi_major_axis"), ell.get("semi_minor_axis"))
    )
    rf = ell.get("inverse_flattening")
    if rf is None and a is not None and b is not None and float(a) > float(b):
        rf = float(a) / (float(a) - float(b))  # ellipsoids defined by a and b (Clarke 1866, Airy 1830)
    if a is None or rf is None:
        raise SurveyError(f"can't read the ellipsoid of {base.get('name', '?')}")
    return float(a), float(rf)


def gaussian_radius(a: float, inverse_flattening: float, lat_deg: float) -> float:
    """√(M·N): the ellipsoid's mean radius of curvature at *lat_deg*."""
    f = 1.0 / inverse_flattening
    e2 = f * (2 - f)
    s2 = math.sin(math.radians(lat_deg)) ** 2
    w = 1 - e2 * s2
    m = a * (1 - e2) / w**1.5
    n = a / math.sqrt(w)
    return math.sqrt(m * n)


def parse_proj_verbose(text: str) -> list[tuple[float, float, float]]:
    """``proj -V`` output → [(meridian scale h, parallel scale k, convergence °)] per point."""
    ks = [float(m) for m in _K.findall(text)]
    hs = [float(m) for m in _H.findall(text)]
    cs = [float(m) for m in _CONV.findall(text)]
    if not ks or not (len(ks) == len(hs) == len(cs)):
        raise SurveyError("could not read scale factors from proj -V")
    return list(zip(hs, ks, cs, strict=True))


def _proj_args(projected: dict[str, Any], *, sink: CommandSink | None, step: str) -> list[str]:
    out = run(["projinfo", "-o", "PROJ", "-q", json.dumps(projected)], step=step, sink=sink).stdout
    return [t for t in out.split() if t != "+type=crs"]


def scale_factors(
    crs: CrsRecord, bounds: Bounds3D, mean_height: float, *, sink: CommandSink | None = None, step: str = "ingest"
) -> ScaleFactors:
    projected = horizontal_projjson(crs, sink=sink, step=step)
    args = _proj_args(projected, sink=sink, step=step)
    cx, cy = (bounds.minx + bounds.maxx) / 2, (bounds.miny + bounds.maxy) / 2
    pts = [(cx, cy)] + [(x, y) for x in (bounds.minx, bounds.maxx) for y in (bounds.miny, bounds.maxy)]
    # proj works in the projection's own easting/northing → lon/lat order, whatever the CRS's axis order
    inv = run(
        ["proj", "-I", "-f", "%.10f", *args], step=step, sink=sink, stdin="".join(f"{x!r} {y!r}\n" for x, y in pts)
    ).stdout
    lonlat = [(float(p[0]), float(p[1])) for p in (ln.split() for ln in inv.splitlines()) if len(p) >= 2]
    if len(lonlat) != len(pts):
        raise SurveyError("could not place the site on the ellipsoid (proj -I)")
    verbose = run(
        ["proj", "-V", *args], step=step, sink=sink, stdin="".join(f"{lo!r} {la!r}\n" for lo, la in lonlat)
    ).stdout
    factors = parse_proj_verbose(verbose)
    _, k, convergence = factors[0]
    corner_k = [f[1] for f in factors]
    lon, lat = lonlat[0]
    a, rf = ellipsoid_of(projected)
    radius = gaussian_radius(a, rf, lat)

    ellipsoidal: float | None = None
    separation: float | None = None
    op_name: str | None = None
    note = ""
    if crs.height_reference == "ellipsoidal":
        ellipsoidal = mean_height
    elif crs.height_reference == "gravity":
        base = projected["base_crs"].get("id") or {}
        if base.get("authority") != "EPSG":
            raise SurveyError("the base geographic CRS has no EPSG code; can't find the ellipsoidal height")
        geo3d = crs_mod.promote_3d(f"EPSG:{base['code']}", sink=sink, step=step)
        target = crs_mod.describe(geo3d, sink=sink, step=step)
        bbox = (
            min(p[0] for p in lonlat),
            min(p[1] for p in lonlat),
            max(p[0] for p in lonlat),
            max(p[1] for p in lonlat),
        )
        op = crs_mod.select_operation(crs.definition, geo3d, bbox_lonlat=bbox, sink=sink, step=step)
        pipeline = crs_mod.pdal_coord_op(op, crs, target)
        out = run(["cct", "-d", "4", *pipeline.split()], step=step, sink=sink, stdin=f"{cx!r} {cy!r} {mean_height!r}\n")
        # cct reports a failed point as a "# Record N TRANSFORMATION ERROR" line, whose third token is N
        rows = [ln.split() for ln in out.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        try:
            ellipsoidal = float(rows[0][2]) if len(rows) == 1 and len(rows[0]) >= 3 else math.nan
        except ValueError:
            ellipsoidal = math.nan
        if not math.isfinite(ellipsoidal):
            raise SurveyError("transforming the site centroid to ellipsoidal height failed")
        separation = ellipsoidal - mean_height
        op_name = op.name
    else:
        note = "; no vertical datum, so no height factor (state --assume-vertical to get one)"

    height_factor = radius / (radius + ellipsoidal) if ellipsoidal is not None else None
    combined = k * height_factor if height_factor is not None else None
    return ScaleFactors(
        easting=round(cx, 3),
        northing=round(cy, 3),
        longitude=round(lon, 8),
        latitude=round(lat, 8),
        height_m=round(mean_height, 3),
        ellipsoidal_height_m=round(ellipsoidal, 3) if ellipsoidal is not None else None,
        geoid_separation_m=round(separation, 3) if separation is not None else None,
        point_scale=round(k, 8),
        point_scale_min=round(min(corner_k), 8),
        point_scale_max=round(max(corner_k), 8),
        convergence_deg=round(convergence, 6),
        radius_m=round(radius, 1),
        height_factor=round(height_factor, 8) if height_factor is not None else None,
        combined=round(combined, 8) if combined is not None else None,
        operation=op_name,
        method=(
            "point scale k from proj -V at the extent's centroid (range over its corners); height factor "
            "R/(R+h), R the Gaussian mean radius at the centroid's latitude, h the ellipsoidal height of the "
            f"mean point height; combined = k × R/(R+h); ground = grid / combined{note}"
        ),
    )
