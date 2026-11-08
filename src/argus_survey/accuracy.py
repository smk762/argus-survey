"""Vertical accuracy against surveyed check points, per ASPRS Positional Accuracy Standards, Edition 2, Version 2.

Checked against the standard's text (Edition 2, Version 2, 2024; section numbers below are
that edition's):

- Accuracy is reported as **RMSE only** — Edition 1's 95 % confidence figure is gone, so
  it is never printed. (Australian ICSM specifications still state accuracy at 95 %; that
  figure is deliberately not reported here — see the project's issues.)
- Accuracy classes are free-valued ("#-cm", §7.4, Table 7.2): NVA RMSE_V ≤ #; VVA "as
  found". The user states the class with ``--class-cm``.
- **Product accuracy** (§7.12.4): RMSE_V = √(RMSE_V1² + RMSE_V2²), where RMSE_V1 is the
  residuals' RMSE and RMSE_V2 the check point survey's own 1σ vertical RMSE as reported by the
  surveyor (``--checkpoint-rmse``; Addendum II §A). Check points should be at least twice as
  accurate as the class (§7.13).
- At least **30** check points per group (§7.14); fewer get §7.16.1's reduced-checkpoint
  statement ("tested as required by … ONLY n checkpoints"), not the "tested to meet" one.
- Statements follow §7.16.1's wording. The standard has no wording for a failed NVA test;
  ours says so plainly.
- §7.16 statistics: min, max, mean, median, std, RMSE. §7.2: mean error should be < 25 % of
  the target RMSE (else investigate); a residual beyond 3 × the target RMSE is a blunder.
  Blunders are listed, never dropped (§C.9: "> 3σ" alone is no reason to discard a point).
- The surface is sampled bilinearly. §C.11 recommends the containing pixel unless the data
  user agrees otherwise; the method is stated in the report either way.
- Horizontal accuracy (RMSE_H) is not tested. Fine for LiDAR (§7.6), but §7.6 asks for it
  on photogrammetric elevation data, so a photogrammetry job gets a note.

Residuals are surface − check (data minus checkpoint). A point outside the surface or in a
void is excluded and listed, never silently dropped. Check points are given in a stated CRS
(with a vertical datum) and transformed to the job CRS through one explicit PROJ operation
(``cct``), recorded like every other.
"""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path

from argus_survey import crs as crs_mod
from argus_survey import rasters
from argus_survey.jobs import Job
from argus_survey.models import (
    AccuracyGroup,
    AccuracyReport,
    Bounds3D,
    CheckResidual,
    FileRef,
    SurveyError,
    TransformOp,
)
from argus_survey.qa import horizontal_srs
from argus_survey.tools import run, sha256_file

STEP = "accuracy"
STANDARD = "ASPRS Positional Accuracy Standards for Digital Geospatial Data, Edition 2, Version 2 (2024)"
MIN_CHECKS = 30
BIAS_FRACTION = 0.25  # §7.2: mean error should be below 25 % of the target RMSE
BLUNDER_FACTOR = 3.0  # §7.2: beyond 3 × the target RMSE is a blunder

VVA_WORDS = ("veg", "vva")


def read_checks(path: Path, cols: tuple[str, str, str, str], desc_col: str = "description") -> list[dict[str, str]]:
    id_c, x_c, y_c, z_c = cols
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SurveyError(f"{path.name} has no rows")
    missing = [c for c in cols if c not in rows[0]]
    if missing:
        raise SurveyError(f"{path.name} lacks column(s) {', '.join(missing)} (have: {', '.join(rows[0])}); see --cols")
    out = []
    for r in rows:
        try:
            out.append(
                {
                    "id": r[id_c].strip(),
                    "x": float(r[x_c]),
                    "y": float(r[y_c]),
                    "z": float(r[z_c]),
                    "description": (r.get(desc_col) or "").strip(),
                }
            )  # type: ignore[dict-item]
        except ValueError as exc:
            raise SurveyError(f"{path.name}: bad number in row {r}") from exc
    ids = [r["id"] for r in out]
    if len(set(ids)) != len(ids):
        raise SurveyError(f"{path.name}: check point ids are not unique")
    return out


