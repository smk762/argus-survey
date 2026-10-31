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

import re

from argus_survey.models import OperationCandidate, SurveyError, TransformOp
from argus_survey.tools import CommandSink, find_grid, run

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
