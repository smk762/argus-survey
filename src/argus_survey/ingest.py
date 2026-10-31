"""``info`` and ``ingest``: read point clouds, settle their CRS, and write one COPC.

Ingest is the step where datums are decided, so it is the most defensive:

1. Every input is pinned by SHA256 and size, and read with ``pdal info --summary``
   (header only — no full pass over a drone-scale file).
2. The source CRS comes from the files (all must agree), or from ``--s-srs``
   (recorded as user-supplied). A file without a vertical datum stays that way
   unless ``--assume-vertical`` says what its heights are; the assumption is
   recorded and the report flags it.
3. A target CRS (``--t-srs``) is reached through one explicit operation chosen
   by :func:`argus_survey.crs.select_operation` and run verbatim by
   ``filters.projpipeline``; per-point transform errors on PDAL's stderr fail
   the step even though PDAL itself exits 0.
4. Each input streams (bounded memory) through transform → optional crop →
   ``filters.stats`` → LAZ; untwine then builds ``points.copc.laz`` from the parts
   out of core, so a 500-million-point flight never has to fit in RAM.
5. Point counts are reconciled: without a crop, every input point must arrive.
"""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path
from typing import Any

import structlog

from argus_survey import crs as crs_mod
from argus_survey.jobs import Job
from argus_survey.models import (
    Bounds3D,
    CloudSummary,
    CrsRecord,
    DimStats,
    IngestRecord,
    InputRecord,
    SourceType,
    SurveyError,
    TransformOp,
)
from argus_survey.tools import CommandSink, pdal_pipeline, run, run_json, sha256_file

log = structlog.get_logger(__name__)

STEP = "ingest"
COPC_NAME = "points.copc.laz"
POINT_SUFFIXES = (".las", ".laz")
OUTPUT_SCALE = 0.001  # mm: finer than any survey tolerance, coarse enough to compress well

# PDAL logs these per point and exits 0; we treat them as a failed transform.
TRANSFORM_FATAL = (
    "falls outside grid",
    "Reprojection failed",
    "failed to reproject",
    "Unable to transform",
    "Point outside of projection domain",
)

STATS_DIMS = ("Z", "Intensity", "ReturnNumber", "NumberOfReturns", "Classification")
COUNT_DIMS = ("ReturnNumber", "NumberOfReturns", "Classification")


# ── reading ───────────────────────────────────────────────────────────────────


def summarize(path: Path, *, sink: CommandSink | None = None, step: str = STEP) -> tuple[CloudSummary, dict[str, Any]]:
    """Header-level summary of a point cloud and its raw ``srs`` block."""
    info = run_json(["pdal", "info", "--summary", str(path)], step=step, sink=sink)
    s = info.get("summary", {})
    b = s.get("bounds") or {}
    meta = s.get("metadata") or {}
    dims = [d.strip() for d in str(s.get("dimensions", "")).split(",") if d.strip()]
    scale = (meta["scale_x"], meta["scale_y"], meta["scale_z"]) if "scale_x" in meta else None
    summary = CloudSummary(
        count=int(s.get("num_points", meta.get("count", 0))),
        bounds=Bounds3D(**{k: float(b.get(k, math.nan)) for k in ("minx", "miny", "minz", "maxx", "maxy", "maxz")}),
        dimensions=dims,
        las_version=f"{meta['major_version']}.{meta['minor_version']}" if "major_version" in meta else None,
        point_format=meta.get("dataformat_id"),
        # readers.copc (PDAL's choice for *.copc.laz) reports no LAS header block in the summary.
        copc=bool(meta.get("copc", False)) or info.get("reader") == "readers.copc",
        scale=scale,
    )
    return summary, s.get("srs") or {}


def expand_inputs(paths: list[Path]) -> list[Path]:
    """Files as given; a directory contributes its ``*.las``/``*.laz``, sorted."""
    out: list[Path] = []
    for p in paths:
        p = p.expanduser()
        if p.is_dir():
            out.extend(sorted(q for q in p.iterdir() if q.suffix.lower() in POINT_SUFFIXES and q.is_file()))
        elif p.is_file():
            out.append(p)
        else:
            raise SurveyError(f"input {p} does not exist")
    if not out:
        raise SurveyError("no point cloud inputs (.las/.laz) found")
    return out


