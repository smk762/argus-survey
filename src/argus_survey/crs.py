"""CRS records and coordinate-operation selection.

Operation selection is deliberately explicit. PROJ, left to itself, falls back
to a *ballpark* transformation when a datum grid is missing — for heights that
means "ellipsoidal height reported as AHD", tens of metres wrong with no error.
So :func:`select_operation` asks ``projinfo`` for the non-ballpark candidates,
takes the first whose grids are all on disk (or the one the user named), and
returns its PROJ pipeline string. PDAL then runs exactly that string via
``filters.projpipeline``: if a grid disappears, the job fails instead of
degrading, and the sidecar records the operation that was actually applied.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from argus_survey.models import (
    Bounds3D,
    CrsRecord,
    CrsRef,
    HeightReference,
    OperationCandidate,
    SurveyError,
    TransformOp,
)
from argus_survey.tools import CommandSink, find_grid, run, run_json

# Linear units we accept as "metres internally" (CLAUDE.md: never guess units).
METRE_TOLERANCE = 1e-12

_OP_HEADER = re.compile(r"^Operation No\. \d+:\s*$")
_SUMMARY = re.compile(r"^(?P<id>[^,]+), (?P<name>.+?), (?P<acc>\d+(?:\.\d+)? m|unknown accuracy), (?P<area>.*)$")
_GRIDS = re.compile(r"\+grids=(\S+)")


class ParsedOperation(OperationCandidate):
    proj: str


def parse_projinfo_operations(text: str) -> list[ParsedOperation]:
    """Parse ``projinfo -s A -t B -o PROJ`` output into its candidate operations, in PROJ's order."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if _OP_HEADER.match(line):
            blocks.append([])
        elif blocks:
            blocks[-1].append(line)
    if not blocks and "+proj=" in text:  # a single operation printed without headers
        blocks = [text.splitlines()]

    ops: list[ParsedOperation] = []
    for block in blocks:
        summary = next((ln.strip() for ln in block if _SUMMARY.match(ln.strip())), None)
        try:
            start = next(i for i, ln in enumerate(block) if ln.strip() == "PROJ string:") + 1
        except StopIteration:
            continue
        proj_lines: list[str] = []
        for ln in block[start:]:
            if not ln.strip() or ln.startswith("---"):
                break
            proj_lines.append(ln.strip())
        proj = " ".join(proj_lines)
        name, accuracy = "unnamed operation", None
        if summary:
            m = _SUMMARY.match(summary)
            assert m is not None
            name = m.group("name")
            acc = m.group("acc")
            accuracy = None if acc == "unknown accuracy" else float(acc.removesuffix(" m"))
        grids: list[str] = []
        for value in _GRIDS.findall(proj):
            # A leading '@' marks a grid PROJ may skip when absent. We never allow that: it is a silent datum.
            grids.extend(g.lstrip("@") for g in value.split(",") if g)
        ops.append(ParsedOperation(name=name, accuracy_m=accuracy, grids=grids, proj=proj))
    return ops


