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