# ── CRS resolution ───────────────────────────────────────────────────────────


def resolve_source_crs(
    srs_blocks: list[tuple[Path, dict[str, Any]]],
    *,
    s_srs: str | None,
    assume_vertical: str | None,
    sink: CommandSink | None,
) -> CrsRecord:
    """The CRS the input coordinates are actually in, with any assumption recorded."""
    if s_srs is not None:
        record = crs_mod.describe(s_srs, sink=sink, step=STEP, source="user")
    else:
        records = [(p, crs_mod.record_from_pdal_srs(srs)) for p, srs in srs_blocks]
        missing = [p.name for p, r in records if r is None]
        if missing:
            raise SurveyError(
                f"{', '.join(missing[:5])} {'has' if len(missing) == 1 else 'have'} no CRS. Pass --s-srs with the "
                "CRS the coordinates are in (e.g. EPSG:7850 for MGA2020 zone 50); it is recorded as user-supplied"
            )
        first = records[0][1]
        assert first is not None
        for p, r in records[1:]:
            assert r is not None
            if not crs_mod.same_crs(first, r):
                raise SurveyError(f"inputs disagree on CRS: {records[0][0].name} is {first.name}, {p.name} is {r.name}")
        record = first

    if assume_vertical is None:
        return record
    if record.has_height_datum:
        raise SurveyError(
            f"the source CRS already has a vertical datum ({record.vertical.name if record.vertical else '?'}); "
            "--assume-vertical only applies to data without one (use --s-srs to override a wrong file CRS)"
        )
    reason = (
        f"--assume-vertical {assume_vertical}: the input declares no vertical datum; the user stated its heights are "
    )
    if assume_vertical.lower() == "ellipsoidal":
        definition = crs_mod.promote_3d(record.definition, sink=sink, step=STEP)
        assumed = crs_mod.describe(definition, sink=sink, step=STEP, source=record.source)
        assumed.vertical_assumed = reason + "ellipsoidal heights on the horizontal CRS's datum"
        return assumed
    if record.horizontal.epsg is None or not assume_vertical.upper().startswith("EPSG:"):
        raise SurveyError("--assume-vertical EPSG:<code> needs a horizontal CRS with an EPSG code; give --s-srs EPSG:…")
    compound = f"EPSG:{record.horizontal.epsg}+{assume_vertical.split(':', 1)[1]}"
    assumed = crs_mod.describe(compound, sink=sink, step=STEP, source=record.source)
    if assumed.vertical is None:
        raise SurveyError(f"{assume_vertical} is not a vertical CRS")
    assumed.vertical_assumed = reason + assumed.vertical.name
    return assumed


def resolve_target_crs(source: CrsRecord, t_srs: str | None, *, sink: CommandSink | None) -> CrsRecord:
    """The CRS the job will work in. A 2D ``--t-srs`` keeps the source's heights (and says so)."""
    if t_srs is None:
        return source
    target = crs_mod.describe(t_srs, sink=sink, step=STEP, source="user")
    if target.has_height_datum and not source.has_height_datum:
        raise SurveyError(
            f"--t-srs {t_srs} has a vertical datum but the input heights have none; heights cannot be converted "
            "from an unknown datum. Add --assume-vertical (e.g. EPSG:5711 or 'ellipsoidal') if you know it"
        )
    if not target.has_height_datum and source.has_height_datum:
        if source.height_reference == "ellipsoidal":
            target = crs_mod.describe(crs_mod.promote_3d(t_srs, sink=sink, step=STEP), sink=sink, step=STEP)
        elif source.vertical is not None and source.vertical.epsg is not None and target.horizontal.epsg is not None:
            target = crs_mod.describe(f"EPSG:{target.horizontal.epsg}+{source.vertical.epsg}", sink=sink, step=STEP)
        else:
            raise SurveyError(
                f"--t-srs {t_srs} has no vertical datum; give a compound CRS (e.g. EPSG:7850+5711) so the heights' "
                "datum is explicit"
            )
        target.source = "user"
    return target


def geoid_grids(op: TransformOp) -> list[str]:
    """Grids applied by ``vgridshift`` steps — the geoid model a height transformation used."""
    out: list[str] = []
    for step in op.proj.split("+step"):
        if "+proj=vgridshift" in step:
            out.extend(g.lstrip("@") for m in re.findall(r"\+grids=(\S+)", step) for g in m.split(","))
    return out


