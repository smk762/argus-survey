"""Volumes between two surfaces on one grid: stockpiles (surface vs a base) and cut/fill (design vs surface).

The engine (:func:`difference_volumes`) is shared with ``change``:

1. Both surfaces are cut to the same window of the job grid — cell for cell, never
   resampled unless the user asked for it (``--resample``, recorded).
2. One ``gdal raster calc`` writes six bands: volume above, volume below, area above,
   area below, void, and 1 (area) — a cell is *void* when either surface has no value.
3. ``gdal raster zonal-stats --pixels fractional`` sums each band over each polygon,
   weighting boundary cells by the fraction inside it. (The usual cell-centre rule
   miscounts a polygon's edge cells; on a coarse grid that is tens of percent.)

Sign conventions are stated in every report:

- stockpile: *above* = surface above base = the stockpile volume; *below* = surface below base.
- cut/fill: *fill* = design above surface (material to bring in); *cut* = surface above design.
- change: *gain* = later above earlier; *loss* = later below earlier (beyond the LoD).

Bases for a stockpile: ``plane`` (least-squares plane through the surface heights sampled
along the polygon boundary — its RMS residual is reported as an indicator of how well a
plane fits the toe), ``lowest-perimeter`` (horizontal plane at the lowest boundary height),
or ``surface:<tif>`` (another grid, which must align exactly).
"""

from __future__ import annotations

import csv
import io
import math
import shutil
from array import array
from pathlib import Path
from typing import Any

import structlog

from argus_survey import crs as crs_mod
from argus_survey import grid as grid_mod
from argus_survey import rasters
from argus_survey.jobs import Job
from argus_survey.models import CrsRecord, GridSpec, SurveyError, VolumeReport, ZoneVolume
from argus_survey.qa import raster_srs
from argus_survey.tools import CommandSink, run, run_json
from argus_survey.vectors import densify, read_polygons, wkt_bbox, wkt_rings, write_zones

log = structlog.get_logger(__name__)

STEP = "volume"
MAX_VOID_PERCENT = 5.0
METHOD = (
    "per-cell (upper − lower) × cell area, summed separately for positive and negative cells; boundary cells "
    "weighted by the fraction inside the polygon (gdal raster zonal-stats --pixels fractional)"
)


# ── engine ────────────────────────────────────────────────────────────────────


def difference_volumes(
    upper: Path,
    lower: Path,
    zones_gpkg: Path,
    grid: GridSpec,
    work: Path,
    *,
    lod: float = 0.0,
    sink: CommandSink | None,
    step: str,
) -> dict[str, dict[str, float]]:
    """Per zone name: above/below volumes (m³), above/below/void/total areas (m²) between two aligned rasters.

    The output's nodata is NaN, which no band ever produces: ``isnodata()`` in ``gdal raster calc``
    only works when the output declares a nodata value (with ``--nodata none`` it is always false —
    voids would silently count as zero volume).
    """
    void = "isnodata(U) || isnodata(L)"
    d = "(U - L)"
    gain = f"{d} >= {lod!r}" if lod > 0 else f"{d} > 0"
    loss = f"{d} <= {-lod!r}" if lod > 0 else f"{d} < 0"
    bands = work / "difference_bands.tif"
    argv = [
        "gdal",
        "raster",
        "calc",
        "--overwrite",
        "--ot",
        "Float64",
        "--nodata",
        "nan",
        "-o",
        str(bands),
        "-i",
        f"U={upper}",
        "-i",
        f"L={lower}",
    ]
    for expr in (
        f"{void} ? 0 : ({gain} ? {d} : 0)",
        f"{void} ? 0 : ({loss} ? -{d} : 0)",
        f"{void} ? 0 : ({gain} ? 1 : 0)",
        f"{void} ? 0 : ({loss} ? 1 : 0)",
        f"{void} ? 1 : 0",
        "isnodata(U) ? 1 : 1",  # 1 everywhere (GDAL rejects an expression naming no input; U − U is NaN on voids)
    ):
        argv += ["--calc", expr]
    run(argv, step=step, sink=sink, env=rasters.NO_PAM)
    out = run(
        [
            "gdal",
            "raster",
            "zonal-stats",
            "-i",
            str(bands),
            "--zones",
            str(zones_gpkg),
            "--stat",
            "sum",
            "--pixels",
            "fractional",
            "--include-field",
            "name",
            "-f",
            "CSV",
            "-o",
            "/vsistdout/",
        ],
        step=step,
        sink=sink,
    ).stdout
    a = grid.cell_area
    result: dict[str, dict[str, float]] = {}
    for row in csv.DictReader(io.StringIO(out)):
        s = [float(row.get(f"sum_band_{i}") or 0.0) for i in range(1, 7)]
        name = row["name"]
        prev = result.get(name)
        vals = {
            "above_m3": s[0] * a,
            "below_m3": s[1] * a,
            "area_above_m2": s[2] * a,
            "area_below_m2": s[3] * a,
            "void_m2": s[4] * a,
            "area_m2": s[5] * a,
        }
        if prev:  # several features sharing a name are one zone
            vals = {k: prev[k] + v for k, v in vals.items()}
        result[name] = vals
    bands.unlink(missing_ok=True)
    return result


