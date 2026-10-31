"""``argus-survey`` — the Typer CLI.

Every command that processes data writes or extends ``<job>/job.json``. Errors a
user can act on (:class:`SurveyError`) print one line and exit 2; tool failures
also print the stderr tail.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console
from rich.table import Table

from argus_survey import __version__, config
from argus_survey.jobs import Job, open_job
from argus_survey.models import CrsRecord, SurveyError
from argus_survey.tools import ToolError

app = typer.Typer(
    name="argus-survey",
    help="Point-cloud toolkit for land surveyors: QA, ground, DTM/DSM, contours, volumes, change and accuracy, "
    "with every datum, transform and command recorded.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

EXIT_REFUSED = 2


def _configure_logging(verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"argus-survey {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log every tool call")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Print the version")
    ] = False,
) -> None:
    _configure_logging(verbose)


def fail(exc: SurveyError) -> typer.Exit:
    err.print(f"[bold red]error:[/] {exc}")
    if isinstance(exc, ToolError) and exc.stderr_tail:
        err.print(f"[dim]{exc.stderr_tail}[/]")
    return typer.Exit(EXIT_REFUSED)


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Exit 1 if any required check fails")] = False,
    no_digests: Annotated[bool, typer.Option("--no-digests", help="Skip re-hashing the datum grids")] = False,
) -> None:
    """Check PDAL/GDAL/PROJ: versions, drivers, datum grids, and the transformations surveyors rely on."""
    from argus_survey.tools import doctor as run_doctor

    report = run_doctor(verify_grid_digests=not no_digests)
    if as_json:
        print(report.model_dump_json(indent=2))
    else:
        v = report.versions
        console.print(f"argus-survey {__version__} · PDAL {v.pdal} · GDAL {v.gdal} · PROJ {v.proj}")
        table = Table(show_header=True, header_style="bold")
        table.add_column("check")
        table.add_column("")
        table.add_column("detail", overflow="fold")
        for c in report.checks:
            mark = "[green]ok[/]" if c.ok else ("[red]FAIL[/]" if c.required else "[yellow]warn[/]")
            table.add_row(c.name, mark, c.detail)
        console.print(table)
        console.print("[green]toolchain ok[/]" if report.ok else "[red]toolchain incomplete[/]")
    if strict and not report.ok:
        raise typer.Exit(1)


# ── shared options ──
JobOpt = Annotated[str | None, typer.Option("--job", "-j", help="Job id under SURVEY_JOBS_DIR (created on first use)")]
OutOpt = Annotated[Path | None, typer.Option("--out", help="Job directory (its name is the job id)")]


def _job(job_id: str | None, out: Path | None, *, create: bool = False) -> Job:
    return open_job(job_id, out, jobs_root=config.jobs_dir(), create=create)


def _print_crs(record: CrsRecord | None, title: str = "CRS") -> None:
    if record is None:
        console.print(f"[bold]{title}:[/] [red]none declared[/]")
        return
    table = Table(title=title, show_header=False, title_justify="left")
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    table.add_row("name", record.name)
    table.add_row("definition", record.definition if len(record.definition) < 120 else "(WKT/PROJJSON)")
    h = record.horizontal
    table.add_row("horizontal", f"{h.name}" + (f" (EPSG:{h.epsg})" if h.epsg else "") + f", {h.units}")
    if record.vertical is None:
        table.add_row("vertical", "[red]none — height steps will refuse without --assume-vertical[/]")
    else:
        v = record.vertical
        table.add_row(
            "vertical",
            f"{v.name}" + (f" (EPSG:{v.epsg})" if v.epsg else "") + f", {v.units} [{record.height_reference}]",
        )
    if record.geoid:
        table.add_row("geoid", record.geoid)
    if record.vertical_assumed:
        table.add_row("[red]ASSUMED[/]", f"[red]{record.vertical_assumed}[/]")
    if record.epoch is not None:
        table.add_row("epoch", str(record.epoch))
    console.print(table)


@app.command()
def info(
    path: Annotated[Path, typer.Argument(help="LAS/LAZ/COPC file", exists=True, dir_okay=False)],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    """Header summary and CRS record of a point cloud (no job is created)."""
    from argus_survey.ingest import info_report

    try:
        report = info_report(path)
    except SurveyError as exc:
        raise fail(exc) from exc
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return
    s = report["summary"]
    b = s["bounds"]
    console.print(
        f"[bold]{path.name}[/] · {s['count']:,} points · LAS {s['las_version']} format {s['point_format']}"
        + (" · COPC" if s["copc"] else "")
    )
    console.print(
        f"bounds  x {b['minx']:.3f} … {b['maxx']:.3f}   y {b['miny']:.3f} … {b['maxy']:.3f}   "
        f"z {b['minz']:.3f} … {b['maxz']:.3f}"
    )
    _print_crs(CrsRecord.model_validate(report["crs"]) if report["crs"] else None)


@app.command()
def ingest(
    inputs: Annotated[list[Path], typer.Argument(help="LAS/LAZ files or directories of them")],
    job: JobOpt = None,
    out: OutOpt = None,
    t_srs: Annotated[
        str | None, typer.Option("--t-srs", help="Target CRS, e.g. EPSG:7850+5711 (MGA2020 z50 + AHD)")
    ] = None,
    s_srs: Annotated[
        str | None, typer.Option("--s-srs", help="Source CRS when the files have none (or a wrong one)")
    ] = None,
    assume_vertical: Annotated[
        str | None,
        typer.Option("--assume-vertical", help="Heights' datum when the input has none: EPSG:<code> or 'ellipsoidal'"),
    ] = None,
    operation: Annotated[
        str | None,
        typer.Option("--operation", help="Pin a transformation by (part of) its name, e.g. 'GDA94 to GDA2020 (2)'"),
    ] = None,
    crop: Annotated[
        Path | None, typer.Option("--crop", help="Polygon file (GeoJSON/GPKG/SHP) to keep points inside")
    ] = None,
    crop_srs: Annotated[str | None, typer.Option("--crop-srs", help="CRS of the crop polygon if it has none")] = None,
    force: Annotated[bool, typer.Option("--force", help="Redo an ingested job (deletes its products)")] = False,
) -> None:
    """Read point clouds, settle and record the CRS, and write one COPC (points.copc.laz)."""
    from argus_survey.ingest import ingest as run_ingest

    try:
        j = _job(job, out, create=True)
        record = run_ingest(
            j,
            inputs,
            t_srs=t_srs,
            s_srs=s_srs,
            assume_vertical=assume_vertical,
            operation=operation,
            crop=crop,
            crop_srs=crop_srs,
            force=force,
        )
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(f"[green]ingested[/] {record.output_count:,} points into job [bold]{j.id}[/] ({j.dir})")
    if record.transform:
        t = record.transform
        console.print(
            f"transformation: {t.name} ({t.accuracy_m} m)" + (f" · grids {', '.join(t.grids)}" if t.grids else "")
        )
    console.print(f"source type: {record.source_type}")
    _print_crs(j.sidecar.crs, "Job CRS")
    for w in j.sidecar.warnings:
        console.print(f"[yellow]warning:[/] {w}")


@app.command()
def synth(
    out: Annotated[Path, typer.Argument(help="Output .las/.laz")],
    epoch: Annotated[int, typer.Option("--epoch", min=1, max=2, help="2: the stockpile has grown 1 m at its apex")] = 1,
    photogrammetry: Annotated[
        bool, typer.Option("--photogrammetry", help="No returns info, RGB, no ground under trees")
    ] = False,
    srs: Annotated[str, typer.Option("--srs", help="CRS written to the file, or 'none'")] = "EPSG:7850+5711",
    checks: Annotated[Path | None, typer.Option("--checks", help="Also write check points CSV here")] = None,
) -> None:
    """Write a synthetic survey scene with known answers (cone stockpile, building, trees) — for demos and tests."""
    from argus_survey import synthetic

    scene = synthetic.Scene(photogrammetry=photogrammetry)
    if epoch == 2:
        scene = synthetic.epoch2(scene)
    try:
        synthetic.write_scene(scene, out, srs=None if srs.lower() == "none" else srs)
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(f"wrote {out} · stockpile cone volume {scene.cone_volume:.2f} m³ above the ground plane")
    if checks is not None:
        synthetic.write_check_points(synthetic.check_points(scene), checks)
        console.print(f"wrote {checks}")


WorkersOpt = Annotated[
    int | None, typer.Option("--workers", min=1, help="Parallel tiles (default: SURVEY_WORKERS or ≤4)")
]


def _warnings(j: Job, before: int) -> None:
    for w in j.sidecar.warnings[before:]:
        console.print(f"[yellow]warning:[/] {w}")


@app.command()
def qa(
    job: JobOpt = None,
    out: OutOpt = None,
    density_res: Annotated[float, typer.Option("--density-res", help="Density grid cell size (m)")] = 1.0,
    min_density: Annotated[
        float | None, typer.Option("--min-density", help="Gap threshold in pts/m² (default: 25% of the median)")
    ] = None,
    workers: WorkersOpt = None,
) -> None:
    """Point count, density, class/return histograms, outliers, coverage boundary and gaps (qa.json)."""
    from argus_survey.qa import qa as run_qa

    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_qa(j, density_res=density_res, min_density=min_density, workers=workers)
    except SurveyError as exc:
        raise fail(exc) from exc
    d = r.density
    console.print(
        f"[green]qa[/] {r.points:,} points over {r.coverage_area_m2:,.0f} m² · "
        f"density p5/p50/p95 {d.p5:.1f}/{d.p50:.1f}/{d.p95:.1f} pts/m² · "
        f"outliers {r.outliers.count:,} ({r.outliers.percent:.2f}%) · gaps {r.gaps.area_m2:,.1f} m²"
    )
    _warnings(j, n)


@app.command()
def ground(
    job: JobOpt = None,
    out: OutOpt = None,
    method: Annotated[str, typer.Option("--method", help="smrf (default) or csf")] = "smrf",
    slope: Annotated[float | None, typer.Option(help="SMRF slope (rise/run)")] = None,
    window: Annotated[float | None, typer.Option(help="SMRF max window (m)")] = None,
    threshold: Annotated[float | None, typer.Option(help="SMRF/CSF elevation threshold (m)")] = None,
    scalar: Annotated[float | None, typer.Option(help="SMRF elevation scalar")] = None,
    cell: Annotated[float | None, typer.Option(help="SMRF cell size (m)")] = None,
    resolution: Annotated[float | None, typer.Option(help="CSF cloth resolution (m)")] = None,
    rigidness: Annotated[int | None, typer.Option(help="CSF rigidness (1-3)")] = None,
    keep_classes: Annotated[bool, typer.Option("--keep-classes", help="Don't reset existing classes first")] = False,
    workers: WorkersOpt = None,
) -> None:
    """Classify ground (SMRF default, CSF option) per buffered tile; every point is kept (ground.copc.laz)."""
    from argus_survey.ground import ground as run_ground

    if method not in {"smrf", "csf"}:
        raise fail(SurveyError("--method must be smrf or csf"))
    if method == "smrf":
        params = {"slope": slope, "window": window, "threshold": threshold, "scalar": scalar, "cell": cell}
    else:
        params = {"resolution": resolution, "threshold": threshold, "rigidness": rigidness}
    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_ground(j, method=method, params=params, keep_classes=keep_classes, workers=workers)  # type: ignore[arg-type]
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(
        f"[green]ground[/] {r.method.upper()} · {r.ground_points:,} ground points ({r.ground_percent:.1f}%) · "
        f"ground density {r.ground_density:.2f} pts/m² (spacing {r.ground_spacing:.2f} m) · "
        f"noise {r.noise_points:,} · {r.tiles} tile(s)"
    )
    _warnings(j, n)


@app.command()
def surfaces(
    job: JobOpt = None,
    out: OutOpt = None,
    res: Annotated[float, typer.Option("--res", help="Cell size (m)")] = 0.5,
    dtm: Annotated[bool, typer.Option("--dtm/--no-dtm")] = True,
    dsm: Annotated[bool, typer.Option("--dsm/--no-dsm")] = True,
    chm: Annotated[bool, typer.Option("--chm/--no-chm")] = True,
    fill_voids: Annotated[
        int | None, typer.Option("--fill-voids", min=1, help="Fill DTM voids up to N cells across (recorded)")
    ] = None,
    workers: WorkersOpt = None,
) -> None:
    """DTM (ground IDW), DSM (max), CHM (height above ground) on a snapped grid, with void masks and hillshade."""
    from argus_survey.surfaces import surfaces as run_surfaces

    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_surfaces(j, res=res, dtm=dtm, dsm=dsm, chm=chm, fill_voids=fill_voids, workers=workers)
    except SurveyError as exc:
        raise fail(exc) from exc
    g = r.grid
    console.print(f"[green]surfaces[/] {g.width}×{g.height} cells at {g.res} m, origin ({g.origin_x}, {g.origin_y})")
    for kind, s in r.surfaces.items():
        fill = f" → {s.void_percent_after_fill:.2f}% after fill" if s.void_percent_after_fill is not None else ""
        console.print(
            f"  {kind.upper()}: {s.stats.minimum:.3f} … {s.stats.maximum:.3f} m · voids {s.void_percent:.2f}%"
            f"{fill} · {s.method}"
        )
    _warnings(j, n)


@app.command()
def contours(
    job: JobOpt = None,
    out: OutOpt = None,
    interval: Annotated[float, typer.Option("--interval", help="Contour interval (m)")] = 0.5,
    index: Annotated[int, typer.Option("--index", help="Every N-th contour is an index contour (0: none)")] = 5,
) -> None:
    """Contours from the DTM (contours.gpkg + contours.geojson, index contours flagged)."""
    from argus_survey.contours import contours as run_contours

    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_contours(j, interval=interval, index=index or None)
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(f"[green]contours[/] {r.features} lines every {r.interval} m from {r.min_elev} to {r.max_elev} m")
    _warnings(j, n)


@app.command()
def run(
    inputs: Annotated[list[Path], typer.Argument(help="LAS/LAZ files or directories")],
    job: JobOpt = None,
    out: OutOpt = None,
    res: Annotated[float, typer.Option("--res", help="Surface cell size (m)")] = 0.5,
    interval: Annotated[float, typer.Option("--interval", help="Contour interval (m)")] = 0.5,
    t_srs: Annotated[str | None, typer.Option("--t-srs", help="Target CRS, e.g. EPSG:7850+5711")] = None,
    s_srs: Annotated[str | None, typer.Option("--s-srs", help="Source CRS when the files have none")] = None,
    assume_vertical: Annotated[
        str | None, typer.Option("--assume-vertical", help="EPSG:<code> or 'ellipsoidal'")
    ] = None,
    operation: Annotated[str | None, typer.Option("--operation", help="Pin a transformation by name")] = None,
    crop: Annotated[Path | None, typer.Option("--crop", help="Polygon to keep points inside")] = None,
    crop_srs: Annotated[str | None, typer.Option("--crop-srs", help="CRS of the crop polygon if it has none")] = None,
    method: Annotated[str, typer.Option("--method", help="Ground filter: smrf or csf")] = "smrf",
    fill_voids: Annotated[int | None, typer.Option("--fill-voids", min=1, help="Fill DTM voids up to N cells")] = None,
    workers: WorkersOpt = None,
) -> None:
    """ingest → qa → ground → surfaces → contours → report, in one job."""
    from argus_survey.pipeline import run_all

    try:
        j = _job(job, out, create=True)
        run_all(
            j,
            inputs,
            res=res,
            interval=interval,
            t_srs=t_srs,
            s_srs=s_srs,
            assume_vertical=assume_vertical,
            operation=operation,
            crop=crop,
            crop_srs=crop_srs,
            method=method,
            fill_voids=fill_voids,
            workers=workers,
            echo=console.print,
        )
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(f"[bold green]done[/] job [bold]{j.id}[/] · {len(j.sidecar.products)} products in {j.dir}")
    _warnings(j, 0)


if __name__ == "__main__":  # pragma: no cover
    app()
