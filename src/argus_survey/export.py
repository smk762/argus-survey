"""Point-cloud deliverables for ReCap / Civil 3D: uncompressed LAS 1.4 or E57, with export notes.

ReCap Pro imports LAS and E57 (Autodesk's supported-formats table); LAZ is not listed, so the
LAS is written uncompressed. Civil 3D brings point clouds in through ReCap (``.rcp``/``.rcs``).

- The source is the classified cloud when ground has run (ASPRS classes kept as they are),
  else the ingested COPC.
- ``--polygon`` crops to polygon(s) (reprojected to the job CRS like every other polygon);
  ``--thin S`` keeps the first point in each S-metre voxel (``filters.voxeldownsize``, mode
  ``first``): real points only, never averaged or synthesised.
- Everything streams, so a drone-scale cloud exports in bounded memory.
- **E57 has no CRS field.** It is written in double precision (the writer's single-precision
  default rounds an MGA northing by ~0.25 m), and the notes file states the CRS that the
  coordinates are in; so does the LAS header (WKT, LAS 1.4).
- Without a crop or thinning, the exported count must equal the source count.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import structlog

from argus_survey.ingest import parse_stats
from argus_survey.jobs import Job
from argus_survey.models import ExportRecord, SurveyError
from argus_survey.qa import ASPRS_CLASSES, raster_srs
from argus_survey.tools import pdal_pipeline
from argus_survey.vectors import read_polygons

log = structlog.get_logger(__name__)

STEP = "export"
SCALE = 0.001


def export_points(
    job: Job,
    fmt: Literal["las", "e57"],
    *,
    polygon: Path | None = None,
    polygon_srs: str | None = None,
    thin: float | None = None,
) -> ExportRecord:
    job.require_crs(STEP, height=False)
    if fmt not in ("las", "e57"):
        raise SurveyError("--points must be las or e57")
    if thin is not None and thin <= 0:
        raise SurveyError("--thin must be a positive voxel size in metres")
    crs = job.sidecar.crs
    assert crs is not None
    classified = "classified" in job.sidecar.products
    src = job.product_path("classified" if classified else "copc", needed_by=STEP)
    expected = job.sidecar.ingest.output_count if job.sidecar.ingest else None
    sink = job.sink

    with job.step(STEP):
        stages: list[dict[str, Any]] = [{"type": "readers.copc", "filename": str(src)}]
        crop_desc = None
        if polygon is not None:
            polys = read_polygons(polygon, raster_srs(job), src_srs=polygon_srs, sink=sink, step=STEP)
            stages.append({"type": "filters.crop", "polygon": [w for _, w in polys]})
            crop_desc = f"{polygon.name} ({len(polys)} polygon(s))"
        if thin is not None:
            stages.append({"type": "filters.voxeldownsize", "cell": thin, "mode": "first"})
        stages.append({"type": "filters.stats", "dimensions": "Classification", "count": "Classification"})
        name = f"points.{fmt}"
        out = job.path(name)
        out.unlink(missing_ok=True)
        # the previous export of this format is gone from disk: forget it now, so a failed run leaves no stale record
        job.sidecar.exports = [e for e in job.sidecar.exports if e.format != fmt]
        for kind in (f"points_{fmt}", f"points_{fmt}_notes"):
            if (old := job.sidecar.products.pop(kind, None)) is not None:
                job.path(old.path).unlink(missing_ok=True)
        if fmt == "las":
            stages.append(
                {
                    "type": "writers.las",
                    "filename": str(out),
                    "compression": False,
                    "minor_version": 4,
                    "dataformat_id": 7 if _has_rgb(job) else 6,
                    "extra_dims": "all",
                    "scale_x": SCALE,
                    "scale_y": SCALE,
                    "scale_z": SCALE,
                    "offset_x": "auto",
                    "offset_y": "auto",
                    "offset_z": "auto",
                    "a_srs": crs.definition,
                }
            )
        else:
            stages.append({"type": "writers.e57", "filename": str(out), "double_precision": True})
        meta = pdal_pipeline(stages, step=STEP, sink=sink, stream=True)
        cls = next((s for s in parse_stats(meta) if s.name == "Classification"), None)
        count = cls.count if cls else 0
        refusal = None
        if count == 0:
            refusal = "nothing to export: the crop/thinning left no points"
        elif polygon is None and thin is None and expected is not None and count != expected:
            refusal = f"exported {count} points but the job holds {expected}; refusing a partial export"
        if refusal is not None:
            out.unlink(missing_ok=True)  # a refused export must not leave a deliverable-looking file behind
            raise SurveyError(refusal)

        classes = {int(k): v for k, v in (cls.counts if cls else {}).items()}
        notes_name = f"points_{fmt}_notes.txt"
        record = ExportRecord(
            format=fmt,
            path=name,
            source="classified" if classified else "copc",
            points=count,
            crop=crop_desc,
            thin_m=thin,
            crs=crs.definition,
            crs_name=crs.name,
            crs_in_file=fmt == "las",
            classes=classes,
            notes=notes_name,
        )
        job.path(notes_name).write_text(export_notes(record, job), encoding="utf-8")
        job.add_product(
            f"points_{fmt}", name, step=STEP, description=f"Point cloud for ReCap ({fmt.upper()})", count=count
        )
        job.add_product(f"points_{fmt}_notes", notes_name, step=STEP, description=f"{fmt.upper()} export notes")
        job.sidecar.exports.append(record)
        job.save()
        if fmt == "e57":
            job.warn(f"points.e57 carries no CRS: its coordinates are {crs.name}; see {notes_name}")
    return record


def _has_rgb(job: Job) -> bool:
    inputs = job.sidecar.inputs
    return bool(inputs) and all(
        i.summary is not None and {"Red", "Green", "Blue"} <= set(i.summary.dimensions) for i in inputs
    )


def export_notes(r: ExportRecord, job: Job) -> str:
    crs = job.sidecar.crs
    assert crs is not None
    lines = [
        f"argus-survey point cloud export — job {job.id}",
        "",
        f"File:        {r.path} ({r.format.upper()}{', LAS 1.4, uncompressed' if r.format == 'las' else ', double precision'})",
        f"Points:      {r.points:,}",
        f"Source:      {'classified cloud (ground.copc.laz)' if r.source == 'classified' else 'ingested cloud (points.copc.laz), not classified'}",
        f"Crop:        {r.crop or 'none'}",
        f"Thinning:    {f'first point per {r.thin_m:g} m voxel' if r.thin_m else 'none'}",
        "",
        "Coordinates",
        f"  CRS:       {crs.name} ({crs.definition})",
        f"  Vertical:  {crs.vertical.name if crs.vertical else 'NONE — heights are of unknown datum'}",
        f"  Geoid:     {crs.geoid or 'none applied'}",
        f"  In file:   {'yes (LAS 1.4 WKT)' if r.crs_in_file else 'NO — E57 has no CRS field; assign the CRS above on import'}",
    ]
    if crs.vertical_assumed:
        lines.append(f"  ASSUMED:   {crs.vertical_assumed}")
    sf = job.sidecar.ingest.scale_factors if job.sidecar.ingest else None
    if sf is not None:
        lines.append(
            f"  Grid:      coordinates are grid; combined scale factor {sf.combined:.8f}"
            if sf.combined is not None
            else f"  Grid:      coordinates are grid; point scale factor {sf.point_scale:.8f}"
        )
    lines += ["", "Classification (ASPRS LAS 1.4 codes, unchanged)"]
    for code, n in sorted(r.classes.items()):
        lines.append(f"  {code:>3}  {ASPRS_CLASSES.get(code, 'user-defined/reserved'):<28} {n:>12,}")
    lines += [
        "",
        "ReCap / Civil 3D",
        "  ReCap Pro imports LAS and E57 directly (LAZ is not in Autodesk's supported-formats list, hence",
        "  uncompressed LAS). Import this file into a ReCap project; where the import settings offer a",
        "  coordinate system, choose the CRS above (for E57 it is the only place it can come from).",
        "  Save the .rcp and attach it in a Civil 3D drawing set to the same coordinate system.",
        "  The Autodesk coordinate system code for this CRS is not mapped yet (argus-survey issue #9).",
    ]
    return "\n".join(lines) + "\n"