def zone_volume(name: str, v: dict[str, float], base: str, **extra: Any) -> ZoneVolume:
    area = v["area_m2"]
    return ZoneVolume(
        name=name,
        area_m2=round(area, 3),
        above_m3=round(v["above_m3"], 3),
        below_m3=round(v["below_m3"], 3),
        net_m3=round(v["above_m3"] - v["below_m3"], 3),
        area_above_m2=round(v["area_above_m2"], 3),
        area_below_m2=round(v["area_below_m2"], 3),
        void_percent=round(100.0 * v["void_m2"] / area, 3) if area > 0 else 100.0,
        base=base,
        **extra,
    )


# ── plane fitting (pure Python; 3x3 normal equations on centred coordinates) ──


def fit_plane(points: list[tuple[float, float, float]]) -> tuple[float, float, float, float, float, float]:
    """Least-squares ``z = a + b (x - x̄) + c (y - ȳ)``. Returns ``(a, b, c, x̄, ȳ, rms)``."""
    n = len(points)
    if n < 3:
        raise SurveyError("a plane base needs at least 3 boundary heights")
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sxy = syy = sz = sxz = syz = 0.0
    for x, y, z in points:
        dx, dy = x - mx, y - my
        sxx += dx * dx
        sxy += dx * dy
        syy += dy * dy
        sz += z
        sxz += dx * z
        syz += dy * z
    a = sz / n  # centred: the intercept decouples
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-12 * max(sxx * syy, 1e-30):
        raise SurveyError("the boundary is (nearly) a straight line; a plane through it is undefined")
    b = (sxz * syy - syz * sxy) / det
    c = (syz * sxx - sxz * sxy) / det
    rms = math.sqrt(sum((z - (a + b * (x - mx) + c * (y - my))) ** 2 for x, y, z in points) / n)
    return a, b, c, mx, my, rms


def plane_raster(grid: GridSpec, a: float, b: float, c: float, mx: float, my: float) -> array:
    top = grid.origin_y + grid.height * grid.res
    vals = array("f")
    for j in range(grid.height):
        y = top - (j + 0.5) * grid.res
        for i in range(grid.width):
            x = grid.origin_x + (i + 0.5) * grid.res
            vals.append(a + b * (x - mx) + c * (y - my))
    return vals


# ── helpers shared with cutfill/change ────────────────────────────────────────


def raster_crs(path: Path, *, sink: CommandSink | None, step: str) -> CrsRecord | None:
    """The CRS record of a raster, or ``None`` if it has none."""
    if not (rasters.info(path, sink=sink, step=step).get("coordinateSystem") or {}).get("wkt"):
        return None
    pj = run_json(["gdalsrsinfo", "-o", "projjson", str(path)], step=step, sink=sink)
    return crs_mod.record_from_projjson(pj)


