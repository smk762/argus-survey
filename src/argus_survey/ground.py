"""Ground classification: outlier flagging then SMRF (default) or CSF, per buffered tile.

Every point is kept — classification changes, nothing is dropped (SPEC §6.3).
By default existing classes are reset to 1 (unclassified) first, so the result
doesn't mix a vendor's ground with ours; ``keep_classes=True`` skips that.
Statistical outliers become class 7 (low point / noise) and are ignored by the
ground filter. Photogrammetry clouds carry ``ReturnNumber = 0``, which LAS 1.4
doesn't allow and which SMRF's return filter would reject wholesale, so their
returns are normalised to 1-of-1 (recorded).

The ground density reported here bounds the DTM resolution: a cell with fewer
than ~1 ground point on average is interpolated, not measured.
"""

from __future__ import annotations

import math
from typing import Any, Literal

import structlog

from argus_survey import tiling
from argus_survey.ingest import parse_stats
from argus_survey.jobs import Job
from argus_survey.models import Bounds3D, GroundReport, SurveyError
from argus_survey.qa import OUTLIER_PARAMS
from argus_survey.tools import pdal_pipeline

log = structlog.get_logger(__name__)

STEP = "ground"
CLASSIFIED_NAME = "ground.copc.laz"

SMRF_DEFAULTS: dict[str, Any] = {"slope": 0.15, "window": 18.0, "threshold": 0.5, "scalar": 1.25, "cell": 1.0}
CSF_DEFAULTS: dict[str, Any] = {
    "resolution": 1.0,
    "threshold": 0.5,
    "rigidness": 3,
    "smooth": True,
    "step": 0.65,
    "iterations": 500,
}


def ground(
    job: Job,
    *,
    method: Literal["smrf", "csf"] = "smrf",
    params: dict[str, Any] | None = None,
    keep_classes: bool = False,
    workers: int | None = None,
) -> GroundReport:
    job.require_crs(STEP, height=False)
    copc = job.product_path("copc", needed_by=STEP)
    ingest = job.sidecar.ingest
    assert ingest is not None
    defaults = SMRF_DEFAULTS if method == "smrf" else CSF_DEFAULTS
    unknown = set(params or {}) - set(defaults)
    if unknown:
        raise SurveyError(f"unknown {method} parameter(s): {', '.join(sorted(unknown))}")
    chosen = {**defaults, **{k: v for k, v in (params or {}).items() if v is not None}}

    rn = next((s for s in ingest.stats if s.name == "ReturnNumber"), None)
    normalise = rn is not None and rn.maximum == 0
    bounds = Bounds3D(**job.sidecar.products["copc"].meta["bounds"])
    total = ingest.output_count
    # the filter must see a full window of terrain past the tile edge
    buffer = max(25.0, 1.5 * float(chosen.get("window", chosen.get("resolution", 1.0) * 20)))
    plan = tiling.plan(bounds, total, buffer=buffer)

    def middle(_tile: tiling.Tile) -> list[dict[str, Any]]:
        stages: list[dict[str, Any]] = []
        assign: list[str] = []
        if not keep_classes:
            assign.append("Classification = 1")
        if normalise:
            assign += ["ReturnNumber = 1", "NumberOfReturns = 1"]
        if assign:
            stages.append({"type": "filters.assign", "value": assign})
        stages.append({"type": "filters.outlier", **OUTLIER_PARAMS})
        if method == "smrf":
            stages.append({"type": "filters.smrf", "ignore": "Classification[7:7]", "returns": "last,only", **chosen})
        else:
            stages.append({"type": "filters.csf", "ignore": "Classification[7:7]", **chosen})
        return stages

    with job.step(STEP):
        out = job.path(CLASSIFIED_NAME)
        tiling.run_tiles(job, copc, plan, middle, step=STEP, out=out, expected_count=total, workers=workers)
        meta = pdal_pipeline(
            [
                {"type": "readers.copc", "filename": str(out)},
                {"type": "filters.stats", "dimensions": "Classification", "count": "Classification"},
            ],
            step=STEP,
            sink=job.sink,
            stream=True,
        )
        cls = next((s for s in parse_stats(meta) if s.name == "Classification"), None)
        counts = cls.counts if cls else {}
        n_ground = counts.get("2", 0)
        n_noise = counts.get("7", 0)
        if n_ground == 0:
            raise SurveyError(f"{method} found no ground points — check the parameters (and the data's units)")
        area = job.sidecar.qa.coverage_area_m2 if job.sidecar.qa else bounds.area
        g_density = n_ground / area if area > 0 else math.nan
        report = GroundReport(
            method=method,
            params=chosen,
            outlier_params=OUTLIER_PARAMS,
            points=total,
            ground_points=n_ground,
            ground_percent=round(100.0 * n_ground / total, 3),
            noise_points=n_noise,
            ground_density=round(g_density, 3),
            ground_spacing=round(1.0 / math.sqrt(g_density), 3) if g_density > 0 else math.inf,
            reset_classes=not keep_classes,
            normalised_returns=normalise,
            tiles=len(plan.tiles),
            tile_size=round(plan.size, 3),
            tile_buffer=buffer,
        )
        job.sidecar.ground = report
        job.add_product(
            "classified", CLASSIFIED_NAME, step=STEP, description=f"Point cloud classified by {method.upper()} (COPC)"
        )
        if normalise:
            job.warn("Photogrammetry cloud: ReturnNumber/NumberOfReturns were 0 and are written as 1-of-1")
        if not job.sidecar.qa:
            job.warn("Ground density uses the bounding-box area because QA has not run; run `qa` for coverage area")
    return report
