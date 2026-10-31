"""QA: what a surveyor checks before trusting a point cloud.

One streaming pass over the COPC gives the dimension statistics (class and
return histograms, z range), the coverage boundary (``filters.hexbin``) and a
point-count raster; outliers need neighbours, so they are counted per buffered
tile (``filters.outlier``; counted, not removed — removal is ground's job).

Products: ``qa.json``, ``density.tif`` (pts/m², nodata outside coverage),
``boundary.geojson``, ``coverage.tif`` (1 inside the boundary, on the density
grid), ``gaps.geojson`` (cells inside the boundary below the density threshold).
"""

from __future__ import annotations

import csv
import math
import tempfile
from pathlib import Path
from typing import Any

import structlog

from argus_survey import grid as grid_mod
from argus_survey import rasters, tiling
from argus_survey.ingest import parse_stats
from argus_survey.jobs import Job
from argus_survey.models import (
    Bounds3D,
    ClassCount,
    DensityStats,
    DimStats,
    GapStats,
    GridSpec,
    OutlierStats,
    QaReport,
    SurveyError,
    ValueCount,
)
from argus_survey.tools import pdal_pipeline, run

log = structlog.get_logger(__name__)

STEP = "qa"

# ASPRS LAS 1.4 R15 standard point classes.
ASPRS_CLASSES: dict[int, str] = {
    0: "Created, never classified",
    1: "Unclassified",
    2: "Ground",
    3: "Low vegetation",
    4: "Medium vegetation",
    5: "High vegetation",
    6: "Building",
    7: "Low point (noise)",
    8: "Reserved / model key-point",
    9: "Water",
    10: "Rail",
    11: "Road surface",
    12: "Reserved / overlap",
    13: "Wire – guard (shield)",
    14: "Wire – conductor (phase)",
    15: "Transmission tower",
    16: "Wire-structure connector",
    17: "Bridge deck",
    18: "High noise",
    19: "Overhead structure",
    20: "Ignored ground",
    21: "Snow",
    22: "Temporal exclusion",
}

# 3σ: at 2σ a clean scene loses ~4% of real wall and canopy-edge points to "noise".
OUTLIER_PARAMS: dict[str, Any] = {"method": "statistical", "mean_k": 8, "multiplier": 3.0}
OUTLIER_BUFFER = 5.0
GAP_FRACTION_OF_MEDIAN = 0.25
HEX_THRESHOLD = 15  # points a hexagon needs to count as covered (PDAL's default)
HEX_FILL = 4.0  # size hexagons to hold ~4x the threshold at the average density


def hexbin_edge(points: int, area: float) -> float:
    """Hexagon edge (m) for the coverage boundary: tight, but each hex expects HEX_FILL x HEX_THRESHOLD points.

    PDAL's automatic edge comes from a density sample and can be several times too coarse,
    which inflates the boundary (and every per-area figure derived from it).
    """
    density = points / area if area > 0 else 1.0
    hex_area = HEX_FILL * HEX_THRESHOLD / density  # m², and a hexagon's area is 3*sqrt(3)/2 * edge²
    return max(0.5, math.sqrt(hex_area / 2.598076211))


def class_name(code: int) -> str:
    if code in ASPRS_CLASSES:
        return ASPRS_CLASSES[code]
    return "Reserved" if code < 64 else "User defined"


def class_counts(stat: DimStats | None, total: int) -> list[ClassCount]:
    if stat is None:
        return []
    return [
        ClassCount(code=int(k), name=class_name(int(k)), count=v, percent=round(100.0 * v / total, 3))
        for k, v in sorted(stat.counts.items(), key=lambda kv: int(kv[0]))
    ]


def value_counts(stat: DimStats | None, total: int) -> list[ValueCount]:
    if stat is None:
        return []
    return [
        ValueCount(value=int(k), count=v, percent=round(100.0 * v / total, 3))
        for k, v in sorted(stat.counts.items(), key=lambda kv: int(kv[0]))
    ]


