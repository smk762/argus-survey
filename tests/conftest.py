"""Shared fixtures.

Tests marked ``tools`` shell out to PDAL/GDAL/PROJ. They SKIP when the tools are
absent, unless ``SURVEY_TEST_REQUIRE_TOOLS=1`` (CI and the compose ``test``
profile set it), which turns a missing toolchain into a hard error so a runner
without PDAL can never pass silently.
"""

from __future__ import annotations

import os

import pytest

from argus_survey.tools import tools_available

REQUIRE_TOOLS = os.environ.get("SURVEY_TEST_REQUIRE_TOOLS", "").strip() == "1"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "tools: needs pdal/gdal/projinfo on PATH")
    if REQUIRE_TOOLS and not tools_available():
        raise pytest.UsageError("SURVEY_TEST_REQUIRE_TOOLS=1 but pdal/gdal/projinfo are not on PATH")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if tools_available():
        return
    skip = pytest.mark.skip(reason="pdal/gdal/projinfo not on PATH (set SURVEY_TEST_REQUIRE_TOOLS=1 to fail instead)")
    for item in items:
        if "tools" in item.keywords:
            item.add_marker(skip)


# ── synthetic point clouds (generated with PDAL from text; never binary fixtures) ──

from pathlib import Path  # noqa: E402

from argus_survey import synthetic  # noqa: E402
from argus_survey.tools import pdal_pipeline  # noqa: E402


@pytest.fixture(scope="session")
def scene() -> synthetic.Scene:
    return synthetic.Scene()


@pytest.fixture(scope="session")
def synth_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("synth")


@pytest.fixture(scope="session")
def las_mga_ahd(scene: synthetic.Scene, synth_dir: Path) -> Path:
    """The scene in GDA2020 / MGA zone 50 + AHD (EPSG:7850+5711)."""
    return synthetic.write_scene(scene, synth_dir / "mga_ahd.laz", srs="EPSG:7850+5711")


@pytest.fixture(scope="session")
def las_mga_only(scene: synthetic.Scene, synth_dir: Path) -> Path:
    """Horizontal CRS only — heights carry no datum."""
    return synthetic.write_scene(scene, synth_dir / "mga_only.laz", srs="EPSG:7850")


@pytest.fixture(scope="session")
def las_no_srs(scene: synthetic.Scene, synth_dir: Path) -> Path:
    return synthetic.write_scene(scene, synth_dir / "no_srs.laz", srs=None)


@pytest.fixture(scope="session")
def las_ellipsoidal(las_mga_ahd: Path, synth_dir: Path) -> Path:
    """The same points as GDA2020 lon/lat + ellipsoidal heights (EPSG:7843), made by PDAL's own reprojection
    (PROJ's choice, AUSGeoid2020 grid present) — independent of argus-survey's operation selection."""
    out = synth_dir / "gda2020_ellipsoidal.laz"
    stages = [
        {"type": "readers.las", "filename": str(las_mga_ahd)},
        {"type": "filters.reprojection", "in_srs": "EPSG:7850+5711", "out_srs": "EPSG:7843"},
        {
            "type": "writers.las",
            "filename": str(out),
            "a_srs": "EPSG:7843",
            "scale_x": 1e-9,
            "scale_y": 1e-9,
            "scale_z": 0.001,
            "offset_x": 115,
            "offset_y": -32,
            "offset_z": 0,
            "minor_version": 4,
            "dataformat_id": 6,
        },
    ]
    pdal_pipeline(stages, step="fixture")
    return out


@pytest.fixture(scope="session")
def las_photogrammetry(synth_dir: Path) -> Path:
    return synthetic.write_scene(synthetic.Scene(photogrammetry=True), synth_dir / "photo.laz")


@pytest.fixture()
def jobs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "jobs"
    monkeypatch.setenv("SURVEY_JOBS_DIR", str(root))
    return root