def select_operation(
    source: str,
    target: str,
    *,
    bbox_lonlat: tuple[float, float, float, float] | None = None,
    prefer: str | None = None,
    sink: CommandSink | None = None,
    step: str = "crs",
) -> TransformOp:
    """Pick the coordinate operation from *source* to *target* (any PROJ CRS definition).

    *bbox_lonlat* (west, south, east, north) restricts candidates to those valid over the data.
    *prefer* is a case-insensitive substring of an operation name (e.g. ``"GDA94 to GDA2020 (2)"``)
    to pin a specific candidate; otherwise PROJ's ranking is used, skipping candidates whose grids
    are absent. Ballpark operations are never candidates.
    """
    argv = ["projinfo", "-s", source, "-t", target, "--spatial-test", "intersects", "--hide-ballpark", "-o", "PROJ"]
    if bbox_lonlat is not None:
        argv += ["--bbox", ",".join(f"{v:.6f}" for v in bbox_lonlat)]
    parsed = parse_projinfo_operations(run(argv, step=step, sink=sink).stdout)
    if not parsed:
        raise SurveyError(
            f"PROJ has no non-ballpark operation from {source} to {target}"
            + (" over the data's extent" if bbox_lonlat else "")
            + "; refusing rather than guessing (a ballpark transform ignores datum and geoid differences)"
        )

    candidates = [
        OperationCandidate(
            name=p.name,
            accuracy_m=p.accuracy_m,
            grids=p.grids,
            missing_grids=[g for g in p.grids if find_grid(g) is None],
        )
        for p in parsed
    ]
    usable = [i for i, c in enumerate(candidates) if not c.missing_grids]
    if prefer is not None:
        matches = [i for i, c in enumerate(candidates) if prefer.lower() in c.name.lower()]
        if not matches:
            names = "; ".join(c.name for c in candidates)
            raise SurveyError(f"no operation from {source} to {target} matches {prefer!r} (candidates: {names})")
        chosen = matches[0]
        if candidates[chosen].missing_grids:
            raise SurveyError(
                f"operation {candidates[chosen].name!r} needs grid(s) not on disk: "
                f"{', '.join(candidates[chosen].missing_grids)} — run `argus-survey doctor`"
            )
    elif usable:
        chosen = usable[0]
    else:
        missing = sorted({g for c in candidates for g in c.missing_grids})
        raise SurveyError(
            f"every operation from {source} to {target} needs a datum grid that is not installed "
            f"({', '.join(missing)}); refusing rather than falling back to a ballpark transform — "
            "run `argus-survey doctor`"
        )

    op = parsed[chosen]
    return TransformOp(
        source=source,
        target=target,
        name=op.name,
        accuracy_m=op.accuracy_m,
        proj=op.proj,
        grids=op.grids,
        chosen_by="user" if prefer is not None else "proj-default",
        candidates=candidates,
    )


# ── CRS records ───────────────────────────────────────────────────────────────


def _unit(axis: dict[str, Any]) -> tuple[str, float]:
    unit = axis.get("unit", "metre")
    if isinstance(unit, str):
        return unit, 1.0 if unit == "metre" else math.nan
    return str(unit.get("name", "unknown")), float(unit.get("conversion_factor", math.nan))