def check_raster_crs(job: Job, path: Path, *, assume_vertical: str | None, label: str, step: str) -> str | None:
    """Refuse a raster whose CRS differs from the job's. Returns the assumption made, if any (rule 1)."""
    job_crs = job.sidecar.crs
    assert job_crs is not None
    rec = raster_crs(path, sink=job.sink, step=step)
    if rec is None:
        raise SurveyError(f"{label} {path.name} has no CRS; it must be in the job CRS ({job_crs.name})")
    same_horizontal = (
        rec.horizontal.epsg == job_crs.horizontal.epsg
        if rec.horizontal.epsg is not None and job_crs.horizontal.epsg is not None
        else rec.horizontal.name == job_crs.horizontal.name
    )
    if not same_horizontal:
        raise SurveyError(
            f"{label} is in {rec.horizontal.name}, the job in {job_crs.horizontal.name}; reproject it first "
            "(gdalwarp -t_srs) so nothing is resampled implicitly"
        )
    job_v = job_crs.vertical.epsg if job_crs.vertical else None
    if rec.vertical is None:
        if assume_vertical is None:
            raise SurveyError(
                f"{label} {path.name} declares no vertical datum; heights can't be compared with the job's "
                f"{job_crs.vertical.name if job_crs.vertical else 'heights'} unless you state it "
                "(e.g. --design-vertical EPSG:5711)"
            )
        code = assume_vertical.upper().removeprefix("EPSG:")
        if not code.isdigit() or int(code) != job_v:
            raise SurveyError(
                f"{label} heights are stated as {assume_vertical} but the job is in EPSG:{job_v}; vertical "
                "transformation of rasters is not supported in v0"
            )
        return f"{label} {path.name} has no vertical datum; the user stated EPSG:{code}"
    if rec.vertical.epsg != job_v or rec.height_reference != job_crs.height_reference:
        raise SurveyError(
            f"{label} heights are {rec.vertical.name}, the job's {job_crs.vertical.name if job_crs.vertical else '?'}"
        )
    return None


def align_raster(
    job: Job, path: Path, grid: GridSpec, out: Path, *, resample: bool, label: str, step: str
) -> tuple[Path, str | None]:
    """*path* on exactly *grid*. Returns the path and, if it had to be resampled, how.

    An aligned grid is copied cell for cell (``gdalwarp -r near`` onto identical cell centres is an
    exact copy, and unlike ``gdal_translate -projwin`` it writes nodata — not 0 — beyond the source).
    """
    other = rasters.grid_of(path, sink=job.sink, step=step)
    is_aligned = grid_mod.aligned(other, grid)
    if not is_aligned and not resample:
        raise SurveyError(
            f"{label} grid (res {other.res} m, origin {other.origin_x}, {other.origin_y}) does not align with the "
            f"job's ({grid.res} m, origin {grid.origin_x}, {grid.origin_y}); refusing to resample silently — "
            "pass --resample to warp it (bilinear, recorded)"
        )
    run(
        [
            "gdalwarp",
            "-q",
            "-overwrite",
            "-r",
            "near" if is_aligned else "bilinear",
            *grid_mod.gdal_te_tr(grid),
            "-dstnodata",
            repr(rasters.NODATA),
            "-ot",
            "Float32",
            "-co",
            "COMPRESS=DEFLATE",
            str(path),
            str(out),
        ],
        step=step,
        sink=job.sink,
    )
    # the CRS was checked (or its vertical datum stated) by check_raster_crs: carry the job's full CRS
    rasters.set_crs(out, raster_srs(job), sink=job.sink, step=step)
    if is_aligned:
        return out, None
    how = f"{label} warped bilinear from res {other.res} m / origin ({other.origin_x}, {other.origin_y}) onto the job grid"
    job.warn(how)
    return out, how


# ── stockpile volume ─────────────────────────────────────────────────────────


