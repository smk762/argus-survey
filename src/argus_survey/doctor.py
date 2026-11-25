"""``doctor``: check the toolchain, the datum grids and the key transformations.

It sits above :mod:`argus_survey.tools` (the runner and its probes) and :mod:`argus_survey.crs`, which uses it.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence

from argus_survey.crs import select_operation
from argus_survey.models import DoctorCheck, DoctorReport, SurveyError
from argus_survey.tools import (
    GRID_MANIFEST,
    OPTIONAL_GRIDS,
    REQUIRED_EXECUTABLES,
    REQUIRED_GDAL_ALGORITHMS,
    REQUIRED_GDAL_DRIVERS,
    REQUIRED_GRIDS,
    REQUIRED_PDAL_DRIVERS,
    find_grid,
    gdal_algorithm_ok,
    gdal_drivers,
    pdal_drivers,
    proj_search_paths,
    read_grid_manifest,
    sha256_file,
    versions,
)


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
    report = DoctorReport(versions=versions())
    checks = report.checks

    for exe in REQUIRED_EXECUTABLES:
        path = shutil.which(exe)
        checks.append(DoctorCheck(name=f"exe:{exe}", ok=path is not None, detail=path or "not on PATH"))
    for tool, value in report.versions.model_dump().items():
        checks.append(DoctorCheck(name=f"version:{tool}", ok=value is not None, detail=value or "unknown"))

    checks.append(_missing_check("pdal:drivers", REQUIRED_PDAL_DRIVERS, pdal_drivers()))
    checks.append(_missing_check("gdal:drivers", REQUIRED_GDAL_DRIVERS, gdal_drivers()))
    algs = {a for a in REQUIRED_GDAL_ALGORITHMS if gdal_algorithm_ok(a)}
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