def write_wkt_geojson(wkts: list[str], out: Path, srs: str, *, job: Job, step: str) -> Path:
    """GeoJSON from WKT geometries (via ogr2ogr, so the CRS is written properly)."""
    with tempfile.TemporaryDirectory(prefix="argus-survey-wkt-") as tmp:
        src = Path(tmp) / "geoms.csv"
        with src.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "WKT"])
            for i, wkt in enumerate(wkts, 1):
                w.writerow([i, wkt])
        out.unlink(missing_ok=True)
        run(
            [
                "ogr2ogr",
                "-f",
                "GeoJSON",
                str(out),
                str(src),
                "-oo",
                "GEOM_POSSIBLE_NAMES=WKT",
                "-oo",
                "KEEP_GEOM_COLUMNS=NO",
                "-a_srs",
                srs,
            ],
            step=step,
            sink=job.sink,
        )
    return out


def horizontal_srs(job: Job) -> str:
    """The horizontal CRS for 2D vector products (``EPSG:n`` when known)."""
    crs = job.sidecar.crs
    assert crs is not None
    return f"EPSG:{crs.horizontal.epsg}" if crs.horizontal.epsg else crs.definition


def raster_srs(job: Job) -> str:
    """Rasters carry the job's full (compound) CRS: their values are heights on its vertical datum."""
    crs = job.sidecar.crs
    assert crs is not None
    return crs.definition


def rasterize_coverage(job: Job, boundary: Path, grid: GridSpec, out: Path, *, step: str) -> Path:
    out.unlink(missing_ok=True)
    run(
        [
            "gdal_rasterize",
            "-q",
            "-burn",
            "1",
            "-init",
            "0",
            "-ot",
            "Byte",
            *grid_mod.gdal_te_tr(grid),
            "-co",
            "COMPRESS=DEFLATE",
            str(boundary),
            str(out),
        ],
        step=step,
        sink=job.sink,
    )
    # gdal_rasterize can't stamp a compound CRS (it keeps the horizontal part); set the full one afterwards
    rasters.set_crs(out, raster_srs(job), sink=job.sink, step=step)
    return out


