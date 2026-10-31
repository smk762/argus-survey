"""Contours from the DTM: ``gdal_contour`` at an interval, index contours flagged, GPKG + GeoJSON.

Contours are drawn from the DTM exactly as delivered: a void stays a gap in the
contours rather than being bridged by a guess (fill voids first with
``surfaces --fill-voids`` if that's what the client wants — it's recorded).
The ``index`` attribute is 1 on every N-th contour (multiples of interval × N).
"""

from __future__ import annotations

from argus_survey.jobs import Job
from argus_survey.models import ContoursReport, SurveyError
from argus_survey.tools import run, run_json

STEP = "contours"
LAYER = "contours"


def contours(job: Job, *, interval: float, index: int | None = 5, source: str = "dtm") -> ContoursReport:
    job.require_crs(STEP, height=True)
    if interval <= 0:
        raise SurveyError("--interval must be positive (metres)")
    if index is not None and index < 2:
        raise SurveyError("--index must be 2 or more (every N-th contour is an index contour)")
    dem = job.product_path(source, needed_by=STEP)
    sink = job.sink
    with job.step(STEP):
        raw = job.path(".contours_raw.gpkg")
        gpkg = job.path("contours.gpkg")
        geojson = job.path("contours.geojson")
        for p in (raw, gpkg, geojson):
            p.unlink(missing_ok=True)
        run(
            ["gdal_contour", "-q", "-a", "ELEV", "-i", repr(interval), "-nln", LAYER, "-f", "GPKG", str(dem), str(raw)],
            step=STEP,
            sink=sink,
        )
        if index:
            step_m = interval * index
            idx = f"CASE WHEN ABS(ELEV / {step_m!r} - ROUND(ELEV / {step_m!r})) < 1e-6 THEN 1 ELSE 0 END"
        else:
            idx = "0"
        sql = f"SELECT *, {idx} AS idx FROM {LAYER}"
        run(
            ["ogr2ogr", "-f", "GPKG", str(gpkg), str(raw), "-dialect", "SQLite", "-sql", sql, "-nln", LAYER],
            step=STEP,
            sink=sink,
        )
        raw.unlink(missing_ok=True)
        run(["ogr2ogr", "-f", "GeoJSON", str(geojson), str(gpkg), LAYER], step=STEP, sink=sink)

        summary = run_json(
            [
                "ogrinfo",
                "-json",
                "-features",
                "-ro",
                str(gpkg),
                "-sql",
                f"SELECT COUNT(*) AS n, MIN(ELEV) AS lo, MAX(ELEV) AS hi FROM {LAYER}",
            ],
            step=STEP,
            sink=sink,
        )
        props = summary["layers"][0]["features"][0]["properties"]
        report = ContoursReport(
            interval=interval,
            index_every=index,
            source=source,
            features=int(props.get("n") or 0),
            min_elev=props.get("lo"),
            max_elev=props.get("hi"),
        )
        job.sidecar.contours = report
        job.add_product(
            "contours",
            "contours.gpkg",
            step=STEP,
            description=f"Contours every {interval} m (GPKG)",
            interval=interval,
            index=index,
        )
        job.add_product("contours_geojson", "contours.geojson", step=STEP, description="Contours (GeoJSON)")
        if job.sidecar.crs and job.sidecar.crs.height_reference == "ellipsoidal":
            job.warn("Contours are of ellipsoidal heights, not AHD")
        if report.features == 0:
            job.warn(f"No contours at a {interval} m interval — the DTM's relief is smaller than the interval")
    return report