# ── crop ──────────────────────────────────────────────────────────────────────


def crop_polygons(path: Path, target: CrsRecord, *, crop_srs: str | None, sink: CommandSink | None) -> list[str]:
    """Polygons of *path* as WKT in the target CRS. Refuses a polygon whose CRS is unknown or implausible."""
    from argus_survey.vectors import read_polygons

    return [
        w for _, w in read_polygons(path, target.definition, src_srs=crop_srs, sink=sink, step=STEP, flag="--crop-srs")
    ]


# ── stats ─────────────────────────────────────────────────────────────────────


def parse_stats(metadata: dict[str, Any]) -> list[DimStats]:
    """``filters.stats`` output from a pipeline's metadata."""
    stages = metadata.get("stages", metadata)
    block = stages.get("filters.stats") or {}
    out: list[DimStats] = []
    for s in block.get("statistic", []):
        counts: dict[str, int] = {}
        for item in s.get("counts", []):
            value, _, n = str(item).partition("/")
            try:
                counts[str(int(float(value)))] = int(n)
            except ValueError:
                continue
        out.append(
            DimStats(
                name=s["name"],
                count=int(s.get("count", 0)),
                minimum=float(s.get("minimum", math.nan)),
                maximum=float(s.get("maximum", math.nan)),
                average=float(s.get("average", math.nan)),
                stddev=float(s.get("stddev", 0.0)),
                counts=counts,
            )
        )
    return out


def merge_stats(parts: list[list[DimStats]]) -> list[DimStats]:
    """Combine per-file statistics exactly (pooled mean and variance, summed counts)."""
    by_name: dict[str, list[DimStats]] = {}
    for part in parts:
        for s in part:
            by_name.setdefault(s.name, []).append(s)
    merged: list[DimStats] = []
    for name, items in by_name.items():
        n = sum(i.count for i in items)
        if n == 0:
            continue
        mean = sum(i.average * i.count for i in items) / n
        # pooled variance from per-part (population) variances
        var = sum(i.count * (i.stddev**2 + (i.average - mean) ** 2) for i in items) / n
        counts: dict[str, int] = {}
        for i in items:
            for k, v in i.counts.items():
                counts[k] = counts.get(k, 0) + v
        merged.append(
            DimStats(
                name=name,
                count=n,
                minimum=min(i.minimum for i in items),
                maximum=max(i.maximum for i in items),
                average=mean,
                stddev=math.sqrt(max(var, 0.0)),
                counts=dict(sorted(counts.items(), key=lambda kv: int(kv[0]))),
            )
        )
    return merged


def detect_source_type(stats: list[DimStats], dimensions: list[str]) -> tuple[SourceType, str]:
    """LiDAR vs photogrammetry from return numbering (and colour), with the reason."""
    by = {s.name: s for s in stats}
    rn, nr = by.get("ReturnNumber"), by.get("NumberOfReturns")
    has_rgb = all(d in dimensions for d in ("Red", "Green", "Blue"))
    if rn is not None and rn.maximum == 0:
        return "photogrammetry", "every ReturnNumber is 0 (no laser returns recorded)"
    if nr is not None and nr.maximum >= 2:
        return "lidar", f"multiple returns per pulse (max NumberOfReturns {int(nr.maximum)})"
    if has_rgb:
        return "photogrammetry", "single returns only, with RGB colour"
    return "unknown", "single returns only and no colour: can't tell LiDAR from photogrammetry"


# ── ingest ────────────────────────────────────────────────────────────────────


def pin_input(path: Path, summary: CloudSummary) -> InputRecord:
    path = path.resolve()
    return InputRecord(path=str(path), sha256=sha256_file(path), size=path.stat().st_size, summary=summary)