def _epsg(obj: dict[str, Any]) -> int | None:
    ident = obj.get("id")
    if isinstance(ident, dict) and ident.get("authority") == "EPSG":
        try:
            return int(ident["code"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _ref(obj: dict[str, Any]) -> CrsRef:
    axes = obj.get("coordinate_system", {}).get("axis", [])
    # Horizontal unit from the first axis; for a vertical CRS that is its only axis.
    name, factor = _unit(axes[0]) if axes else ("unknown", math.nan)
    if obj.get("type") == "GeographicCRS":
        name, factor = "degree", math.nan
    return CrsRef(
        name=str(obj.get("name", "unnamed")),
        epsg=_epsg(obj),
        kind=str(obj.get("type", "unknown")),
        units=name,
        unit_factor=factor,
        north_first=bool(axes) and axes[0].get("direction") == "north",
    )


def _ellipsoidal_axis(obj: dict[str, Any]) -> tuple[str, float] | None:
    for axis in obj.get("coordinate_system", {}).get("axis", []):
        if axis.get("direction") == "up":
            return _unit(axis)
    return None


def record_from_projjson(pj: dict[str, Any], *, definition: str | None = None, source: str = "file") -> CrsRecord:
    """Build a :class:`CrsRecord` from PROJJSON (as ``pdal info`` and ``projinfo -o PROJJSON`` print it)."""
    if pj.get("type") == "BoundCRS":  # WKT1 TOWGS84 wrapper — the CRS itself is the source
        pj = pj["source_crs"]
    kind = pj.get("type")
    vertical: CrsRef | None = None
    height: HeightReference = "none"
    if kind == "CompoundCRS":
        comps = [c["source_crs"] if c.get("type") == "BoundCRS" else c for c in pj.get("components", [])]
        horiz_obj = next((c for c in comps if c.get("type") != "VerticalCRS"), None)
        vert_obj = next((c for c in comps if c.get("type") == "VerticalCRS"), None)
        if horiz_obj is None:
            raise SurveyError(f"compound CRS {pj.get('name')!r} has no horizontal component")
        horizontal = _ref(horiz_obj)
        if vert_obj is not None:
            vertical = _ref(vert_obj)
            height = "gravity"
    elif kind in {"ProjectedCRS", "GeographicCRS"}:
        horizontal = _ref(pj)
        up = _ellipsoidal_axis(pj)
        if up is not None:
            vertical = CrsRef(name="ellipsoidal height", kind="EllipsoidalHeight", units=up[0], unit_factor=up[1])
            height = "ellipsoidal"
    elif kind == "VerticalCRS":
        raise SurveyError(f"{pj.get('name')!r} is a vertical CRS only; a point cloud needs a horizontal CRS too")
    else:
        raise SurveyError(f"unsupported CRS type {kind!r} ({pj.get('name')!r}); use a projected or geographic CRS")

    if definition is None:
        top = _epsg(pj)
        v_code = vertical.epsg if vertical else None
        if top is not None:
            definition = f"EPSG:{top}"
        elif kind == "CompoundCRS" and horizontal.epsg is not None and v_code is not None:
            definition = f"EPSG:{horizontal.epsg}+{v_code}"
        else:  # PROJ and PDAL both accept PROJJSON as a CRS definition
            definition = json.dumps(pj, separators=(",", ":"))
    epoch = pj.get("coordinate_epoch") if isinstance(pj.get("coordinate_epoch"), int | float) else None
    return CrsRecord(
        definition=definition,
        name=str(pj.get("name", horizontal.name)),
        horizontal=horizontal,
        vertical=vertical,
        height_reference=height,
        epoch=epoch,
        source=source,  # type: ignore[arg-type]
    )


def record_from_pdal_srs(srs: dict[str, Any]) -> CrsRecord | None:
    """The CRS record of a cloud from ``pdal info``'s ``srs`` block, or ``None`` when the file has none."""
    pj = srs.get("json") or {}
    if not pj:
        return None
    return record_from_projjson(pj)


def describe(definition: str, *, sink: CommandSink | None = None, step: str = "crs", source: str = "user") -> CrsRecord:
    """Resolve any PROJ CRS definition (``EPSG:7850+5711``, WKT, PROJJSON) to a record."""
    try:
        pj = run_json(["projinfo", "-o", "PROJJSON", "-q", definition], step=step, sink=sink)
    except SurveyError as exc:
        raise SurveyError(f"PROJ does not recognise the CRS {definition!r}") from exc
    return record_from_projjson(
        pj, definition=definition if definition.upper().startswith("EPSG:") else None, source=source
    )


def promote_3d(definition: str, *, sink: CommandSink | None = None, step: str = "crs") -> str:
    """A 2D CRS promoted to 3D (ellipsoidal height on its own datum), as single-line WKT2."""
    out = run(["projinfo", "--3d", "-o", "WKT2_2019", "--single-line", "-q", definition], step=step, sink=sink)
    return out.stdout.strip()


def require_metric(record: CrsRecord, what: str) -> None:
    """Refuse non-metre linear units: metres internally, conversions explicit (CLAUDE.md)."""
    for ref, label in ((record.horizontal, "horizontal"), (record.vertical, "vertical")):
        if ref is None or ref.kind == "GeographicCRS":
            continue
        if not (ref.units == "metre" or abs(ref.unit_factor - 1.0) < METRE_TOLERANCE):
            raise SurveyError(
                f"{what} has {label} units of {ref.units!r} ({ref.unit_factor} m); argus-survey works in metres. "
                "Pass --t-srs with a metric CRS (e.g. EPSG:7850+5711) and the conversion is applied and recorded"
            )


# Projections whose "metres" are not ground metres away from the equator (scale = 1/cos φ).
MERCATOR_EPSG = {3857, 3395, 900913, 3785, 102100}


def require_survey_projection(record: CrsRecord, what: str) -> None:
    """Refuse a geographic or Web Mercator working CRS: distances, areas and volumes would be wrong.

    At 32° S a Web Mercator metre is 1.18 ground metres, so areas come out ~40 % large.
    """
    h = record.horizontal
    if h.kind == "GeographicCRS":
        raise SurveyError(f"{what} is geographic (degrees); pass --t-srs with a projected CRS (e.g. EPSG:7850+5711)")
    if h.epsg in MERCATOR_EPSG or "mercator" in h.name.lower() and "transverse" not in h.name.lower():
        raise SurveyError(
            f"{what} is {h.name}: Mercator 'metres' are not ground metres (×1/cos latitude), so areas and volumes "
            "would be badly wrong. Pass --t-srs with a survey projection (MGA2020 zone, PCG2020, UTM, …)"
        )


def same_crs(a: CrsRecord, b: CrsRecord) -> bool:
    if a.definition == b.definition:
        return True
    va = a.vertical.epsg if a.vertical else None
    vb = b.vertical.epsg if b.vertical else None
    return (
        a.horizontal.epsg is not None
        and a.horizontal.epsg == b.horizontal.epsg
        and a.height_reference == b.height_reference
        and va == vb
        and a.horizontal.unit_factor == b.horizontal.unit_factor
    )


def pdal_coord_op(op: TransformOp, source: CrsRecord, target: CrsRecord) -> str:
    """The PROJ string for ``filters.projpipeline``.

    PROJ operations follow each CRS's authority axis order (latitude first for
    EPSG:7843, northing first for some grids), but PDAL always holds X = easting/
    longitude. So an ``axisswap`` is spliced in before the first step when the
    source is north-first, and appended when the target is. Global pipeline
    parameters stay ahead of the first ``+step``.
    """
    tokens = op.proj.split()
    if not tokens or tokens[0] != "+proj=pipeline":
        tokens = ["+proj=pipeline", "+step", *tokens]
    swap = ["+step", "+proj=axisswap", "+order=2,1"]
    if source.horizontal.north_first:
        first = tokens.index("+step") if "+step" in tokens else len(tokens)
        tokens = tokens[:first] + swap + tokens[first:]
    if target.horizontal.north_first:
        tokens = tokens + swap
    return " ".join(tokens)


def lonlat_bbox(
    definition: str, bounds: Bounds3D, *, sink: CommandSink | None = None, step: str = "crs"
) -> tuple[float, float, float, float]:
    """(west, south, east, north) in degrees for *bounds* given in *definition*, from its corners and edge midpoints."""
    xs = (bounds.minx, (bounds.minx + bounds.maxx) / 2, bounds.maxx)
    ys = (bounds.miny, (bounds.miny + bounds.maxy) / 2, bounds.maxy)
    pts = "\n".join(f"{x!r} {y!r}" for x in xs for y in ys) + "\n"
    # gdaltransform uses GIS axis order (x=lon) on both sides regardless of the CRS's authority order.
    out = run(
        ["gdaltransform", "-s_srs", definition, "-t_srs", "EPSG:4326", "-output_xy"], step=step, sink=sink, stdin=pts
    ).stdout
    lons, lats = [], []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            lon, lat = float(parts[0]), float(parts[1])
            if math.isfinite(lon) and math.isfinite(lat):
                lons.append(lon)
                lats.append(lat)
    if not lons:
        raise SurveyError(f"could not place the data's extent on the globe from {definition!r}")
    return min(lons), min(lats), max(lons), max(lats)