def group_of(description: str) -> str:
    d = description.lower()
    return "VVA" if any(d.startswith(w) for w in VVA_WORDS) else "NVA"


def group_stats(
    rows: list[CheckResidual], group: str, checkpoint_rmse: float | None, target_class_cm: float | None = None
) -> AccuracyGroup:
    mine = [r for r in rows if r.group == group and r.residual is not None]
    used = [r.residual for r in mine if r.residual is not None]
    excluded = sum(1 for r in rows if r.group == group and r.residual is None)
    n = len(used)
    if n == 0:
        return AccuracyGroup(
            group=group,
            n=0,
            excluded=excluded,
            mean_m=None,
            std_m=None,
            rmse_data_m=None,  # type: ignore[arg-type]
            rmse_m=None,
            max_abs_m=None,
            meets_minimum_count=False,
            pass_fail_applies=group == "NVA",
        )
    mean = sum(used) / n
    std = math.sqrt(sum((e - mean) ** 2 for e in used) / (n - 1)) if n > 1 else None
    rmse = math.sqrt(sum(e * e for e in used) / n)
    product = math.sqrt(rmse * rmse + checkpoint_rmse * checkpoint_rmse) if checkpoint_rmse is not None else rmse
    limit = BLUNDER_FACTOR * (target_class_cm / 100 if target_class_cm is not None else rmse)
    blunders = [r.id for r in mine if r.residual is not None and limit > 0 and abs(r.residual) > limit]
    return AccuracyGroup(
        group=group,  # type: ignore[arg-type]
        n=n,
        excluded=excluded,
        mean_m=round(mean, 4),
        std_m=round(std, 4) if std is not None else None,
        rmse_data_m=round(rmse, 4),
        rmse_m=round(product, 4),
        max_abs_m=round(max(abs(e) for e in used), 4),
        meets_minimum_count=n >= MIN_CHECKS,
        pass_fail_applies=group == "NVA",
        min_m=round(min(used), 4),
        max_m=round(max(used), 4),
        median_m=round(statistics.median(used), 4),
        blunders=blunders,
    )


def statement_for(g: AccuracyGroup, target_class_cm: float | None) -> str:
    """§7.16.1's wording for one tested group (NVA or VVA)."""
    assert g.rmse_m is not None
    found = f"{100 * g.rmse_m:.1f} (cm)"
    name = "Non-Vegetated Vertical Accuracy (NVA)" if g.group == "NVA" else "Vegetated Vertical Accuracy (VVA)"
    area = f"{g.group} tested area"
    if not g.meets_minimum_count:
        produced = (
            f"This data set was produced to meet a {target_class_cm:g} (cm) RMSEV Vertical Positional Accuracy Class. "
            if target_class_cm is not None
            else ""
        )
        return (
            f"This data set was tested as required by {STANDARD}. Although the Standards call for a minimum of "
            f"thirty (30) checkpoints, this test was performed using ONLY {g.n} checkpoints. {produced}"
            f"The tested vertical positional accuracy was found to be RMSEV = {found} using the reduced number of "
            f"checkpoints in the {area}."
        )
    if target_class_cm is None:
        return f"This data set was tested as required by {STANDARD}. The {name} was found to be RMSEV = {found}."
    if g.group == "NVA" and 100 * g.rmse_m > target_class_cm:
        # the standard has no wording for a failed test
        return (
            f"This data set was tested against {STANDARD} for a {target_class_cm:g} (cm) RMSEV Vertical Accuracy "
            f"Class and does NOT meet it. The {name} was found to be RMSEV = {found}."
        )
    return (
        f"This data set was tested to meet {STANDARD} for a {target_class_cm:g} (cm) RMSEV Vertical Accuracy Class. "
        f"The {name} was found to be RMSEV = {found}."
    )


