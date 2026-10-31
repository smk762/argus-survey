"""DTM, DSM and CHM on one snapped grid, with voids measured honestly.

- **DTM**: ground (class 2), IDW within ``r = res·√2`` of each cell centre
  (PDAL's radius); a cell with no ground point that close stays a void.
- **DSM**: IDW of the top surface by default — first returns for LiDAR, all
  points for photogrammetry, noise (class 7) excluded. Unbiased on smooth
  surfaces such as stockpiles. ``dsm_method="max"`` gives the highest point per
  cell instead (``binmode``: a point lands in exactly one cell), which suits
  roof edges and canopy but reads high on any slope by about slope × res/2 — a
  +6 % volume bias on a 1:2 stockpile at 0.25 m — so it is never the default.
- **CHM**: ``filters.hag_nn`` per buffered tile, then the highest
  HeightAboveGround per cell (binmode).

Every surface gets a void mask (1 = void, 0 = data, 255 = outside the coverage
boundary) and a void % *of the covered area*, before and after any fill.
``--fill-voids N`` runs ``gdal raster fill-nodata`` (max N cells) and re-masks to
the coverage, so filling never extrapolates past the data.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any

import structlog

from argus_survey import grid as grid_mod
from argus_survey import rasters, tiling
from argus_survey.jobs import Job
from argus_survey.models import Bounds3D, GridSpec, SurfaceRecord, SurfacesReport, SurveyError
from argus_survey.qa import raster_srs
from argus_survey.tools import pdal_pipeline, run

log = structlog.get_logger(__name__)

STEP = "surfaces"
HAG_BUFFER = 20.0
VOID_OUTSIDE = 255


def _writer(path: Path, grid: GridSpec, output_type: str, *, dimension: str = "Z", **extra: Any) -> dict[str, Any]:
    return {
        "type": "writers.gdal",
        "filename": str(path),
        "output_type": output_type,
        "dimension": dimension,
        "data_type": "float32",
        "nodata": rasters.NODATA,
        "gdalopts": rasters.GTIFF_OPTS,
        **grid_mod.writer_options(grid),
        **extra,
    }


def _void_mask(job: Job, surface: Path, coverage: Path, out: Path) -> float:
    """Write the void mask for *surface*; return the void fraction of the covered area."""
    rasters.calc(
        out,
        f"B == 1 ? (isnodata(A) ? 1 : 0) : {VOID_OUTSIDE}",
        {"A": surface, "B": coverage},
        sink=job.sink,
        step=STEP,
        data_type="Byte",
        nodata=VOID_OUTSIDE,
    )
    s = rasters.stats(out, sink=job.sink, step=STEP)
    return s.mean or 0.0


def _coverage_on(job: Job, grid: GridSpec) -> Path:
    from argus_survey.qa import rasterize_coverage

    boundary = job.product_path("boundary", needed_by=STEP)
    return rasterize_coverage(job, boundary, grid, job.path(f"coverage_{_res_tag(grid.res)}.tif"), step=STEP)


def _res_tag(res: float) -> str:
    return f"{res:g}".replace(".", "p")


def _fill(job: Job, surface: Path, coverage: Path, max_distance: int) -> None:
    filled = surface.with_name(surface.stem + "_filled.tif")
    run(
        [
            "gdal",
            "raster",
            "fill-nodata",
            "--overwrite",
            "--max-distance",
            str(max_distance),
            "-i",
            str(surface),
            "-o",
            str(filled),
            "--co",
            "COMPRESS=DEFLATE",
            "--co",
            "TILED=YES",
        ],
        step=STEP,
        sink=job.sink,
    )
    # never let the fill reach past the data's coverage
    rasters.calc(surface, f"B == 1 ? A : {rasters.NODATA!r}", {"A": filled, "B": coverage}, sink=job.sink, step=STEP)
    filled.unlink(missing_ok=True)


def surfaces(
    job: Job,
    *,
    res: float,
    dtm: bool = True,
    dsm: bool = True,
    chm: bool = True,
    fill_voids: int | None = None,
    dsm_method: str = "idw",
    workers: int | None = None,
) -> SurfacesReport:
    job.require_crs(STEP, height=True)
    if dsm_method not in {"idw", "max"}:
        raise SurveyError("--dsm-method must be idw or max")
    if not (dtm or dsm or chm):
        raise SurveyError("nothing to do: choose at least one of --dtm/--dsm/--chm")
    ingest = job.sidecar.ingest
    assert ingest is not None
    copc = job.product_path("copc", needed_by=STEP)
    classified = job.product_path("classified", needed_by="DTM/CHM") if (dtm or chm) else None
    bounds = Bounds3D(**job.sidecar.products["copc"].meta["bounds"])
    grid = grid_mod.snap(bounds, res)
    sink = job.sink
    report = SurfacesReport(grid=grid)

    g = job.sidecar.ground
    if g is not None and g.ground_density * res * res < 1.0:
        report.resolution_warning = (
            f"--res {res} m is finer than the ground supports: {g.ground_density:.2f} ground pts/m² gives "
            f"{g.ground_density * res * res:.2f} per cell on average (ground spacing {g.ground_spacing:.2f} m); "
            "most DTM cells are interpolated, not measured"
        )
        job.warn(report.resolution_warning)

    with job.step(STEP):
        coverage = _coverage_on(job, grid)
        lidar = ingest.source_type == "lidar"
        radius = round(res * math.sqrt(2), 6)
        jobs: list[tuple[str, Path, str, str, float | None]] = []

        if dtm:
            assert classified is not None
            path = job.path("dtm.tif")
            pdal_pipeline(
                [
                    {"type": "readers.copc", "filename": str(classified)},
                    {"type": "filters.range", "limits": "Classification[2:2]"},
                    _writer(path, grid, "idw", radius=radius, window_size=0),
                ],
                step=STEP,
                sink=sink,
                stream=True,
            )
            jobs.append(("dtm", path, "idw", "ground points (class 2)", radius))

        if dsm:
            src = classified or copc
            path = job.path("dsm.tif")
            stages: list[dict[str, Any]] = [{"type": "readers.copc", "filename": str(src)}]
            if classified is not None:
                stages.append({"type": "filters.range", "limits": "Classification![7:7]"})
            if lidar:
                stages.append({"type": "filters.range", "limits": "ReturnNumber[1:1]"})
            if dsm_method == "idw":
                stages.append(_writer(path, grid, "idw", radius=radius, window_size=0))
            else:
                stages.append(_writer(path, grid, "max", binmode=True))
            pdal_pipeline(stages, step=STEP, sink=sink, stream=True)
            used = "first returns" if lidar else "all points (no reliable return numbering)"
            used += ", noise excluded" if classified else ""
            jobs.append(("dsm", path, dsm_method, used, radius if dsm_method == "idw" else None))

        if chm:
            assert classified is not None
            parts = job.path(".chm-parts")
            plan = tiling.plan(bounds, ingest.output_count, buffer=HAG_BUFFER)
            try:
                tiling.run_tiles(
                    job,
                    classified,
                    plan,
                    lambda _t: [{"type": "filters.hag_nn"}],
                    step=STEP,
                    out=None,
                    parts_dir=parts,
                    expected_count=ingest.output_count,
                    workers=workers,
                )
                path = job.path("chm.tif")
                readers = [{"type": "readers.las", "filename": str(p)} for p in sorted(parts.glob("*.laz"))]
                pdal_pipeline(
                    [
                        *readers,
                        {"type": "filters.range", "limits": "Classification![7:7]"},
                        _writer(path, grid, "max", dimension="HeightAboveGround", binmode=True),
                    ],
                    step=STEP,
                    sink=sink,
                    stream=True,
                )
                # writers.gdal can't take an SRS for an extra dimension's grid from multiple readers reliably
                rasters.set_crs(path, raster_srs(job), sink=sink, step=STEP)
            finally:
                shutil.rmtree(parts, ignore_errors=True)
            jobs.append(("chm", path, "max", "HeightAboveGround (hag_nn) of all non-noise points", None))

        for kind, path, output_type, used, r in jobs:
            void = _void_mask(job, path, coverage, job.path(f"{kind}_voids.tif"))
            after = None
            if fill_voids and kind == "dtm":
                _fill(job, path, coverage, fill_voids)
                after = _void_mask(job, path, coverage, job.path(f"{kind}_voids_after_fill.tif"))
                job.path(f"{kind}_voids_after_fill.tif").unlink(missing_ok=True)
            stats = rasters.stats(path, sink=sink, step=STEP)
            if output_type == "idw":
                method = f"IDW (power 1) of {used} within r = {r:.3f} m of each cell centre"
            else:
                method = f"highest value per cell (binned) of {used}"
            rec = SurfaceRecord(
                kind=kind,  # type: ignore[arg-type]
                method=method,
                output_type=output_type,
                radius=r,
                window_size=0,
                points_used=used,
                void_percent=round(100.0 * void, 3),
                void_percent_after_fill=round(100.0 * after, 3) if after is not None else None,
                filled_max_distance=fill_voids if after is not None else None,
                stats=stats,
            )
            report.surfaces[kind] = rec
            job.add_product(
                kind,
                path.name,
                step=STEP,
                description=f"{kind.upper()} at {res} m",
                res=res,
                grid=grid.model_dump(),
                void_percent=rec.void_percent,
            )
            job.add_product(
                f"{kind}_voids",
                f"{kind}_voids.tif",
                step=STEP,
                description=f"{kind.upper()} void mask (1 void, 0 data, 255 outside coverage)",
            )
            if rec.void_percent > 5:
                job.warn(f"{kind.upper()} has {rec.void_percent:.1f}% voids inside the coverage")

        if dtm:
            hs = job.path("hillshade.tif")
            hs.unlink(missing_ok=True)
            run(
                [
                    "gdaldem",
                    "hillshade",
                    "-compute_edges",
                    "-multidirectional",
                    "-co",
                    "COMPRESS=DEFLATE",
                    str(job.path("dtm.tif")),
                    str(hs),
                ],
                step=STEP,
                sink=sink,
            )
            job.add_product(
                "hillshade", "hillshade.tif", step=STEP, description="Multidirectional hillshade of the DTM"
            )

        job.sidecar.surfaces = report
        job.save()
    return report
