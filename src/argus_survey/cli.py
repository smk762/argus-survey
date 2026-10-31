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
from argus_survey.models import CrsRecord, SurveyError, VolumeReport
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
    dsm_method: Annotated[
        str, typer.Option("--dsm-method", help="idw (unbiased, for volumes) or max (roof edges/canopy)")
    ] = "idw",
    workers: WorkersOpt = None,
) -> None:
    """DTM (ground IDW), DSM (top-surface IDW or max), CHM (height above ground) on a snapped grid."""
    from argus_survey.surfaces import surfaces as run_surfaces

    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_surfaces(
            j, res=res, dtm=dtm, dsm=dsm, chm=chm, fill_voids=fill_voids, dsm_method=dsm_method, workers=workers
        )
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


def _print_volumes(r: VolumeReport) -> None:
    labels = {"stockpile": ("above", "below"), "cutfill": ("fill", "cut"), "change": ("gain", "loss")}[r.kind]
    table = Table(title=f"{r.kind} · {r.surface} · {r.res} m cells", title_justify="left")
    for col in ("zone", "area m²", f"{labels[0]} m³", f"{labels[1]} m³", "net m³", "voids %"):
        table.add_column(col, justify="right" if col != "zone" else "left")
    for z in r.zones:
        table.add_row(
            z.name,
            f"{z.area_m2:,.2f}",
            f"{z.above_m3:,.2f}",
            f"{z.below_m3:,.2f}",
            f"{z.net_m3:,.2f}",
            f"{z.void_percent:.2f}",
        )
    console.print(table)
    console.print(f"[dim]{r.sign_convention}[/]")
    for z in r.zones:
        console.print(
            f"[dim]{z.name}: base {z.base}"
            + (f" (fit RMS {z.base_fit_rms_m} m)" if z.base_fit_rms_m is not None else "")
            + "[/]"
        )


@app.command()
def volume(
    job: JobOpt = None,
    out: OutOpt = None,
    polygon: Annotated[Path, typer.Option("--polygon", help="Stockpile polygon(s) (GeoJSON/GPKG/SHP)")] = ...,  # type: ignore[assignment]
    base: Annotated[
        str, typer.Option("--base", help="plane | lowest-perimeter | surface:<raster on the job grid>")
    ] = "plane",
    surface: Annotated[str, typer.Option("--surface", help="dtm or dsm (the top of the pile)")] = "dtm",
    polygon_srs: Annotated[str | None, typer.Option("--polygon-srs", help="CRS of the polygon if it has none")] = None,
    allow_voids: Annotated[bool, typer.Option("--allow-voids", help="Accept >5% voids (counted as zero)")] = False,
    resample: Annotated[bool, typer.Option("--resample", help="Warp a misaligned base surface (recorded)")] = False,
    base_vertical: Annotated[
        str | None, typer.Option("--base-vertical", help="Vertical datum of a base surface that declares none")
    ] = None,
) -> None:
    """Stockpile volumes over polygons against a plane, lowest-perimeter or surface base (volume.json)."""
    from argus_survey.volume import volume as run_volume

    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_volume(
            j,
            polygon,
            base=base,
            surface=surface,
            polygon_srs=polygon_srs,
            allow_voids=allow_voids,
            resample=resample,
            base_vertical=base_vertical,
        )
    except SurveyError as exc:
        raise fail(exc) from exc
    _print_volumes(r)
    _warnings(j, n)


@app.command()
def cutfill(
    job: JobOpt = None,
    out: OutOpt = None,
    design: Annotated[Path, typer.Option("--design", help="Design surface GeoTIFF in the job CRS")] = ...,  # type: ignore[assignment]
    polygon: Annotated[
        Path | None, typer.Option("--polygon", help="Limit to polygon(s); default the design extent")
    ] = None,
    polygon_srs: Annotated[str | None, typer.Option("--polygon-srs")] = None,
    surface: Annotated[str, typer.Option("--surface", help="Existing surface: dtm or dsm")] = "dtm",
    design_vertical: Annotated[
        str | None, typer.Option("--design-vertical", help="Design heights' datum if the file declares none")
    ] = None,
    resample: Annotated[bool, typer.Option("--resample", help="Warp a misaligned design (bilinear, recorded)")] = False,
    allow_voids: Annotated[bool, typer.Option("--allow-voids")] = False,
) -> None:
    """Cut and fill between the existing surface and a design surface (cutfill.json)."""
    from argus_survey.volume import cutfill as run_cutfill

    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_cutfill(
            j,
            design,
            polygon=polygon,
            polygon_srs=polygon_srs,
            surface=surface,
            design_vertical=design_vertical,
            resample=resample,
            allow_voids=allow_voids,
        )
    except SurveyError as exc:
        raise fail(exc) from exc
    _print_volumes(r)
    _warnings(j, n)