def qa(job: Job, *, density_res: float = 1.0, min_density: float | None = None, workers: int | None = None) -> QaReport:
    job.require_crs(STEP, height=False)
    copc = job.product_path("copc", needed_by=STEP)
    ingest = job.sidecar.ingest
    assert ingest is not None
    bounds = Bounds3D(**job.sidecar.products["copc"].meta["bounds"])
    grid = grid_mod.snap(bounds, density_res)
    sink = job.sink
    hex_edge = round(hexbin_edge(ingest.output_count, bounds.area), 3)

    with job.step(STEP):
        count_tif = job.path("density_count.tif")
        meta = pdal_pipeline(
            [
                {"type": "readers.copc", "filename": str(copc)},
                {
                    "type": "filters.stats",
                    "dimensions": "Z,ReturnNumber,NumberOfReturns,Classification",
                    "count": "ReturnNumber,NumberOfReturns,Classification",
                },
                {"type": "filters.hexbin", "edge_size": hex_edge, "threshold": HEX_THRESHOLD},
                {
                    "type": "writers.gdal",
                    "filename": str(count_tif),
                    "output_type": "count",
                    "binmode": True,
                    "data_type": "float32",
                    "nodata": rasters.NODATA,
                    "gdalopts": rasters.GTIFF_OPTS,
                    **grid_mod.writer_options(grid),
                },
            ],
            step=STEP,
            sink=sink,
            stream=True,
        )
        stats = {s.name: s for s in parse_stats(meta)}
        total = stats["Z"].count if "Z" in stats else ingest.output_count
        hexbin = (meta.get("stages") or {}).get("filters.hexbin") or {}
        boundary_wkt = hexbin.get("boundary")
        if not boundary_wkt:
            raise SurveyError("filters.hexbin produced no coverage boundary (too few points?)")
        coverage_area = float(hexbin.get("area", 0.0))

        boundary = write_wkt_geojson(
            [boundary_wkt], job.path("boundary.geojson"), horizontal_srs(job), job=job, step=STEP
        )
        coverage = rasterize_coverage(job, boundary, grid, job.path("coverage.tif"), step=STEP)

        cell_area = grid.cell_area
        density_tif = rasters.calc(
            job.path("density.tif"),
            f"B == 1 ? A / {cell_area!r} : {rasters.NODATA!r}",
            {"A": count_tif, "B": coverage},
            sink=sink,
            step=STEP,
        )
        count_tif.unlink(missing_ok=True)

        values, nodata = rasters.read_values(density_tif, sink=sink, step=STEP)
        covered = sorted(v for v in values if v != nodata)
        if not covered:
            raise SurveyError("no density cells inside the coverage boundary")
        mean = sum(covered) / len(covered)
        p50 = rasters.percentile(covered, 50)
        density = DensityStats(
            res=density_res,
            mean=round(mean, 3),
            p5=round(rasters.percentile(covered, 5), 3),
            p50=round(p50, 3),
            p95=round(rasters.percentile(covered, 95), 3),
            covered_cells=len(covered),
        )

        if min_density is not None:
            threshold, rule = min_density, f"--min-density {min_density} pts/m²"
        else:
            threshold = GAP_FRACTION_OF_MEDIAN * p50
            rule = f"{GAP_FRACTION_OF_MEDIAN:.0%} of the median density ({p50:.2f} pts/m²)"
        gap_cells = sum(1 for v in covered if v < threshold)
        gaps_mask = rasters.calc(
            job.path("gaps_mask.tif"),
            f"isnodata(A) ? 0 : (A < {threshold!r} ? 1 : 0)",
            {"A": density_tif},
            sink=sink,
            step=STEP,
            data_type="Byte",
            nodata=0,
        )
        gaps_geojson = job.path("gaps.geojson")
        gaps_geojson.unlink(missing_ok=True)
        run(
            [
                "gdal",
                "raster",
                "polygonize",
                "-i",
                str(gaps_mask),
                "-o",
                str(gaps_geojson),
                "--of",
                "GeoJSON",
                "--attribute-name",
                "gap",
                "--overwrite",
            ],
            step=STEP,
            sink=sink,
        )
        gaps_mask.unlink(missing_ok=True)
        gaps = GapStats(
            threshold=round(threshold, 3),
            threshold_rule=rule,
            cells=gap_cells,
            area_m2=round(gap_cells * cell_area, 2),
            percent_of_coverage=round(100.0 * gap_cells / len(covered), 3),
        )

        tile_plan = tiling.plan(bounds, total, buffer=OUTLIER_BUFFER)
        metas = tiling.run_tiles(
            job,
            copc,
            tile_plan,
            lambda _t: [{"type": "filters.outlier", **OUTLIER_PARAMS}],
            step=STEP,
            out=None,
            expected_count=total,
            workers=workers,
            tail=[{"type": "filters.stats", "dimensions": "Classification", "count": "Classification"}],
        )
        noise = 0
        for m in metas:
            for s in parse_stats(m):
                if s.name == "Classification":
                    noise += s.counts.get("7", 0)
        # points the source already had as class 7 are not "new" outliers
        noise -= stats["Classification"].counts.get("7", 0) if "Classification" in stats else 0

        report = QaReport(
            points=total,
            bounds=bounds,
            coverage_area_m2=round(coverage_area, 2),
            coverage_hex_edge=hex_edge,
            source_type=ingest.source_type,
            density=density,
            classes=class_counts(stats.get("Classification"), total),
            returns=value_counts(stats.get("ReturnNumber"), total),
            outliers=OutlierStats(
                method="filters.outlier (statistical)",
                params=OUTLIER_PARAMS,
                count=max(noise, 0),
                percent=round(100.0 * max(noise, 0) / total, 4),
            ),
            gaps=gaps,
            tiles=len(tile_plan.tiles),
        )
        job.path("qa.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        job.sidecar.qa = report
        job.add_product("qa", "qa.json", step=STEP, description="QA figures (JSON)")
        job.add_product(
            "density",
            "density.tif",
            step=STEP,
            description="Point density (pts/m²)",
            res=density_res,
            grid=grid.model_dump(),
        )
        job.add_product("boundary", "boundary.geojson", step=STEP, description="Coverage boundary (hexbin)")
        job.add_product("coverage", "coverage.tif", step=STEP, description="Coverage mask on the density grid")
        job.add_product("gaps", "gaps.geojson", step=STEP, description=f"Cells below {threshold:.2f} pts/m²")
        if gaps.percent_of_coverage > 5:
            job.warn(f"{gaps.percent_of_coverage:.1f}% of the covered area is below the density threshold ({rule})")
    return report
