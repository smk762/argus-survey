"""Raster facts via GDAL's CLIs: grid, nodata, exact statistics, and raw values.

Statistics are always computed fresh (``GDAL_PAM_ENABLED=NO``): a stale
``.aux.xml`` sidecar from an earlier run must never stand in for the numbers of
the current raster.
"""

from __future__ import annotations

import math
import sys
import tempfile
from array import array
from pathlib import Path
from typing import Any

from argus_survey.models import GridSpec, RasterStats, SurveyError
from argus_survey.tools import CommandSink, run, run_json

NODATA = -9999.0
GTIFF_OPTS = "COMPRESS=DEFLATE,PREDICTOR=3,TILED=YES"
NO_PAM = {"GDAL_PAM_ENABLED": "NO"}


def info(path: Path, *, stats: bool = False, sink: CommandSink | None = None, step: str = "raster") -> dict[str, Any]:
    argv = ["gdalinfo", "-json"] + (["-stats"] if stats else []) + [str(path)]
    return run_json(argv, step=step, sink=sink, env=NO_PAM)


def grid_of(path: Path, *, sink: CommandSink | None = None, step: str = "raster") -> GridSpec:
    meta = info(path, sink=sink, step=step)
    gt = meta["geoTransform"]
    width, height = meta["size"]
    if abs(gt[2]) > 0 or abs(gt[4]) > 0 or abs(gt[1] + gt[5]) > 1e-9:
        raise SurveyError(f"{path.name} is rotated or has non-square cells; argus-survey needs a north-up square grid")
    return GridSpec(origin_x=gt[0], origin_y=gt[3] + gt[5] * height, res=gt[1], width=width, height=height)


def stats(path: Path, *, sink: CommandSink | None = None, step: str = "raster") -> RasterStats:
    """Exact statistics of band 1 (valid cells counted from GDAL's valid percentage)."""
    meta = info(path, stats=True, sink=sink, step=step)
    width, height = meta["size"]
    band = meta["bands"][0]
    md = (band.get("metadata") or {}).get("", {})
    total = width * height
    valid_pct = float(md.get("STATISTICS_VALID_PERCENT", 0.0))
    valid = round(total * valid_pct / 100.0)
    if valid == 0:
        return RasterStats(valid_cells=0, total_cells=total)
    return RasterStats(
        valid_cells=valid,
        total_cells=total,
        minimum=float(md["STATISTICS_MINIMUM"]),
        maximum=float(md["STATISTICS_MAXIMUM"]),
        mean=float(md["STATISTICS_MEAN"]),
        stddev=float(md["STATISTICS_STDDEV"]),
    )