@app.command()
def change(
    job: JobOpt = None,
    out: OutOpt = None,
    against: Annotated[str, typer.Option("--against", help="Earlier epoch's job id (same jobs dir)")] = ...,  # type: ignore[assignment]
    lod: Annotated[float | None, typer.Option("--lod", help="Level of detection (m); default 0.10")] = None,
    lod_from_accuracy: Annotated[
        bool, typer.Option("--lod-from-accuracy", help="LoD95 = 1.96·√(σ₁²+σ₂²) from both epochs' NVA RMSE")
    ] = False,
    surface: Annotated[str, typer.Option("--surface", help="dtm or dsm")] = "dtm",
    polygon: Annotated[Path | None, typer.Option("--polygon", help="Only within these polygon(s)")] = None,
    polygon_srs: Annotated[str | None, typer.Option("--polygon-srs")] = None,
) -> None:
    """DEM of difference (this job − --against) with a level of detection (change.json, dod.tif)."""
    from argus_survey.change import change as run_change
    from argus_survey.jobs import JobStore

    try:
        j = _job(job, out)
        other = JobStore(j.dir.parent).open(against)
        n = len(j.sidecar.warnings)
        r = run_change(
            j,
            other,
            surface=surface,
            lod=lod,
            lod_from_accuracy=lod_from_accuracy,
            polygon=polygon,
            polygon_srs=polygon_srs,
        )
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(f"[green]change[/] {j.id} − {r.against_job} ({r.surface.upper()}, LoD {r.lod_m} m: {r.lod_rule})")
    console.print(
        f"  gain {r.gain_m3:,.2f} m³ over {r.gain_area_m2:,.1f} m² · loss {r.loss_m3:,.2f} m³ over "
        f"{r.loss_area_m2:,.1f} m² · net {r.net_m3:+,.2f} m³ · voids {r.void_percent:.2f}%"
    )
    _warnings(j, n)


@app.command()
def accuracy(
    job: JobOpt = None,
    out: OutOpt = None,
    checks: Annotated[Path, typer.Option("--checks", help="Check points CSV: id,x,y,z[,description]")] = ...,  # type: ignore[assignment]
    cols: Annotated[str, typer.Option("--cols", help="Column names for id,x,y,z")] = "id,x,y,z",
    checks_crs: Annotated[
        str | None, typer.Option("--crs", help="Check points' compound CRS, e.g. EPSG:7850+5711")
    ] = None,
    checkpoint_rmse: Annotated[
        float | None, typer.Option("--checkpoint-rmse", help="Vertical RMSE of the check point survey (m)")
    ] = None,
    target_class: Annotated[
        float | None, typer.Option("--class-cm", help="ASPRS vertical accuracy class to test against (cm RMSEV)")
    ] = None,
    surface: Annotated[str, typer.Option("--surface", help="Surface to test (dtm)")] = "dtm",
) -> None:
    """Vertical accuracy against check points (ASPRS ed. 2: NVA pass/fail, VVA reported) → accuracy.json."""
    from argus_survey.accuracy import accuracy as run_accuracy

    names = tuple(c.strip() for c in cols.split(","))
    if len(names) != 4:
        raise fail(SurveyError("--cols needs four names: id,x,y,z"))
    try:
        j = _job(job, out)
        n = len(j.sidecar.warnings)
        r = run_accuracy(
            j,
            checks,
            checks_crs=checks_crs,
            cols=names,
            checkpoint_rmse=checkpoint_rmse,  # type: ignore[arg-type]
            surface=surface,
            target_class_cm=target_class,
        )
    except SurveyError as exc:
        raise fail(exc) from exc
    table = Table(title=f"{r.standard} · {r.surface.upper()}", title_justify="left")
    for col in ("group", "n", "excluded", "mean m", "std m", "RMSE m", "max |e| m", "≥30"):
        table.add_column(col, justify="right")
    for g in r.groups:
        fmt = lambda v: "—" if v is None else f"{v:.3f}"  # noqa: E731
        table.add_row(
            g.group,
            str(g.n),
            str(g.excluded),
            fmt(g.mean_m),
            fmt(g.std_m),
            fmt(g.rmse_m),
            fmt(g.max_abs_m),
            "yes" if g.meets_minimum_count else "[red]no[/]",
        )
    console.print(table)
    console.print(r.statement)
    _warnings(j, n)


@app.command()
def profile(
    job: JobOpt = None,
    out: OutOpt = None,
    line: Annotated[Path, typer.Option("--line", help="Line file (GeoJSON/GPKG/SHP)")] = ...,  # type: ignore[assignment]
    step: Annotated[float, typer.Option("--step", help="Sample spacing along the line (m)")] = 0.5,
    line_srs: Annotated[str | None, typer.Option("--line-srs", help="CRS of the line if it has none")] = None,
) -> None:
    """Sample DTM/DSM along a polyline → profile.csv + profile.svg."""
    from argus_survey.profile import profile as run_profile

    try:
        j = _job(job, out)
        r = run_profile(j, line, step=step, line_srs=line_srs)
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(
        f"[green]profile[/] {r.line}: {r.length_m:,.1f} m, {r.samples} samples of "
        f"{', '.join(s.upper() for s in r.surfaces)} → {j.path('profile.svg')}"
    )


@app.command()
def report(job: JobOpt = None, out: OutOpt = None) -> None:
    """Write the self-contained, printable report.html for a job."""
    from argus_survey.report import report as run_report

    try:
        path = run_report(_job(job, out))
    except SurveyError as exc:
        raise fail(exc) from exc
    console.print(f"[green]report[/] {path}")


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p")] = config.DEFAULT_PORT,
    cors: Annotated[bool, typer.Option("--cors", help="Enable CORS for $SURVEY_CORS_ORIGINS")] = False,
    allow_process: Annotated[
        bool | None,
        typer.Option(
            "--allow-process/--read-only", help="Enable POST processing routes (default: $SURVEY_ALLOW_PROCESS)"
        ),
    ] = None,
) -> None:
    """Start the API and viewer on :8107 (read-only unless processing is allowed)."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise fail(SurveyError("the server needs: pip install argus-survey[server]")) from exc
    from argus_survey.server import create_app

    _configure_logging(True)
    allowed = config.process_allowed(allow_process)
    console.print(
        f"argus-survey {__version__} on http://{host}:{port}/viewer · "
        + ("[yellow]processing ENABLED[/]" if allowed else "read-only")
    )
    uvicorn.run(create_app(cors=cors, allow_process=allowed), host=host, port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    app()
