"""The one subprocess runner, and the checks ``doctor`` runs on the toolchain.

Every PDAL, GDAL and PROJ call in the package goes through :func:`run`. It logs
(structlog), times, captures stderr, hands a :class:`CommandRecord` to the
caller's sink (the job sidecar, in run order) and raises :class:`ToolError` with
the argv and the stderr tail on a non-zero exit. Failed commands are recorded
too: the sidecar shows what was attempted, not only what worked.

The tools are system binaries (conda-forge in the image, see ``env/geo.yml``),
never Python bindings. ``PROJ_NETWORK=OFF`` is forced on every child process,
so PROJ can never fetch a grid mid-job; grids are provisioned up front and
:mod:`argus_survey.crs` refuses an operation whose grid is absent.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import structlog

from argus_survey.models import CommandRecord, DoctorCheck, DoctorReport, SurveyError, ToolVersions, utcnow

log = structlog.get_logger(__name__)

CommandSink = Callable[[CommandRecord], None]

STDERR_TAIL = 4000

# Executables the package calls. `gdal` is the unified GDAL CLI (>= 3.11): calc,
# zonal-stats, pixel-info and fill-nodata come from it, so no Python bindings are needed.
REQUIRED_EXECUTABLES: tuple[str, ...] = (
    "pdal",
    "gdal",
    "gdalinfo",
    "gdal_translate",
    "gdalwarp",
    "gdaldem",
    "gdal_contour",
    "gdaltransform",
    "ogr2ogr",
    "projinfo",
    "cs2cs",
)

REQUIRED_PDAL_DRIVERS: tuple[str, ...] = (
    "readers.las",
    "readers.copc",
    "readers.text",
    "writers.las",
    "writers.copc",
    "writers.gdal",
    "filters.projpipeline",
    "filters.reprojection",
    "filters.crop",
    "filters.range",
    "filters.expression",
    "filters.assign",
    "filters.outlier",
    "filters.smrf",
    "filters.csf",
    "filters.hag_nn",
    "filters.hexbin",
    "filters.stats",
    "filters.splitter",
    "filters.merge",
)

REQUIRED_GDAL_DRIVERS: tuple[str, ...] = ("GTiff", "COG", "PNG", "GPKG", "GeoJSON", "CSV")

# `gdal raster <alg>` subcommands the package relies on.
REQUIRED_GDAL_ALGORITHMS: tuple[str, ...] = ("calc", "zonal-stats", "pixel-info", "fill-nodata", "polygonize", "edit")

# Datum grids the image provisions (env/proj-grids.sha256). Required ones gate `doctor --strict`.
GRID_MANIFEST = "argus-survey-grids.sha256"
REQUIRED_GRIDS: tuple[str, ...] = (
    "au_ga_AUSGeoid2020_20180201.tif",  # GDA2020 ellipsoidal <-> AHD
    "au_ga_AUSGeoid09_V1.01.tif",  # GDA94 ellipsoidal <-> AHD (legacy WA data)
    "au_icsm_GDA94_GDA2020_conformal_and_distortion.tif",  # GDA94 -> GDA2020 (ICSM grid)
)
OPTIONAL_GRIDS: tuple[str, ...] = (
    "au_ga_AGQG_20201120.tif",  # GDA2020 <-> AVWS
    "us_noaa_g2018u0.tif",  # NAD83(2011) <-> NAVD88 (GEOID18)
)


class ToolError(SurveyError):
    """An external tool exited non-zero."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr_tail = stderr[-STDERR_TAIL:].strip()
        tail = self.stderr_tail.splitlines()[-1] if self.stderr_tail else "(no stderr)"
        super().__init__(f"{self.argv[0]} exited {returncode}: {tail}")


class ToolMissing(SurveyError):
    """An external tool is not on PATH."""


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PROJ_NETWORK"] = "OFF"
    if extra:
        env.update(extra)
    return env