def read_values(path: Path, *, sink: CommandSink | None = None, step: str = "raster") -> tuple[array, float | None]:
    """Every cell of band 1 as float32 (row-major from the top-left) and the nodata value.

    Through ``gdal_translate -of ENVI`` (raw little-endian floats) rather than Python bindings.
    Meant for modest rasters (density grids, sample windows), not full-resolution DTMs.
    """
    meta = info(path, sink=sink, step=step)
    nodata = meta["bands"][0].get("noDataValue")
    with tempfile.TemporaryDirectory(prefix="argus-survey-raw-") as tmp:
        raw = Path(tmp) / "band.bin"
        run(
            [
                "gdal_translate",
                "-q",
                "-of",
                "ENVI",
                "-ot",
                "Float32",
                "-b",
                "1",
                "-co",
                "INTERLEAVE=BSQ",
                str(path),
                str(raw),
            ],
            step=step,
            sink=sink,
        )
        values = array("f")
        values.frombytes(raw.read_bytes())
    if sys.byteorder != "little":  # pragma: no cover - ENVI from GDAL is native-endian on x86/ARM
        values.byteswap()
    return values, (float(nodata) if nodata is not None and not isinstance(nodata, str) else None)


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile (numpy's default 'linear' method) of pre-sorted values."""
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def calc(
    out: Path,
    expression: str,
    inputs: dict[str, Path],
    *,
    sink: CommandSink | None,
    step: str,
    data_type: str = "Float32",
    nodata: float | str | None = NODATA,
    propagate_nodata: bool = False,
) -> Path:
    """``gdal raster calc`` — inputs must share a grid (GDAL checks extent and CRS)."""
    argv = ["gdal", "raster", "calc", "--overwrite", "--ot", data_type, "--calc", expression, "-o", str(out)]
    for name, path in inputs.items():
        argv += ["-i", f"{name}={path}"]
    if nodata is not None:
        argv += ["--nodata", str(nodata)]
    if propagate_nodata:
        argv.append("--propagate-nodata")
    argv += ["--co", "COMPRESS=DEFLATE", "--co", "TILED=YES"]
    run(argv, step=step, sink=sink, env=NO_PAM)
    return out


def set_crs(path: Path, definition: str, *, sink: CommandSink | None, step: str) -> None:
    """Stamp *definition* on a raster in place (gdal_rasterize drops the vertical part of a compound CRS)."""
    run(["gdal", "raster", "edit", "--crs", definition, str(path)], step=step, sink=sink)


def sample(
    path: Path,
    points: list[tuple[str, float, float]],
    *,
    crs: str,
    resampling: str = "bilinear",
    sink: CommandSink | None = None,
    step: str = "raster",
) -> dict[str, float | None]:
    """Values of *path* at ``(id, x, y)`` points given in *crs*, in one ``gdal raster pixel-info`` call.

    ``None`` for a point outside the raster or on a nodata cell. With bilinear resampling, a point
    whose 2x2 neighbourhood touches a void is also ``None`` (GDAL does not interpolate across nodata).
    """
    import json

    nodata = info(path, sink=sink, step=step)["bands"][0].get("noDataValue")
    features = [
        {"type": "Feature", "properties": {"id": pid}, "geometry": {"type": "Point", "coordinates": [x, y]}}
        for pid, x, y in points
    ]
    with tempfile.TemporaryDirectory(prefix="argus-survey-sample-") as tmp:
        pts = Path(tmp) / "points.geojson"
        pts.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
        result = Path(tmp) / "sampled.geojson"
        # not /vsistdout/: GDAL's GeoJSON writer may need to reopen its output
        run(
            [
                "gdal",
                "raster",
                "pixel-info",
                "-i",
                str(path),
                "--position-dataset",
                str(pts),
                "--position-crs",
                crs,
                "-r",
                resampling,
                "-f",
                "GeoJSON",
                "-o",
                str(result),
            ],
            step=step,
            sink=sink,
        )
        out = json.loads(result.read_text(encoding="utf-8"))
    values: dict[str, float | None] = {pid: None for pid, _, _ in points}
    for feat in out.get("features", []):
        props = feat.get("properties", {})
        bands = props.get("bands") or []
        if not bands:
            continue
        v = bands[0].get("raw_value")
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        if nodata is not None and not isinstance(nodata, str) and abs(float(v) - float(nodata)) < 1e-6:
            continue
        values[str(props.get("id"))] = float(v)
    return values


def window(
    grid: GridSpec, bbox: tuple[float, float, float, float], pad: int = 1
) -> tuple[int, int, int, int, GridSpec]:
    """The cell window of *grid* covering *bbox* (+*pad* cells), clamped: ``(xoff, yoff, xsize, ysize, subgrid)``.

    ``xoff``/``yoff`` count from the top-left (GDAL's ``-srcwin`` convention); the subgrid shares
    *grid*'s lattice exactly, so cutting it never resamples.
    """
    minx, miny, maxx, maxy = bbox
    c0 = max(0, math.floor((minx - grid.origin_x) / grid.res) - pad)
    c1 = min(grid.width, math.ceil((maxx - grid.origin_x) / grid.res) + pad)
    r0 = max(0, math.floor((miny - grid.origin_y) / grid.res) - pad)  # rows from the bottom
    r1 = min(grid.height, math.ceil((maxy - grid.origin_y) / grid.res) + pad)
    if c1 <= c0 or r1 <= r0:
        raise SurveyError("the area lies outside the surface grid")
    sub = GridSpec(
        origin_x=grid.origin_x + c0 * grid.res,
        origin_y=grid.origin_y + r0 * grid.res,
        res=grid.res,
        width=c1 - c0,
        height=r1 - r0,
    )
    return c0, grid.height - r1, c1 - c0, r1 - r0, sub


def cut_window(
    src: Path,
    out: Path,
    grid: GridSpec,
    bbox: tuple[float, float, float, float],
    *,
    sink: CommandSink | None,
    step: str,
) -> GridSpec:
    """Copy the *bbox* window of *src* (on *grid*) to *out*, cell for cell."""
    xoff, yoff, xsize, ysize, sub = window(grid, bbox)
    run(
        [
            "gdal_translate",
            "-q",
            "-srcwin",
            str(xoff),
            str(yoff),
            str(xsize),
            str(ysize),
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(src),
            str(out),
        ],
        step=step,
        sink=sink,
    )
    return sub


def write_raster(
    values: array, grid: GridSpec, out: Path, *, crs: str, sink: CommandSink | None, step: str, nodata: float = NODATA
) -> Path:
    """Write row-major (top-left first) float32 *values* on *grid* as a GeoTIFF, via an ENVI raw file.

    For rasters the package computes itself (e.g. a least-squares base plane) without Python bindings.
    """
    if len(values) != grid.width * grid.height:
        raise ValueError("values do not match the grid size")
    with tempfile.TemporaryDirectory(prefix="argus-survey-write-") as tmp:
        raw = Path(tmp) / "r.bin"
        data = array("f", values)
        if sys.byteorder != "little":  # pragma: no cover
            data.byteswap()
        raw.write_bytes(data.tobytes())
        top = grid.origin_y + grid.height * grid.res
        (Path(tmp) / "r.hdr").write_text(
            "ENVI\n"
            f"samples = {grid.width}\nlines = {grid.height}\nbands = 1\nheader offset = 0\n"
            "file type = ENVI Standard\ndata type = 4\ninterleave = bsq\nbyte order = 0\n"
            f"map info = {{Arbitrary, 1, 1, {grid.origin_x!r}, {top!r}, {grid.res!r}, {grid.res!r}}}\n"
            f"data ignore value = {nodata!r}\n",
            encoding="utf-8",
        )
        run(
            [
                "gdal_translate",
                "-q",
                "-of",
                "GTiff",
                "-a_srs",
                crs,
                "-a_nodata",
                repr(nodata),
                "-co",
                "COMPRESS=DEFLATE",
                str(raw),
                str(out),
            ],
            step=step,
            sink=sink,
        )
    set_crs(out, crs, sink=sink, step=step)
    return out
