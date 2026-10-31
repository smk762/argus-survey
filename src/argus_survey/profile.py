"""Long sections: sample the surfaces along a polyline at a fixed spacing → CSV + SVG.

Chainage runs from the line's first vertex. Every vertex is sampled as well as every
*step*, so changes of direction are never cut. Heights are bilinear; a void is a gap in the
CSV (empty cell) and a break in the drawn line, never interpolated across.
"""

from __future__ import annotations

import csv
import html
import math
from pathlib import Path

from argus_survey import rasters
from argus_survey.jobs import Job
from argus_survey.models import ProfileReport, SurveyError
from argus_survey.qa import horizontal_srs, raster_srs
from argus_survey.vectors import densify, read_lines, wkt_rings

STEP = "profile"
COLOURS = {"dtm": "#8a5a2b", "dsm": "#2e7d32", "chm": "#1565c0", "design": "#c62828"}


def nice_ticks(lo: float, hi: float, target: int = 6) -> list[float]:
    """Round tick positions covering [lo, hi]."""
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 1e-9:
        if t >= lo - step * 1e-9:
            ticks.append(round(t, 10))
        t += step
    return ticks


def svg(title: str, chain: list[float], series: dict[str, list[float | None]], vlabel: str) -> str:
    w, h = 960, 380
    ml, mr, mt, mb = 70, 20, 40, 50
    pw, ph = w - ml - mr, h - mt - mb
    vals = [v for s in series.values() for v in s if v is not None]
    if not vals:
        raise SurveyError("the line crosses no surface data")
    zlo, zhi = min(vals), max(vals)
    pad = max((zhi - zlo) * 0.08, 0.1)
    zlo, zhi = zlo - pad, zhi + pad
    c0, c1 = chain[0], chain[-1] or 1.0

    def px(c: float) -> float:
        return ml + (c - c0) / (c1 - c0) * pw

    def py(z: float) -> float:
        return mt + (zhi - z) / (zhi - zlo) * ph

    ve = (ph / (zhi - zlo)) / (pw / (c1 - c0))
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="12">',
        f'<rect width="{w}" height="{h}" fill="white"/>',
        f'<text x="{ml}" y="22" font-size="14" font-weight="bold">{html.escape(title)}</text>',
        f'<text x="{w - mr}" y="22" text-anchor="end" fill="#555">vertical exaggeration ×{ve:.1f}</text>',
    ]
    for t in nice_ticks(zlo, zhi):
        y = py(t)
        out.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e0e0e0"/>')
        out.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end">{t:g}</text>')
    for t in nice_ticks(c0, c1):
        x = px(t)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{mt}" y2="{mt + ph}" stroke="#f0f0f0"/>')
        out.append(f'<text x="{x:.1f}" y="{mt + ph + 18}" text-anchor="middle">{t:g}</text>')
    out.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#999"/>')
    out.append(f'<text x="{ml + pw / 2}" y="{h - 10}" text-anchor="middle">Chainage (m)</text>')
    out.append(
        f'<text transform="translate(16 {mt + ph / 2}) rotate(-90)" text-anchor="middle">{html.escape(vlabel)}</text>'
    )
    lx = ml + 10
    for name, s in series.items():
        colour = COLOURS.get(name, "#333")
        runs: list[list[str]] = [[]]
        for c, z in zip(chain, s, strict=True):
            if z is None:
                runs.append([])
            else:
                runs[-1].append(f"{px(c):.1f},{py(z):.1f}")
        for r in runs:
            if len(r) > 1:
                out.append(f'<polyline fill="none" stroke="{colour}" stroke-width="1.6" points="{" ".join(r)}"/>')
        out.append(f'<line x1="{lx}" x2="{lx + 18}" y1="{mt + 14}" y2="{mt + 14}" stroke="{colour}" stroke-width="3"/>')
        out.append(f'<text x="{lx + 22}" y="{mt + 18}">{name.upper()}</text>')
        lx += 70
    out.append("</svg>")
    return "\n".join(out)


def profile(
    job: Job, line: Path, *, step: float = 0.5, line_srs: str | None = None, surfaces: tuple[str, ...] = ("dtm", "dsm")
) -> ProfileReport:
    job.require_crs(STEP, height=True)
    if step <= 0:
        raise SurveyError("--step must be positive")
    available = [s for s in surfaces if s in job.sidecar.products]
    if not available:
        raise SurveyError(f"none of {', '.join(surfaces)} exist yet — run `argus-survey surfaces`")
    lines = read_lines(line, raster_srs(job), src_srs=line_srs, sink=job.sink, step=STEP)
    if len(lines) > 1:
        job.warn(f"{line.name} has {len(lines)} lines; the profile follows the first ({lines[0][0]})")
    name, wkt = lines[0]
    coords = wkt_rings(wkt)[0]
    pts = densify(coords, step)
    with job.step(STEP):
        series: dict[str, list[float | None]] = {}
        for s in available:
            vals = rasters.sample(
                job.product_path(s),
                [(str(i), x, y) for i, (_, x, y) in enumerate(pts)],
                crs=horizontal_srs(job),
                sink=job.sink,
                step=STEP,
            )
            series[s] = [vals[str(i)] for i in range(len(pts))]
        with job.path("profile.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["chainage", "x", "y", *available])
            for i, (c, x, y) in enumerate(pts):
                w.writerow(
                    [
                        f"{c:.3f}",
                        f"{x:.3f}",
                        f"{y:.3f}",
                        *("" if series[s][i] is None else f"{series[s][i]:.3f}" for s in available),
                    ]
                )
        crs = job.sidecar.crs
        assert crs is not None
        vlabel = f"Height (m, {crs.vertical.name if crs.vertical else 'unknown datum'})"
        job.path("profile.svg").write_text(
            svg(f"Profile: {name}", [p[0] for p in pts], series, vlabel), encoding="utf-8"
        )
        report = ProfileReport(
            line=name, length_m=round(pts[-1][0], 3), step_m=step, samples=len(pts), surfaces=available
        )
        job.sidecar.profiles.append(report)
        job.add_product("profile", "profile.csv", step=STEP, description=f"Profile along {name} (CSV)")
        job.add_product("profile_svg", "profile.svg", step=STEP, description=f"Profile along {name} (SVG)")
    return report