def run(
    argv: Sequence[str],
    *,
    step: str,
    sink: CommandSink | None = None,
    stdin: str | None = None,
    pipeline: list[dict[str, Any]] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
    fatal_stderr: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run one external command. Records it to *sink* (even when it fails), raises on non-zero exit.

    *fatal_stderr*: substrings that make a zero exit a failure. PDAL, for one, logs per-point
    reprojection errors and still exits 0 — a silent datum failure we refuse to accept.
    """
    argv = [str(a) for a in argv]
    started_at = utcnow()
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            env=child_env(env),
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolMissing(f"{argv[0]} is not on PATH — run `argus-survey doctor`") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(argv, -1, f"timed out after {timeout}s") from exc
    seconds = round(time.perf_counter() - t0, 3)
    record = CommandRecord(
        step=step,
        tool=Path(argv[0]).name,
        argv=argv,
        pipeline=pipeline,
        started_at=started_at,
        seconds=seconds,
        returncode=proc.returncode,
    )
    if sink is not None:
        sink(record)
    log.info("tool.run", step=step, tool=record.tool, seconds=seconds, returncode=proc.returncode)
    if proc.returncode != 0:
        log.warning("tool.failed", step=step, argv=argv, stderr=proc.stderr[-STDERR_TAIL:])
        raise ToolError(argv, proc.returncode, proc.stderr)
    hits = [pat for pat in fatal_stderr if pat in proc.stderr]
    if hits:
        log.warning("tool.fatal_stderr", step=step, argv=argv, patterns=hits)
        raise ToolError(argv, proc.returncode, f"{proc.stderr}\nrefused: stderr reported {hits[0]!r}")
    return proc


def run_json(argv: Sequence[str], *, step: str, sink: CommandSink | None = None, **kw: Any) -> Any:
    """Run a command whose stdout is JSON and parse it."""
    proc = run(argv, step=step, sink=sink, **kw)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(argv, 0, f"output was not JSON: {proc.stdout[:200]!r}") from exc


def pdal_pipeline(
    stages: list[dict[str, Any]],
    *,
    step: str,
    sink: CommandSink | None = None,
    timeout: float | None = None,
    stream: bool = False,
    fatal_stderr: Sequence[str] = (),
) -> dict[str, Any]:
    """Run a PDAL pipeline (JSON on stdin) and return its output metadata.

    *stream*: run in PDAL's streaming mode (bounded memory; every stage must be streamable).
    """
    with tempfile.TemporaryDirectory(prefix="argus-survey-") as tmp:
        meta = Path(tmp) / "metadata.json"
        argv = ["pdal", "pipeline", "--stdin", "--metadata", str(meta)]
        if stream:
            argv.append("--stream")
        run(
            argv,
            step=step,
            sink=sink,
            stdin=json.dumps({"pipeline": stages}),
            pipeline=stages,
            timeout=timeout,
            fatal_stderr=fatal_stderr,
        )
        if not meta.is_file():
            return {}
        return json.loads(meta.read_text(encoding="utf-8"))


# ── Versions ──────────────────────────────────────────────────────────────────

_PDAL_RE = re.compile(r"pdal (\S+)")
_GDAL_RE = re.compile(r"GDAL (\d[^\s,]*)")
_PROJ_RE = re.compile(r"Rel\. (\d[^\s,]*)")


def parse_version(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def _version_output(argv: list[str], *, stdout_only: bool = False) -> str:
    if shutil.which(argv[0]) is None:
        return ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=child_env(), timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if stdout_only else proc.stdout + proc.stderr


@functools.cache
def versions() -> ToolVersions:
    """PDAL, GDAL and PROJ versions (cached per process; ``None`` for a tool that is absent)."""
    return ToolVersions(
        pdal=parse_version(_PDAL_RE, _version_output(["pdal", "--version"])),
        gdal=parse_version(_GDAL_RE, _version_output(["gdalinfo", "--version"])),
        # projinfo has no --version; with no arguments it prints "Rel. X.Y.Z, <date>" in its usage.
        proj=parse_version(_PROJ_RE, _version_output(["proj"])),
    )


def tools_available() -> bool:
    return all(shutil.which(t) for t in ("pdal", "gdal", "projinfo"))


# ── PROJ search paths and grids ──────────────────────────────────────────────


@functools.cache
def proj_search_paths() -> tuple[Path, ...]:
    out = _version_output(["projinfo", "--searchpaths"], stdout_only=True)
    return tuple(Path(line.strip()) for line in out.splitlines() if line.strip().startswith("/"))


def find_grid(name: str) -> Path | None:
    """The file PROJ would open for grid *name*, or ``None`` if it is in no search path."""
    for root in proj_search_paths():
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def read_grid_manifest(path: Path) -> dict[str, str]:
    """``sha256sum``-format lines -> ``{filename: sha256}``."""
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and not line.startswith("#"):
            rows[parts[1].lstrip("*")] = parts[0]
    return rows


# ── doctor ────────────────────────────────────────────────────────────────────


def _pdal_drivers() -> set[str]:
    out = _version_output(["pdal", "--drivers", "--showjson"], stdout_only=True)
    try:
        return {d["name"] for d in json.loads(out)}
    except (json.JSONDecodeError, KeyError, TypeError):
        return set()


def _gdal_drivers() -> set[str]:
    out = _version_output(["gdal", "--drivers"], stdout_only=True)
    try:
        return {d["short_name"] for d in json.loads(out)}
    except (json.JSONDecodeError, KeyError, TypeError):
        return set()


def _gdal_algorithm_ok(name: str) -> bool:
    if shutil.which("gdal") is None:
        return False
    proc = subprocess.run(
        ["gdal", "raster", name, "--help"], capture_output=True, text=True, env=child_env(), timeout=30, check=False
    )
    return proc.returncode == 0


def _missing_check(name: str, wanted: Sequence[str], have: set[str], required: bool = True) -> DoctorCheck:
    missing = [w for w in wanted if w not in have]
    detail = f"missing: {', '.join(missing)}" if missing else f"{len(wanted)} present"
    return DoctorCheck(name=name, ok=not missing, detail=detail, required=required)


# Transformations a surveyor in the target regions will run; each must resolve to a
# non-ballpark operation whose grids are on disk. (source, target, expected grid, required)
DOCTOR_OPERATIONS: tuple[tuple[str, str, str, bool], ...] = (
    ("EPSG:7843", "EPSG:7850+5711", "au_ga_AUSGeoid2020_20180201.tif", True),  # GDA2020 ellipsoidal -> MGA50 + AHD
    ("EPSG:4939", "EPSG:28350+5711", "au_ga_AUSGeoid09_V1.01.tif", True),  # GDA94 ellipsoidal -> MGA94 z50 + AHD
    ("EPSG:6319", "EPSG:6318+5703", "us_noaa_g2018u0.tif", False),  # NAD83(2011) ellipsoidal -> + NAVD88
)


def doctor(*, verify_grid_digests: bool = True) -> DoctorReport:
    """Check the toolchain: executables, versions, PDAL/GDAL drivers and algorithms, datum grids, key operations."""
    from argus_survey.crs import select_operation  # crs imports tools

    report = DoctorReport(versions=versions())
    checks = report.checks

    for exe in REQUIRED_EXECUTABLES:
        path = shutil.which(exe)
        checks.append(DoctorCheck(name=f"exe:{exe}", ok=path is not None, detail=path or "not on PATH"))
    for tool, value in report.versions.model_dump().items():
        checks.append(DoctorCheck(name=f"version:{tool}", ok=value is not None, detail=value or "unknown"))

    checks.append(_missing_check("pdal:drivers", REQUIRED_PDAL_DRIVERS, _pdal_drivers()))
    checks.append(_missing_check("gdal:drivers", REQUIRED_GDAL_DRIVERS, _gdal_drivers()))
    algs = {a for a in REQUIRED_GDAL_ALGORITHMS if _gdal_algorithm_ok(a)}
    checks.append(_missing_check("gdal:algorithms", REQUIRED_GDAL_ALGORITHMS, algs))

    network = os.environ.get("PROJ_NETWORK", "").upper()
    checks.append(
        DoctorCheck(
            name="proj:network",
            ok=network in {"", "OFF"},
            detail="off (forced off for every child process)" if network in {"", "OFF"} else f"PROJ_NETWORK={network}",
            required=False,
        )
    )

    manifest: dict[str, str] = {}
    for root in proj_search_paths():
        if (root / GRID_MANIFEST).is_file():
            manifest = read_grid_manifest(root / GRID_MANIFEST)
            break
    for grid, required in [(g, True) for g in REQUIRED_GRIDS] + [(g, False) for g in OPTIONAL_GRIDS]:
        path = find_grid(grid)
        if path is None:
            checks.append(DoctorCheck(name=f"grid:{grid}", ok=False, detail="not found", required=required))
            continue
        detail, ok = str(path), True
        if verify_grid_digests and grid in manifest:
            ok = sha256_file(path) == manifest[grid]
            detail += " (sha256 ok)" if ok else " (SHA256 MISMATCH)"
        checks.append(DoctorCheck(name=f"grid:{grid}", ok=ok, detail=detail, required=required))

    for source, target, grid, required in DOCTOR_OPERATIONS:
        name = f"op:{source}->{target}"
        try:
            op = select_operation(source, target)
        except SurveyError as exc:
            checks.append(DoctorCheck(name=name, ok=False, detail=str(exc), required=required))
            continue
        ok = grid in op.grids
        detail = f"{op.name} ({op.accuracy_m} m)" if ok else f"{op.name} does not use {grid}"
        checks.append(DoctorCheck(name=name, ok=ok, detail=detail, required=required))
    return report