def ingest(
    job: Job,
    inputs: list[Path],
    *,
    t_srs: str | None = None,
    s_srs: str | None = None,
    assume_vertical: str | None = None,
    operation: str | None = None,
    crop: Path | None = None,
    crop_srs: str | None = None,
    force: bool = False,
) -> IngestRecord:
    """Ingest *inputs* into *job*: CRS settled and recorded, one COPC written."""
    if job.sidecar.ingest is not None and not force:
        raise SurveyError(f"job {job.id!r} is already ingested; use a new job id, or --force to redo it")
    files = expand_inputs(inputs)
    sink = job.sink
    with job.step(STEP):
        if force:
            _reset(job)

        summaries: list[tuple[Path, CloudSummary, dict[str, Any]]] = []
        for f in files:
            summary, srs = summarize(f, sink=sink)
            summaries.append((f, summary, srs))
        job.sidecar.inputs = [pin_input(f, s) for f, s, _ in summaries]
        job.save()

        source = resolve_source_crs(
            [(f, srs) for f, _, srs in summaries], s_srs=s_srs, assume_vertical=assume_vertical, sink=sink
        )
        target = resolve_target_crs(source, t_srs, sink=sink)
        crs_mod.require_metric(target, "the target CRS" if t_srs else "the input CRS")
        crs_mod.require_survey_projection(target, "the target CRS" if t_srs else "the input CRS")

        op: TransformOp | None = None
        coord_op: str | None = None
        if not crs_mod.same_crs(source, target):
            union = _union_bounds([s.bounds for _, s, _ in summaries])
            bbox = crs_mod.lonlat_bbox(source.definition, union, sink=sink, step=STEP)
            op = crs_mod.select_operation(
                source.definition, target.definition, bbox_lonlat=bbox, prefer=operation, sink=sink, step=STEP
            )
            coord_op = crs_mod.pdal_coord_op(op, source, target)
            grids = geoid_grids(op)
            target = target.model_copy(update={"geoid": ", ".join(grids) if grids else None, "source": "transformed"})
            if op.accuracy_m is None:
                job.warn(f"transformation {op.name!r} has no stated accuracy")
        elif operation is not None:
            raise SurveyError("--operation given but source and target CRS are the same; nothing to transform")
        target = target.model_copy(update={"vertical_assumed": source.vertical_assumed})

        polygons = crop_polygons(crop, target, crop_srs=crop_srs, sink=sink) if crop is not None else None

        parts_dir = job.path(".ingest-parts")
        shutil.rmtree(parts_dir, ignore_errors=True)
        parts_dir.mkdir(parents=True)
        part_stats: list[list[DimStats]] = []
        try:
            for i, (f, _summary, _srs) in enumerate(summaries):
                stages = _pipeline(
                    f,
                    parts_dir / f"part-{i:04d}.laz",
                    source=source,
                    target=target,
                    coord_op=coord_op,
                    polygons=polygons,
                    reader_override=s_srs is not None or assume_vertical is not None,
                )
                meta = pdal_pipeline(stages, step=STEP, sink=sink, stream=True, fatal_stderr=TRANSFORM_FATAL)
                part_stats.append(parse_stats(meta))
            stats = merge_stats(part_stats)
            kept = next((s.count for s in stats if s.name == "Z"), 0)
            total = sum(s.count for _, s, _ in summaries)
            if polygons is None and kept != total:
                raise SurveyError(f"{total - kept} of {total} points were lost in ingest; refusing a partial dataset")
            if kept == 0:
                raise SurveyError("no points left after the crop" if polygons else "the inputs contain no points")

            copc = job.path(COPC_NAME)
            copc.unlink(missing_ok=True)
            tmp = job.path(".untwine-tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            run(["untwine", "-i", str(parts_dir), "-o", str(copc), "--temp_dir", str(tmp)], step=STEP, sink=sink)
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            shutil.rmtree(parts_dir, ignore_errors=True)

        out_summary, out_srs = summarize(copc, sink=sink)
        if out_summary.count != kept:
            raise SurveyError(f"COPC holds {out_summary.count} points but {kept} went in")
        b = out_summary.bounds
        if not all(math.isfinite(v) for v in (b.minx, b.miny, b.minz, b.maxx, b.maxy, b.maxz)):
            raise SurveyError("COPC bounds are not finite — the transformation produced invalid coordinates")
        written = crs_mod.record_from_pdal_srs(out_srs)
        if written is None:
            raise SurveyError("the COPC was written without a CRS")

        source_type, why = detect_source_type(stats, summaries[0][1].dimensions)
        record = IngestRecord(
            inputs_count=total,
            output_count=kept,
            cropped=polygons is not None,
            crop=f"{crop.name} ({len(polygons)} polygon(s))" if crop is not None and polygons else None,
            scale=OUTPUT_SCALE,
            source_crs=source,
            transform=op,
            source_type=source_type,
            stats=stats,
        )
        job.sidecar.crs = target
        job.sidecar.ingest = record
        job.add_product(
            "copc",
            COPC_NAME,
            step=STEP,
            description="Point cloud (COPC) in the job CRS",
            count=kept,
            bounds=out_summary.bounds.model_dump(),
            source_type=source_type,
            source_type_reason=why,
        )
        _warnings(job, target, record)
    return record


def _warnings(job: Job, target: CrsRecord, record: IngestRecord) -> None:
    if target.vertical_assumed:
        job.warn(f"Vertical datum ASSUMED, not read from the data: {target.vertical_assumed}")
    if target.height_reference == "ellipsoidal":
        job.warn(
            "Heights are ellipsoidal, not a gravity-related datum (AHD): levels, contours and design comparisons "
            "will differ from AHD by the geoid separation (~30 m around Perth). Re-ingest with e.g. "
            "--t-srs EPSG:7850+5711 to apply AUSGeoid2020"
        )
    if record.transform and record.transform.accuracy_m and record.transform.accuracy_m >= 1.0:
        job.warn(
            f"The transformation used ({record.transform.name}) is only accurate to "
            f"~{record.transform.accuracy_m} m — not survey grade"
        )
    if record.source_type == "unknown":
        job.warn("Could not tell LiDAR from photogrammetry; DSM uses all points")


def _union_bounds(bounds: list[Bounds3D]) -> Bounds3D:
    return Bounds3D(
        minx=min(b.minx for b in bounds),
        miny=min(b.miny for b in bounds),
        minz=min(b.minz for b in bounds),
        maxx=max(b.maxx for b in bounds),
        maxy=max(b.maxy for b in bounds),
        maxz=max(b.maxz for b in bounds),
    )


def _pipeline(
    src: Path,
    dst: Path,
    *,
    source: CrsRecord,
    target: CrsRecord,
    coord_op: str | None,
    polygons: list[str] | None,
    reader_override: bool,
) -> list[dict[str, Any]]:
    reader: dict[str, Any] = {"type": "readers.las", "filename": str(src)}
    if reader_override:
        reader["override_srs"] = source.definition
    stages: list[dict[str, Any]] = [reader]
    if coord_op is not None:
        stages.append({"type": "filters.projpipeline", "coord_op": coord_op, "out_srs": target.definition})
    if polygons is not None:
        stages.append({"type": "filters.crop", "polygon": polygons})
    stages.append({"type": "filters.stats", "dimensions": ",".join(STATS_DIMS), "count": ",".join(COUNT_DIMS)})
    stages.append(
        {
            "type": "writers.las",
            "filename": str(dst),
            "compression": True,
            "forward": "major_version,minor_version,dataformat_id",
            "extra_dims": "all",
            "scale_x": OUTPUT_SCALE,
            "scale_y": OUTPUT_SCALE,
            "scale_z": OUTPUT_SCALE,
            "offset_x": "auto",
            "offset_y": "auto",
            "offset_z": "auto",
            "a_srs": target.definition,
        }
    )
    return stages


def _reset(job: Job) -> None:
    """Forget a previous ingest (and everything derived from it) before redoing it."""
    for product in job.sidecar.products.values():
        job.path(product.path).unlink(missing_ok=True)
    job.sidecar.products = {}
    job.sidecar.ingest = None
    job.sidecar.crs = None
    job.sidecar.inputs = []
    job.sidecar.warnings = []
    job.sidecar.steps_done = [s for s in job.sidecar.steps_done if s == STEP]
    job.save()


def info_report(path: Path) -> dict[str, Any]:
    """What ``argus-survey info`` prints: header summary + CRS record, no job."""
    summary, srs = summarize(path)
    record = crs_mod.record_from_pdal_srs(srs)
    return {
        "file": str(path),
        "summary": summary.model_dump(),
        "crs": record.model_dump() if record else None,
        "crs_note": None if record else "the file declares no CRS; ingest will need --s-srs",
    }