def volume(
    job: Job,
    polygon: Path,
    *,
    base: str = "plane",
    surface: str = "dtm",
    polygon_srs: str | None = None,
    allow_voids: bool = False,
    resample: bool = False,
    base_vertical: str | None = None,
) -> VolumeReport:
    job.require_crs(STEP, height=True)
    if job.sidecar.surfaces is None:
        raise SurveyError("volume needs surfaces — run `argus-survey surfaces` first")
    src = job.product_path(surface, needed_by=STEP)
    grid = job.sidecar.surfaces.grid
    srs = raster_srs(job)
    sink = job.sink
    zones = read_polygons(polygon, srs, src_srs=polygon_srs, sink=sink, step=STEP)
    base_kind, _, base_arg = base.partition(":")
    if base_kind not in {"plane", "lowest-perimeter", "surface"} or (base_kind == "surface") != bool(base_arg):
        raise SurveyError("--base must be plane, lowest-perimeter or surface:<raster>")

    work = job.path(".volume-work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    results: list[ZoneVolume] = []
    resampled: str | None = None
    try:
        with job.step(STEP):
            other_full: Path | None = None
            if base_kind == "surface":
                other = Path(base_arg)
                assumption = check_raster_crs(
                    job, other, assume_vertical=base_vertical, label="base surface", step=STEP
                )
                if assumption:
                    job.warn(assumption)
                other_full, resampled = align_raster(
                    job, other, grid, work / "base_full.tif", resample=resample, label="base surface", step=STEP
                )
            for k, (name, wkt) in enumerate(zones):
                bbox = wkt_bbox(wkt)
                upper = work / f"upper_{k}.tif"
                sub = rasters.cut_window(src, upper, grid, bbox, sink=sink, step=STEP)
                extra: dict[str, Any] = {}
                lower = work / f"lower_{k}.tif"
                if base_kind == "surface":
                    assert other_full is not None
                    rasters.cut_window(other_full, lower, grid, bbox, sink=sink, step=STEP)
                    desc = f"surface {Path(base_arg).name}"
                else:
                    ring_pts = [pt for ring in wkt_rings(wkt) for pt in densify(ring, grid.res)]
                    samples = rasters.sample(
                        upper, [(str(i), x, y) for i, (_, x, y) in enumerate(ring_pts)], crs=srs, sink=sink, step=STEP
                    )
                    pts = [
                        (x, y, samples[str(i)]) for i, (_, x, y) in enumerate(ring_pts) if samples[str(i)] is not None
                    ]
                    missing = len(ring_pts) - len(pts)
                    if len(pts) < 3:
                        raise SurveyError(f"{name}: the surface has no heights along the polygon boundary")
                    if missing / len(ring_pts) > 0.1:
                        job.warn(f"{name}: {missing} of {len(ring_pts)} boundary samples fell in voids")
                    zs = [p[2] for p in pts]
                    if base_kind == "plane":
                        a, b, c, mx, my, rms = fit_plane(pts)  # type: ignore[arg-type]
                        desc = (
                            f"least-squares plane through {len(pts)} boundary heights: "
                            f"z = {a:.3f} + {b:.5f}·(E − {mx:.3f}) + {c:.5f}·(N − {my:.3f})"
                        )
                        extra = {"base_fit_rms_m": round(rms, 4), "boundary_samples": len(pts)}
                    else:
                        lowest = min(zs)
                        a, b, c, mx, my = lowest, 0.0, 0.0, 0.0, 0.0
                        desc = f"horizontal plane at the lowest boundary height, {lowest:.3f} m"
                        extra = {"boundary_samples": len(pts)}
                    rasters.write_raster(plane_raster(sub, a, b, c, mx, my), sub, lower, crs=srs, sink=sink, step=STEP)
                zfile = write_zones([(name, wkt)], work / f"zone_{k}.gpkg", srs, sink=sink, step=STEP)
                v = difference_volumes(upper, lower, zfile, sub, work, sink=sink, step=STEP)[name]
                zv = zone_volume(name, v, desc, **extra)
                if zv.void_percent > MAX_VOID_PERCENT and not allow_voids:
                    raise SurveyError(
                        f"{name}: {zv.void_percent:.1f}% of the polygon has no surface value (limit "
                        f"{MAX_VOID_PERCENT}%); fill voids (surfaces --fill-voids) or pass --allow-voids, which "
                        "counts voids as zero volume"
                    )
                results.append(zv)

            report = VolumeReport(
                kind="stockpile",
                surface=f"{surface.upper()} (upper) vs base (lower)",
                sign_convention="above = surface above base (stockpile volume); below = surface below base",
                res=grid.res,
                surface_void_percent={surface: job.sidecar.surfaces.surfaces[surface].void_percent}
                if surface in job.sidecar.surfaces.surfaces
                else {},
                method=METHOD,
                zones=results,
                resampled=resampled,
                allow_voids=allow_voids,
            )
            _store(job, report, "volume.json", "volume", f"Stockpile volumes vs {base_kind} base")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report


def _store(job: Job, report: VolumeReport, name: str, kind: str, description: str) -> None:
    job.path(name).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    job.sidecar.volumes.append(report)
    job.add_product(kind, name, step=STEP if kind == "volume" else kind, description=description)


# ── cut/fill against a design surface ────────────────────────────────────────


def cutfill(
    job: Job,
    design: Path,
    *,
    polygon: Path | None = None,
    polygon_srs: str | None = None,
    surface: str = "dtm",
    design_vertical: str | None = None,
    resample: bool = False,
    allow_voids: bool = False,
) -> VolumeReport:
    step = "cutfill"
    job.require_crs(step, height=True)
    if job.sidecar.surfaces is None:
        raise SurveyError("cutfill needs surfaces — run `argus-survey surfaces` first")
    src = job.product_path(surface, needed_by=step)
    grid = job.sidecar.surfaces.grid
    srs = raster_srs(job)
    sink = job.sink
    work = job.path(".cutfill-work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        with job.step(step):
            assumption = check_raster_crs(job, design, assume_vertical=design_vertical, label="design", step=step)
            if assumption:
                job.warn(assumption)
            design_on_grid, resampled = align_raster(
                job, design, grid, work / "design.tif", resample=resample, label="design", step=step
            )
            if polygon is not None:
                zones = read_polygons(polygon, srs, src_srs=polygon_srs, sink=sink, step=step)
            else:
                zones = [("design extent", w) for w in _footprint(design_on_grid, work, srs, sink=sink, step=step)]
            zfile = write_zones(zones, work / "zones.gpkg", srs, sink=sink, step=step)
            vols = difference_volumes(design_on_grid, src, zfile, grid, work, sink=sink, step=step)
            results = [zone_volume(n, v, f"{surface.upper()} (existing surface)") for n, v in vols.items()]
            for zv in results:
                if zv.void_percent > MAX_VOID_PERCENT and not allow_voids:
                    raise SurveyError(
                        f"{zv.name}: {zv.void_percent:.1f}% of the area lacks a design or surface value (limit "
                        f"{MAX_VOID_PERCENT}%); pass --allow-voids to count voids as zero"
                    )
            report = VolumeReport(
                kind="cutfill",
                surface=f"design {design.name} (upper) vs {surface.upper()} (lower)",
                sign_convention="above = FILL (design above existing surface: material to bring in); "
                "below = CUT (existing surface above design: material to remove); net = fill − cut",
                res=grid.res,
                surface_void_percent={surface: job.sidecar.surfaces.surfaces[surface].void_percent}
                if surface in job.sidecar.surfaces.surfaces
                else {},
                method=METHOD,
                zones=results,
                resampled=resampled,
                allow_voids=allow_voids,
            )
            design_out = job.path("design_on_grid.tif")
            shutil.copyfile(design_on_grid, design_out)
            job.add_product("design", design_out.name, step=step, description=f"{design.name} on the job grid")
            _store(job, report, "cutfill.json", "cutfill", f"Cut/fill against {design.name}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report


def _footprint(raster: Path, work: Path, srs: str, *, sink: CommandSink | None, step: str) -> list[str]:
    """Polygons (WKT) of a raster's valid cells."""
    mask = rasters.calc(
        work / "valid.tif", "isnodata(A) ? 0 : 1", {"A": raster}, sink=sink, step=step, data_type="Byte", nodata=0
    )
    poly = work / "valid.csv"
    run(
        [
            "gdal",
            "raster",
            "polygonize",
            "-i",
            str(mask),
            "-o",
            str(poly),
            "--of",
            "CSV",
            "--lco",
            "GEOMETRY=AS_WKT",
            "--overwrite",
        ],
        step=step,
        sink=sink,
    )
    with poly.open(encoding="utf-8") as fh:
        wkts = [r["WKT"] for r in csv.DictReader(fh) if r.get("WKT")]
    if not wkts:
        raise SurveyError("the design has no valid cells on the job grid")
    return wkts