def transform_points(
    job: Job, pts: list[dict[str, str]], checks_crs: str
) -> tuple[list[tuple[float, float, float]], TransformOp | None]:
    """Check point coordinates in the job CRS (and the operation used, if one was needed)."""
    job_crs = job.sidecar.crs
    assert job_crs is not None
    src = crs_mod.describe(checks_crs, sink=job.sink, step=STEP)
    if not src.has_height_datum:
        raise SurveyError(
            f"--crs {checks_crs} has no vertical datum; give the check points' compound CRS "
            "(e.g. EPSG:7850+5711) so their heights are comparable"
        )
    coords = [(float(p["x"]), float(p["y"]), float(p["z"])) for p in pts]
    if crs_mod.same_crs(src, job_crs):
        return coords, None
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    bbox = crs_mod.lonlat_bbox(
        src.definition,
        Bounds3D(minx=min(xs), miny=min(ys), minz=0, maxx=max(xs), maxy=max(ys), maxz=0),
        sink=job.sink,
        step=STEP,
    )
    op = crs_mod.select_operation(src.definition, job_crs.definition, bbox_lonlat=bbox, sink=job.sink, step=STEP)
    pipeline = crs_mod.pdal_coord_op(op, src, job_crs)
    stdin = "".join(f"{x!r} {y!r} {z!r}\n" for x, y, z in coords)
    out = run(["cct", "-d", "6", *pipeline.split()], step=STEP, sink=job.sink, stdin=stdin).stdout
    moved: list[tuple[float, float, float]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            moved.append((float(parts[0]), float(parts[1]), float(parts[2])))
    if len(moved) != len(coords) or not all(math.isfinite(v) for p in moved for v in p):
        raise SurveyError("transforming the check points failed (outside the transformation's area or grid)")
    return moved, op


def accuracy(
    job: Job,
    checks: Path,
    *,
    checks_crs: str | None,
    cols: tuple[str, str, str, str] = ("id", "x", "y", "z"),
    checkpoint_rmse: float | None = None,
    surface: str = "dtm",
    target_class_cm: float | None = None,
) -> AccuracyReport:
    job.require_crs(STEP, height=True)
    if checks_crs is None:
        raise SurveyError("state the check points' CRS with --crs (e.g. EPSG:7850+5711); it is never assumed")
    dem = job.product_path(surface, needed_by=STEP)
    pts = read_checks(checks, cols)
    with job.step(STEP):
        coords, op = transform_points(job, pts, checks_crs)
        samples = rasters.sample(
            dem,
            [(p["id"], c[0], c[1]) for p, c in zip(pts, coords, strict=True)],
            crs=horizontal_srs(job),
            resampling="bilinear",
            sink=job.sink,
            step=STEP,
        )
        residuals: list[CheckResidual] = []
        for p, (x, y, z) in zip(pts, coords, strict=True):
            v = samples[p["id"]]
            residuals.append(
                CheckResidual(
                    id=p["id"],
                    x=round(x, 4),
                    y=round(y, 4),
                    z_check=round(z, 4),
                    z_surface=round(v, 4) if v is not None else None,
                    residual=round(v - z, 4) if v is not None else None,
                    group=group_of(p["description"]),  # type: ignore[arg-type]
                    description=p["description"],
                    excluded=None if v is not None else "outside the surface or in a void",
                )
            )
        groups = [group_stats(residuals, g, checkpoint_rmse, target_class_cm) for g in ("NVA", "VVA")]
        groups = [g for g in groups if g.n or g.excluded]
        nva = next((g for g in groups if g.group == "NVA"), None)
        vva = next((g for g in groups if g.group == "VVA"), None)

        notes: list[str] = []
        statement_parts: list[str] = []
        if nva is None or nva.n == 0:
            notes.append("No non-vegetated check points fell on the surface: no NVA could be computed")
        for g in (nva, vva):
            if g is not None and g.rmse_m is not None:
                statement_parts.append(statement_for(g, target_class_cm))
        if vva and vva.rmse_m is not None:
            statement_parts.append("(Edition 2 sets no VVA pass/fail; it is reported as found.)")
        for g in groups:
            if g.n and not g.meets_minimum_count:
                notes.append(
                    f"{g.group}: {g.n} check points — Edition 2 requires at least {MIN_CHECKS} (§7.14); the "
                    "statement uses the reduced-checkpoint wording of §7.16.1"
                )
            if g.excluded:
                notes.append(f"{g.group}: {g.excluded} check point(s) excluded (outside the surface or in a void)")
            if g.blunders:
                basis = f"the {target_class_cm:g}-cm target RMSE" if target_class_cm is not None else "the data RMSE"
                notes.append(
                    f"{g.group}: {len(g.blunders)} residual(s) beyond 3 × {basis} ({', '.join(g.blunders[:10])}) — "
                    "investigate as blunders (§7.2); they are kept in the statistics"
                )
        if checkpoint_rmse is None:
            notes.append(
                "Check point survey accuracy not supplied (--checkpoint-rmse, the surveyor's 1σ vertical RMSE): the "
                "RMSE shown is of the residuals alone; Edition 2 product accuracy also includes it (§7.12.4)"
            )
        elif target_class_cm is not None and 100 * checkpoint_rmse > target_class_cm / 2:
            notes.append(
                f"Check points ({100 * checkpoint_rmse:.1f} cm) should be at least twice as accurate as the "
                f"{target_class_cm:g}-cm class tested (§7.13)"
            )
        if nva and nva.mean_m is not None and nva.rmse_data_m:
            target = target_class_cm / 100 if target_class_cm is not None else nva.rmse_data_m
            if abs(nva.mean_m) > BIAS_FRACTION * target:
                basis = f"the {target_class_cm:g}-cm target RMSE" if target_class_cm is not None else "the RMSE"
                notes.append(
                    f"NVA mean error {100 * nva.mean_m:+.1f} cm exceeds 25 % of {basis} (§7.2): investigate a "
                    "systematic bias (datum, geoid or control)"
                )
        ingest = job.sidecar.ingest
        if ingest is not None and ingest.source_type == "photogrammetry":
            notes.append(
                "Photogrammetric elevation data: §7.6 asks for horizontal accuracy (RMSEH) as well, tested or "
                "'produced to meet'; argus-survey tests vertical accuracy only"
            )
        crs = job.sidecar.crs
        if crs is not None and crs.vertical_assumed:
            notes.append(f"The job's vertical datum was ASSUMED: {crs.vertical_assumed}")

        report = AccuracyReport(
            standard=STANDARD,
            surface=surface,
            sampling="bilinear (gdal raster pixel-info -r bilinear); a point touching a void is excluded",
            checks_file=FileRef(path=str(checks.resolve()), sha256=sha256_file(checks), size=checks.stat().st_size),
            checks_crs=checks_crs,
            transform=op,
            checkpoint_rmse_m=checkpoint_rmse,
            groups=groups,
            residuals=residuals,
            target_class_cm=target_class_cm,
            statement=" ".join(statement_parts) or "No accuracy could be computed.",
            notes=notes,
        )
        job.sidecar.accuracy = report
        job.path("accuracy.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        with job.path("accuracy_residuals.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "group", "x", "y", "z_check", "z_surface", "residual", "excluded", "description"])
            for r in residuals:
                w.writerow(
                    [r.id, r.group, r.x, r.y, r.z_check, r.z_surface, r.residual, r.excluded or "", r.description]
                )
        job.add_product("accuracy", "accuracy.json", step=STEP, description="Vertical accuracy (ASPRS ed. 2 v2)")
        job.add_product("residuals", "accuracy_residuals.csv", step=STEP, description="Check point residuals")
        for n in notes:
            job.warn(f"accuracy: {n}")
    return report
