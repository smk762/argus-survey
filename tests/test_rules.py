"""Repo-level must-nevers from CLAUDE.md that no behaviour test can see: what is committed, locked and written."""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout"
)

# PDAL, GDAL and PROJ are called as CLIs from the conda-forge stack; Python bindings would bring a second GDAL/PROJ.
BANNED_PACKAGES = {"pdal", "gdal", "osgeo", "rasterio", "laspy", "pyproj", "fiona"}

# Stage types the package must never run: filters.reprojection lets PROJ pick an operation (a ballpark one when a
# geoid grid is missing), and writers.copc buffers the whole cloud (untwine builds the COPC out of core).
BANNED_STAGE = re.compile(r"""["']type["']\s*:\s*["'](filters\.reprojection|writers\.copc)["']""")


@needs_git
def test_no_binary_files_under_tests() -> None:
    """Fixtures are synthesised at test time or captured as text (tests/data): never a committed binary."""
    out = subprocess.run(["git", "ls-files", "-z", "tests"], cwd=ROOT, capture_output=True, check=True).stdout
    binary = []
    for name in filter(None, out.decode().split("\0")):
        data = (ROOT / name).read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            binary.append(name)
            continue
        if b"\0" in data:
            binary.append(name)
    assert not binary, f"binary files committed under tests/: {binary}"


def test_no_python_gis_bindings_in_the_lock() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    names = {p["name"].lower() for p in lock.get("package", [])}
    assert not names & BANNED_PACKAGES, f"uv.lock pulls in {sorted(names & BANNED_PACKAGES)}"


def test_no_pip_install_of_gis_bindings_in_the_image() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").lower()
    for line in text.splitlines():
        if "pip install" in line or "uv pip" in line or "uv add" in line:
            hits = [p for p in BANNED_PACKAGES if re.search(rf"\b{p}\b", line)]
            assert not hits, f"Dockerfile installs {hits} with pip: {line.strip()}"


def test_no_banned_pdal_stages() -> None:
    hits = [
        f"{p.relative_to(ROOT)}: {m.group(1)}"
        for p in sorted((ROOT / "src").rglob("*.py"))
        for m in BANNED_STAGE.finditer(p.read_text(encoding="utf-8"))
    ]
    assert not hits, f"banned PDAL stages: {hits}"


def test_must_never_table_names_real_tests() -> None:
    """Every test CLAUDE.md's "Must never happen" table cites exists, so a rename can't leave a rule unchecked."""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    table = text[text.index("## Must never happen") : text.index("\n## ", text.index("## Must never happen") + 1)]
    cited, missing = 0, []
    for row in table.splitlines():
        if not row.startswith("| ") or row.startswith("| Never"):
            continue
        module = None
        for mod, name in re.findall(r"`(test_\w+)?::(\w+|\*)`", row):
            module = mod or module
            path = ROOT / "tests" / f"{module}.py"
            cited += 1
            if not path.is_file() or (name != "*" and not re.search(rf"^def {name}\(", path.read_text(), re.M)):
                missing.append(f"{module}::{name}")
    assert cited > 20 and not missing, f"CLAUDE.md cites tests that don't exist: {missing}"
