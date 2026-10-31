"""Vector inputs (crop/stockpile polygons, profile lines) brought into the job CRS, and small geometry helpers.

A polygon file must say what CRS it is in, or the user must (``--*-srs``). A GeoJSON file
without a ``crs`` member is WGS 84 by RFC 7946 — and GDAL will happily read MGA eastings
as longitudes. So a file that declares geographic coordinates but has a projected-looking
extent is refused rather than reprojected into nonsense.
"""

from __future__ import annotations

import csv
import io
import math
import re
import tempfile
from pathlib import Path

from argus_survey.models import SurveyError
from argus_survey.tools import CommandSink, run, run_json

NAME_FIELDS = ("name", "Name", "NAME", "id", "ID", "label", "Label")
Point = tuple[float, float]
Ring = list[Point]


def _check_declared_crs(path: Path, src_srs: str | None, *, sink: CommandSink | None, step: str, flag: str) -> None:
    if src_srs is not None:
        return
    info = run_json(["ogrinfo", "-json", "-so", "-al", str(path)], step=step, sink=sink)
    layers = info.get("layers") or []
    if not layers:
        raise SurveyError(f"{path.name} has no vector layer")
    fields = layers[0].get("geometryFields") or [{}]
    declared = (fields[0].get("coordinateSystem") or {}).get("projjson")
    extent = fields[0].get("extent") or layers[0].get("extent")
    if not declared:
        raise SurveyError(f"{path.name} has no CRS; pass {flag} with the CRS of its coordinates")
    looks_projected = bool(extent) and (
        max(abs(extent[0]), abs(extent[2])) > 180 or max(abs(extent[1]), abs(extent[3])) > 90
    )
    if declared.get("type") == "GeographicCRS" and looks_projected:
        raise SurveyError(
            f"{path.name} declares geographic coordinates (GeoJSON without a 'crs' member defaults to WGS 84) "
            f"but its extent is clearly projected; pass {flag} (e.g. EPSG:7850)"
        )


def read_features(
    path: Path,
    target: str,
    *,
    src_srs: str | None,
    kinds: tuple[str, ...],
    sink: CommandSink | None,
    step: str,
    flag: str = "--polygon-srs",
) -> list[tuple[str, str]]:
    """``(name, wkt)`` of every feature of *path* whose geometry type starts with one of *kinds*, in *target*."""
    _check_declared_crs(path, src_srs, sink=sink, step=step, flag=flag)
    argv = ["ogr2ogr", "-f", "CSV", "/vsistdout/", str(path), "-t_srs", target, "-lco", "GEOMETRY=AS_WKT"]
    if src_srs is not None:
        argv[5:5] = ["-s_srs", src_srs]
    out = run(argv, step=step, sink=sink).stdout
    rows = list(csv.DictReader(io.StringIO(out)))
    feats: list[tuple[str, str]] = []
    for i, row in enumerate(rows, 1):
        wkt = (row.get("WKT") or "").strip()
        if not wkt.upper().startswith(kinds):
            continue
        name = next((row[f] for f in NAME_FIELDS if row.get(f)), None) or f"{path.stem}-{i}"
        feats.append((name, wkt))
    return feats


def read_polygons(
    path: Path, target: str, *, src_srs: str | None, sink: CommandSink | None, step: str, flag: str = "--polygon-srs"
) -> list[tuple[str, str]]:
    feats = read_features(
        path, target, src_srs=src_srs, kinds=("POLYGON", "MULTIPOLYGON"), sink=sink, step=step, flag=flag
    )
    if not feats:
        raise SurveyError(f"{path.name} contains no polygon")
    return feats


def read_lines(
    path: Path, target: str, *, src_srs: str | None, sink: CommandSink | None, step: str, flag: str = "--line-srs"
) -> list[tuple[str, str]]:
    feats = read_features(
        path, target, src_srs=src_srs, kinds=("LINESTRING", "MULTILINESTRING"), sink=sink, step=step, flag=flag
    )
    if not feats:
        raise SurveyError(f"{path.name} contains no line")
    return feats


# ── WKT geometry (just enough for rings and lines; no shapely) ───────────────

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_COORD_LIST = re.compile(r"\(([^()]+)\)")


def _coords(text: str) -> list[Point]:
    pts: list[Point] = []
    for pair in text.split(","):
        nums = re.findall(_NUM, pair)
        if len(nums) >= 2:
            pts.append((float(nums[0]), float(nums[1])))
    return pts


def wkt_rings(wkt: str) -> list[Ring]:
    """Every coordinate list of a (MULTI)POLYGON or (MULTI)LINESTRING WKT, in order (exteriors and holes)."""
    return [_coords(m) for m in _COORD_LIST.findall(wkt) if _coords(m)]


def wkt_bbox(wkt: str) -> tuple[float, float, float, float]:
    pts = [p for ring in wkt_rings(wkt) for p in ring]
    if not pts:
        raise SurveyError("empty geometry")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def densify(line: Ring, step: float) -> list[tuple[float, float, float]]:
    """Points every *step* along *line* (vertices included) as ``(chainage, x, y)``."""
    out: list[tuple[float, float, float]] = []
    chain = 0.0
    for (x0, y0), (x1, y1) in zip(line, line[1:], strict=False):
        seg = math.hypot(x1 - x0, y1 - y0)
        n = max(1, math.ceil(seg / step))
        for k in range(n):
            t = k / n
            out.append((chain + t * seg, x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
        chain += seg
    if line:
        out.append((chain, line[-1][0], line[-1][1]))
    return out


def write_zones(zones: list[tuple[str, str]], out: Path, srs: str, *, sink: CommandSink | None, step: str) -> Path:
    """A GPKG of named polygons (``name`` field) in *srs*, for ``gdal raster zonal-stats``."""
    with tempfile.TemporaryDirectory(prefix="argus-survey-zones-") as tmp:
        src = Path(tmp) / "zones.csv"
        with src.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["name", "WKT"])
            w.writerows(zones)
        out.unlink(missing_ok=True)
        run(
            [
                "ogr2ogr",
                "-f",
                "GPKG",
                str(out),
                str(src),
                "-oo",
                "GEOM_POSSIBLE_NAMES=WKT",
                "-oo",
                "KEEP_GEOM_COLUMNS=NO",
                "-a_srs",
                srs,
                "-nln",
                "zones",
            ],
            step=step,
            sink=sink,
        )
    return out
