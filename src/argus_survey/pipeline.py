"""``run``: the whole chain for one job, step by step, each recorded as if run by hand."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from argus_survey.contours import contours
from argus_survey.ground import filter_params, ground
from argus_survey.ingest import ingest
from argus_survey.jobs import Job
from argus_survey.qa import qa
from argus_survey.report import report
from argus_survey.surfaces import surfaces


def run_all(
    job: Job,
    inputs: list[Path],
    *,
    res: float = 0.5,
    interval: float = 0.5,
    t_srs: str | None = None,
    s_srs: str | None = None,
    assume_vertical: str | None = None,
    operation: str | None = None,
    crop: Path | None = None,
    crop_srs: str | None = None,
    method: str = "smrf",
    preset: str | None = None,
    fill_voids: int | None = None,
    workers: int | None = None,
    echo: Callable[[str], Any] = print,
) -> Job:
    filter_params(method, preset=preset)  # refuse a bad method/preset before the ingest, not after it
    rec = ingest(
        job,
        inputs,
        t_srs=t_srs,
        s_srs=s_srs,
        assume_vertical=assume_vertical,
        operation=operation,
        crop=crop,
        crop_srs=crop_srs,
    )
    echo(f"ingest   {rec.output_count:,} points · {job.sidecar.crs.name if job.sidecar.crs else '?'}")
    q = qa(job, workers=workers)
    echo(f"qa       {q.density.p50:.1f} pts/m² median · {q.coverage_area_m2:,.0f} m² covered")
    g = ground(job, method=method, preset=preset, workers=workers)  # type: ignore[arg-type]
    echo(f"ground   {g.ground_percent:.1f}% ground · spacing {g.ground_spacing:.2f} m")
    if job.sidecar.crs is not None and job.sidecar.crs.has_height_datum:
        s = surfaces(job, res=res, fill_voids=fill_voids, workers=workers)
        echo("surfaces " + ", ".join(f"{k.upper()} {v.void_percent:.1f}% voids" for k, v in s.surfaces.items()))
        c = contours(job, interval=interval)
        echo(f"contours {c.features} lines at {c.interval} m")
    else:
        job.warn("No vertical datum: surfaces and contours were skipped (re-run with --assume-vertical if known)")
    path = report(job)
    echo(f"report   {path}")
    return job
