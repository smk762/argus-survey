"""One self-contained ``report.html`` per job: what was done, to which datum, with what result.

Stdlib templating only — no JavaScript, no external fonts, scripts or images (thumbnails are
PNGs from ``gdal_translate``, embedded as data URIs), so the file can be emailed, archived
with the job and printed to PDF as the deliverable's cover document. Every figure links to
the numbered commands that produced it, and the full command log closes the report.
"""

from __future__ import annotations

import base64
import html
import json
import tempfile
from pathlib import Path
from typing import Any

from argus_survey import __version__
from argus_survey.jobs import Job
from argus_survey.models import CommandRecord, SurveyError, VolumeReport, utcnow
from argus_survey.tools import run

STEP = "report"
THUMB_PX = 760

CSS = """
:root { --ink:#1d2329; --muted:#5f6b76; --line:#d8dee4; --bg:#ffffff; --soft:#f5f7f9;
        --warn:#8a5a00; --warnbg:#fff4d6; --bad:#9b1c1c; --badbg:#fde8e8; --ok:#1e6b34; }
* { box-sizing: border-box; }
html { background: var(--bg); }
body { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--ink);
       max-width: 1000px; margin: 0 auto; padding: 24px 16px 64px; background: var(--bg); }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 32px 0 8px; padding-bottom: 4px; border-bottom: 2px solid var(--ink); }
h3 { font-size: 15px; margin: 18px 0 6px; }
.sub { color: var(--muted); margin: 0 0 16px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 12px; font-size: 13px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: var(--soft); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.kv th { width: 28%; background: none; color: var(--muted); font-weight: 500; }
.banner { padding: 10px 14px; border-radius: 6px; margin: 12px 0; font-weight: 600; }
.banner.bad { background: var(--badbg); color: var(--bad); border: 1px solid var(--bad); }
.banner.warn { background: var(--warnbg); color: var(--warn); border: 1px solid var(--warn); font-weight: 500; }
.note { color: var(--muted); font-size: 12.5px; }
.statement { background: var(--soft); border-left: 4px solid var(--ok); padding: 8px 12px; margin: 8px 0; }
figure { margin: 8px 0 16px; break-inside: avoid; }
figure img { max-width: 100%; max-height: 420px; width: auto; height: auto; border: 1px solid var(--line); display: block; }
figure svg { max-width: 100%; height: auto; border: 1px solid var(--line); display: block; }
figcaption { color: var(--muted); font-size: 12.5px; margin-top: 4px; }
code, pre { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: 11.5px; }
pre { background: var(--soft); padding: 6px 8px; white-space: pre-wrap; word-break: break-all; margin: 4px 0; }
.cmd { border-bottom: 1px solid var(--line); padding: 6px 0; break-inside: avoid; }
.cmd .meta { color: var(--muted); font-size: 12px; }
.sha { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px; word-break: break-all; }
a { color: #1f5fa8; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 700px) { .grid2 { grid-template-columns: 1fr; } table.kv th { width: 40%; } }
@media print {
  body { max-width: none; padding: 0; font-size: 11px; }
  h2 { break-after: avoid; }
  table, figure, .banner, .statement { break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
}
@page { size: A4; margin: 15mm 12mm; }
"""


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def num(value: float | None, digits: int = 3, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}{unit}"


def params(d: dict[str, Any]) -> str:
    """``{"slope": 0.15, "window": 18.0}`` → ``slope 0.15 · window 18``."""
    return " · ".join(
        f"{k.replace('_', ' ')} {v:g}" if isinstance(v, float) else f"{k.replace('_', ' ')} {v}" for k, v in d.items()
    )


def kv(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{e(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return f'<table class="kv">{body}</table>'


def table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(
        f'<th class="num">{e(h)}</th>' if i in numeric else f"<th>{e(h)}</th>" for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(f'<td class="num">{c}</td>' if i in numeric else f"<td>{c}</td>" for i, c in enumerate(r))
        + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def cmd_links(commands: list[CommandRecord], step: str) -> str:
    idx = [i for i, c in enumerate(commands, 1) if c.step == step]
    if not idx:
        return ""
    links = ", ".join(f'<a href="#cmd-{i}">#{i}</a>' for i in idx)
    return f'<p class="note">Produced by command{"s" if len(idx) > 1 else ""} {links}.</p>'


def thumbnail(job: Job, kind: str, *, color: list[tuple[float, tuple[int, int, int]]] | None = None) -> str | None:
    """A PNG data URI of product *kind* (optionally colour-ramped), or ``None`` if it doesn't exist."""
    product = job.sidecar.products.get(kind)
    if product is None or not job.path(product.path).is_file():
        return None
    src = job.path(product.path)
    with tempfile.TemporaryDirectory(prefix="argus-survey-thumb-") as tmp:
        png = Path(tmp) / "t.png"
        if color:
            ramp = Path(tmp) / "ramp.txt"
            ramp.write_text("".join(f"{v!r} {r} {g} {b}\n" for v, (r, g, b) in color) + "nv 255 255 255 0\n")
            coloured = Path(tmp) / "c.tif"
            run(
                ["gdaldem", "color-relief", "-q", "-alpha", str(src), str(ramp), str(coloured)],
                step=STEP,
                sink=job.sink,
            )
            src = coloured
        run(
            ["gdal_translate", "-q", "-of", "PNG", "-outsize", str(THUMB_PX), "0", str(src), str(png)],
            step=STEP,
            sink=job.sink,
        )
        return "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode("ascii")


def figure(uri: str | None, caption: str) -> str:
    if uri is None:
        return ""
    return f'<figure><img src="{uri}" alt="{e(caption)}"><figcaption>{e(caption)}</figcaption></figure>'


VIRIDIS = [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)]


# ── sections ──────────────────────────────────────────────────────────────────


def section_summary(job: Job) -> str:
    s = job.sidecar
    out = [
        f"<h1>Survey report — {e(s.job_id)}</h1>",
        f'<p class="sub">Generated {e(utcnow().strftime("%Y-%m-%d %H:%M UTC"))} by argus-survey {e(__version__)} · '
        f"PDAL {e(s.tools.pdal)} · GDAL {e(s.tools.gdal)} · PROJ {e(s.tools.proj)}</p>",
    ]
    if s.crs and s.crs.vertical_assumed:
        out.append(
            f'<div class="banner bad">Vertical datum ASSUMED, not read from the data — '
            f"{e(s.crs.vertical_assumed)}. Every height in this report depends on that assumption.</div>"
        )
    if s.crs and not s.crs.has_height_datum:
        out.append(
            '<div class="banner bad">No vertical datum: heights are of unknown datum; no surfaces, volumes '
            "or accuracy were produced.</div>"
        )
    if s.warnings:
        items = "".join(f"<li>{e(w)}</li>" for w in s.warnings)
        out.append(f'<div class="banner warn"><strong>Warnings</strong><ul>{items}</ul></div>')
    rows = [("Steps run", e(", ".join(s.steps_done) or "none")), ("Created", e(s.created_at.isoformat(" ", "seconds")))]
    if s.ingest:
        rows.append(("Points", e(f"{s.ingest.output_count:,}") + (" (cropped)" if s.ingest.cropped else "")))
        rows.append(("Source type", e(s.ingest.source_type)))
    out.append(kv(rows))
    return "\n".join(out)


def section_crs(job: Job) -> str:
    s = job.sidecar
    if s.crs is None:
        return ""
    c = s.crs
    h = c.horizontal
    rows = [
        ("CRS", e(c.name)),
        (
            "Definition",
            f"<code>{e(c.definition if len(c.definition) < 200 else '(WKT/PROJJSON, see job.json)')}</code>",
        ),
        ("Horizontal", e(f"{h.name}" + (f" (EPSG:{h.epsg})" if h.epsg else "") + f", {h.units}")),
    ]
    if c.vertical:
        v = c.vertical
        rows.append(
            (
                "Vertical",
                e(f"{v.name}" + (f" (EPSG:{v.epsg})" if v.epsg else "") + f", {v.units} [{c.height_reference}]"),
            )
        )
    else:
        rows.append(("Vertical", '<strong style="color:var(--bad)">none</strong>'))
    rows.append(("Geoid model applied", e(c.geoid or "none (no height transformation)")))
    if c.vertical_assumed:
        rows.append(("Assumption", f'<strong style="color:var(--bad)">{e(c.vertical_assumed)}</strong>'))
    if c.epoch is not None:
        rows.append(("Coordinate epoch", e(c.epoch)))
    out = ["<h2>Coordinate reference</h2>", kv(rows)]
    sf = s.ingest.scale_factors if s.ingest else None
    if sf is not None:
        srows = [
            (
                "Point scale factor k",
                e(
                    f"{sf.point_scale:.8f} at the centroid ({sf.point_scale_min:.8f} – {sf.point_scale_max:.8f} over "
                    f"the extent's corners)"
                ),
            ),
            ("Grid convergence", e(f"{sf.convergence_deg:+.6f}°")),
        ]
        if sf.height_factor is not None:
            h = f"{sf.ellipsoidal_height_m:.3f} m ellipsoidal"
            if sf.geoid_separation_m is not None:
                h += f" (mean height {sf.height_m:.3f} m + geoid separation {sf.geoid_separation_m:.3f} m)"
            srows.append(("Height factor R/(R+h)", e(f"{sf.height_factor:.8f}, h = {h}, R = {sf.radius_m:,.1f} m")))
            srows.append(("Combined scale factor", f"<strong>{e(f'{sf.combined:.8f}')}</strong>"))
        else:
            srows.append(("Combined scale factor", "not known (no vertical datum, so no height factor)"))
        out.append(
            "<h3>Grid to ground</h3>"
            + kv(srows)
            + f'<p class="note">{e(sf.method)}. Areas and volumes in this report are grid values unless marked '
            "ground.</p>"
        )
    if s.ingest:
        src = s.ingest.source_crs
        out.append(f"<h3>Source data</h3>{kv([('Source CRS', e(src.name)), ('Given by', e(src.source))])}")
        t = s.ingest.transform
        if t:
            cand = "".join(
                f"<li>{e(x.name)} — {num(x.accuracy_m, 3, ' m') if x.accuracy_m is not None else 'unknown accuracy'}"
                + (f" <em>(grid missing: {e(', '.join(x.missing_grids))})</em>" if x.missing_grids else "")
                + "</li>"
                for x in t.candidates
            )
            out.append(
                "<h3>Transformation</h3>"
                + kv(
                    [
                        ("Operation", e(t.name)),
                        ("Stated accuracy", num(t.accuracy_m, 3, " m") if t.accuracy_m is not None else "unknown"),
                        ("Grids", e(", ".join(t.grids) or "none")),
                        ("Chosen by", e(t.chosen_by)),
                        ("PROJ pipeline", f"<pre>{e(t.proj)}</pre>"),
                        ("Candidates considered", f"<ol>{cand}</ol>"),
                    ]
                )
            )
        inputs = [
            [
                f"<code>{e(Path(i.path).name)}</code>",
                e(f"{i.summary.count:,}" if i.summary else "?"),
                e(f"{i.size:,}"),
                f'<span class="sha">{e(i.sha256)}</span>',
            ]
            for i in s.inputs
        ]
        out.append("<h3>Inputs</h3>" + table(["File", "Points", "Bytes", "SHA256"], inputs, {1, 2}))
        out.append(cmd_links(s.commands, "ingest"))
    return "\n".join(out)


def section_qa(job: Job) -> str:
    q = job.sidecar.qa
    if q is None:
        return ""
    b = q.bounds
    out = [
        "<h2>Quality assessment</h2>",
        kv(
            [
                ("Points", e(f"{q.points:,}")),
                (
                    "Covered area",
                    e(f"{q.coverage_area_m2:,.1f} m²") + f' <span class="note">(hexbin boundary, edge '
                    f"{q.coverage_hex_edge} m)</span>",
                ),
                ("Height range", e(f"{b.minz:.3f} … {b.maxz:.3f} m")),
                (
                    "Density (pts/m²)",
                    e(
                        f"mean {q.density.mean:.2f} · p5 {q.density.p5:.2f} · p50 {q.density.p50:.2f} · "
                        f"p95 {q.density.p95:.2f}  ({q.density.res} m cells)"
                    ),
                ),
                (
                    "Gaps",
                    e(
                        f"{q.gaps.area_m2:,.1f} m² ({q.gaps.percent_of_coverage:.2f}% of coverage) below "
                        f"{q.gaps.threshold:.2f} pts/m² — {q.gaps.threshold_rule}"
                    ),
                ),
                (
                    "Outliers flagged",
                    e(
                        f"{q.outliers.count:,} ({q.outliers.percent:.3f}%) by {q.outliers.method}: {params(q.outliers.params)}"
                    ),
                ),
            ]
        ),
    ]
    d = q.density
    stops = [d.p5 * 0.5, d.p5, d.p50, d.p95, d.p95 * 1.5]
    uri = thumbnail(job, "density", color=list(zip(stops, VIRIDIS, strict=True)))
    out.append(figure(uri, f"Point density, pts/m² (purple ≤ {d.p5:.1f}, green ≈ {d.p50:.1f}, yellow ≥ {d.p95:.1f})"))
    out.append(
        '<div class="grid2"><div><h3>Classes</h3>'
        + table(
            ["Code", "Class", "Points", "%"],
            [[e(c.code), e(c.name), e(f"{c.count:,}"), e(f"{c.percent:.2f}")] for c in q.classes],
            {0, 2, 3},
        )
        + "</div><div><h3>Return numbers</h3>"
        + table(
            ["Return", "Points", "%"],
            [[e(r.value), e(f"{r.count:,}"), e(f"{r.percent:.2f}")] for r in q.returns],
            {0, 1, 2},
        )
        + "</div></div>"
    )
    out.append(cmd_links(job.sidecar.commands, "qa"))
    return "\n".join(out)


def section_ground(job: Job) -> str:
    g = job.sidecar.ground
    if g is None:
        return ""
    return "\n".join(
        [
            "<h2>Ground classification</h2>",
            kv(
                [
                    (
                        "Method",
                        e(f"{g.method.upper()}{f' (preset {g.preset})' if g.preset else ''}: {params(g.params)}"),
                    ),
                    (
                        "Outliers first",
                        e(f"filters.outlier ({params(g.outlier_params)}) → class 7, ignored by the ground filter"),
                    ),
                    ("Ground points", e(f"{g.ground_points:,} of {g.points:,} ({g.ground_percent:.2f}%)")),
                    ("Ground density / spacing", e(f"{g.ground_density:.2f} pts/m² · {g.ground_spacing:.2f} m")),
                    ("Existing classes", e("reset to 1 before filtering" if g.reset_classes else "kept")),
                    ("Tiling", e(f"{g.tiles} tile(s) of {g.tile_size:.0f} m with {g.tile_buffer:.0f} m buffer")),
                ]
            ),
            cmd_links(job.sidecar.commands, "ground"),
        ]
    )


def section_surfaces(job: Job) -> str:
    s = job.sidecar.surfaces
    if s is None:
        return ""
    g = s.grid
    out = [
        "<h2>Surfaces</h2>",
        kv(
            [
                (
                    "Grid",
                    e(
                        f"{g.width} × {g.height} cells of {g.res} m, lower-left origin ({g.origin_x}, {g.origin_y}) — "
                        "snapped to multiples of the resolution"
                    ),
                ),
            ]
        ),
    ]
    if s.resolution_warning:
        out.append(f'<div class="banner warn">{e(s.resolution_warning)}</div>')
    rows = []
    for kind, r in s.surfaces.items():
        fill = (
            f"{r.void_percent_after_fill:.2f}% after fill ({r.filled_max_distance} cells)"
            if r.void_percent_after_fill is not None
            else ""
        )
        rows.append(
            [
                e(kind.upper()),
                e(r.method),
                e(f"{r.void_percent:.2f}%") + (f"<br>{e(fill)}" if fill else ""),
                e(f"{num(r.stats.minimum)} … {num(r.stats.maximum)}"),
            ]
        )
    out.append(table(["Surface", "Method", "Voids (of coverage)", "Range (m)"], rows, {2, 3}))
    out.append(figure(thumbnail(job, "hillshade"), "Hillshade of the DTM (multidirectional)"))
    out.append(cmd_links(job.sidecar.commands, "surfaces"))
    c = job.sidecar.contours
    if c:
        out.append(
            "<h3>Contours</h3>"
            + kv(
                [
                    ("Interval", e(f"{c.interval} m" + (f", index every {c.index_every}" if c.index_every else ""))),
                    ("Lines", e(f"{c.features:,} from {num(c.min_elev)} to {num(c.max_elev)} m")),
                ]
            )
            + cmd_links(job.sidecar.commands, "contours")
        )
    return "\n".join(out)


def _volume_table(v: VolumeReport) -> str:
    a, b = {"stockpile": ("Above base", "Below base"), "cutfill": ("Fill", "Cut"), "change": ("Gain", "Loss")}[v.kind]
    rows = [
        [
            e(z.name),
            e(f"{z.area_m2:,.2f}"),
            e(f"{z.above_m3:,.2f}"),
            e(f"{z.below_m3:,.2f}"),
            e(f"{z.net_m3:,.2f}"),
            e(f"{z.void_percent:.2f}"),
        ]
        for z in v.zones
    ]
    t = table(["Zone", "Area m²", f"{a} m³", f"{b} m³", "Net m³", "Voids %"], rows, {1, 2, 3, 4, 5})
    if v.combined_scale_factor is not None:
        grows = [
            [
                e(z.name),
                e(f"{z.ground_area_m2:,.2f}"),
                e(f"{z.ground_above_m3:,.2f}"),
                e(f"{z.ground_below_m3:,.2f}"),
                e(f"{z.ground_net_m3:,.2f}"),
            ]
            for z in v.zones
            if z.ground_area_m2 is not None
        ]
        t += f'<p class="note">At ground (grid ÷ {v.combined_scale_factor:.8f}²):</p>' + table(
            ["Zone", "Ground area m²", f"{a} m³", f"{b} m³", "Net m³"], grows, {1, 2, 3, 4}
        )
    bases = "".join(
        f"<li><strong>{e(z.name)}</strong>: {e(z.base)}"
        + (
            f" — fit RMS {z.base_fit_rms_m} m over {z.boundary_samples} boundary heights"
            if z.base_fit_rms_m is not None
            else ""
        )
        + "</li>"
        for z in v.zones
    )
    notes = [
        f"Sign convention: {v.sign_convention}.",
        f"Surfaces: {v.surface}; {v.res} m cells.",
        f"Method: {v.method}.",
    ]
    if v.surface_void_percent:
        notes.append(
            "Surface voids (whole site): "
            + ", ".join(f"{k.upper()} {p:.2f}%" for k, p in v.surface_void_percent.items())
            + "."
        )
    if v.resampled:
        notes.append(f"Resampled: {v.resampled}.")
    if v.allow_voids:
        notes.append("Voids above 5% were accepted (--allow-voids) and count as zero volume.")
    return t + f"<ul class='note'>{bases}</ul>" + "".join(f'<p class="note">{e(n)}</p>' for n in notes)


def section_volumes(job: Job) -> str:
    s = job.sidecar
    out: list[str] = []
    if s.volumes:
        out.append("<h2>Volumes</h2>")
        for v in s.volumes:
            title = {"stockpile": "Stockpiles", "cutfill": "Cut and fill", "change": "Change"}[v.kind]
            out.append(f"<h3>{e(title)}</h3>{_volume_table(v)}")
        out.append(cmd_links(s.commands, "volume") + cmd_links(s.commands, "cutfill"))
    changes = s.changes or ([s.change] if s.change else [])  # older sidecars keep only the latest
    if changes:
        out.append("<h2>Change between epochs</h2>")
    for c in changes:
        out.append(
            f"<h3>{e(c.surface.upper())}</h3>"
            + kv(
                [
                    (
                        "Compared with",
                        e(f"job {c.against_job}")
                        + f' <span class="sha">(job.json {e(c.against_sidecar_sha256)})</span>',
                    ),
                    ("This job", f'<span class="sha">job.json {e(c.this_sidecar_sha256)} at comparison</span>'),
                    ("Surface / area", e(f"{c.surface.upper()} · {c.area} · {c.res} m cells")),
                    ("Level of detection", e(f"{c.lod_m} m ({c.lod_rule}); |Δ| below it is 'no detectable change'")),
                    ("Gain", e(f"{c.gain_m3:,.2f} m³ over {c.gain_area_m2:,.1f} m²")),
                    ("Loss", e(f"{c.loss_m3:,.2f} m³ over {c.loss_area_m2:,.1f} m²")),
                    ("Net", e(f"{c.net_m3:+,.2f} m³")),
                    (
                        "No detectable change",
                        e(
                            f"{c.no_change_area_m2:,.1f} m² of {c.compared_area_m2:,.1f} m² compared; "
                            f"voids {c.void_percent:.2f}%"
                        ),
                    ),
                ]
            )
        )
        lod_kind = f"dod_{c.surface}_lod" if f"dod_{c.surface}_lod" in s.products else "dod_lod"
        ramp = [
            (-1.0, (33, 102, 172)),
            (-c.lod_m, (146, 197, 222)),
            (0, (247, 247, 247)),
            (c.lod_m, (244, 165, 130)),
            (1.0, (178, 24, 43)),
        ]
        out.append(
            figure(
                thumbnail(job, lod_kind, color=ramp),
                "DoD beyond the LoD: red = gain, blue = loss, white = no detectable change (colours saturate at ±1 m)",
            )
        )
    if changes:
        out.append(cmd_links(s.commands, "change"))
    return "\n".join(out)


def section_accuracy(job: Job) -> str:
    a = job.sidecar.accuracy
    if a is None:
        return ""
    out = [
        "<h2>Vertical accuracy</h2>",
        f'<div class="statement">{e(a.statement)}</div>',
        kv(
            [
                ("Standard", e(a.standard)),
                ("Surface tested", e(f"{a.surface.upper()}, sampled {a.sampling}")),
                (
                    "Check points",
                    e(f"{Path(a.checks_file.path).name}") + f' <span class="sha">{e(a.checks_file.sha256)}</span>',
                ),
                (
                    "Check point CRS",
                    e(a.checks_crs) + (e(f" → transformed by {a.transform.name}") if a.transform else ""),
                ),
                (
                    "Check point survey RMSE",
                    e(f"{a.checkpoint_rmse_m} m" if a.checkpoint_rmse_m is not None else "not supplied"),
                ),
            ]
        ),
    ]
    rows = [
        [
            e(g.group),
            e(g.n),
            e(g.excluded),
            num(g.min_m),
            num(g.max_m),
            num(g.mean_m),
            num(g.median_m),
            num(g.std_m),
            num(g.rmse_data_m),
            num(g.rmse_m),
            "pass/fail" if g.pass_fail_applies else "reported as found",
            "yes" if g.meets_minimum_count else '<strong style="color:var(--bad)">no</strong>',
        ]
        for g in a.groups
    ]
    out.append(
        table(
            [
                "Group",
                "n",
                "Excl.",
                "Min m",
                "Max m",
                "Mean m",
                "Median m",
                "Std m",
                "RMSE data m",
                "RMSE product m",
                "Use",
                "≥ 30",
            ],
            rows,
            {1, 2, 3, 4, 5, 6, 7, 8, 9},
        )
    )
    out.append("<p class='note'>RMSE product = √(RMSE data² + check point survey RMSE²) (ASPRS ed. 2 v2 §7.12.4).</p>")
    if a.notes:
        out.append("<ul>" + "".join(f"<li class='note'>{e(n)}</li>" for n in a.notes) + "</ul>")
    res_rows = [
        [
            e(r.id),
            e(r.group),
            e(f"{r.x:.3f}"),
            e(f"{r.y:.3f}"),
            e(f"{r.z_check:.3f}"),
            num(r.z_surface),
            num(r.residual),
            e(r.excluded or ""),
        ]
        for r in a.residuals
    ]
    out.append(
        "<h3>Residuals (surface − check)</h3>"
        + table(["Id", "Group", "E", "N", "Check z", "Surface z", "Residual", "Excluded"], res_rows, {2, 3, 4, 5, 6})
    )
    out.append(cmd_links(job.sidecar.commands, "accuracy"))
    return "\n".join(out)


def section_profiles(job: Job) -> str:
    if not job.sidecar.profiles:
        return ""
    p = job.sidecar.profiles[-1]
    svg_path = job.path("profile.svg")
    svg = svg_path.read_text(encoding="utf-8") if svg_path.is_file() else ""
    return "\n".join(
        [
            "<h2>Profile</h2>",
            f"<figure>{svg}<figcaption>{e(p.line)}: {p.length_m:,.1f} m sampled every {p.step_m} m "
            f"({', '.join(s.upper() for s in p.surfaces)}, bilinear; gaps are voids)</figcaption></figure>",
            cmd_links(job.sidecar.commands, "profile"),
        ]
    )


def section_products(job: Job) -> str:
    rows = [
        [
            e(k),
            f"<code>{e(p.path)}</code>",
            e(p.description),
            e(f"{p.size:,}"),
            f'<span class="sha">{e(p.sha256)}</span>',
        ]
        for k, p in sorted(job.sidecar.products.items())
        if k != "report"
    ]
    return "<h2>Products</h2>" + table(["Kind", "File", "Description", "Bytes", "SHA256"], rows, {3})


def section_commands(job: Job) -> str:
    out = [
        f"<h2>Command log</h2><p class='note'>{len(job.sidecar.commands)} external commands, in run order, "
        "exactly as executed (PDAL pipelines were fed on stdin as shown).</p>"
    ]
    for i, c in enumerate(job.sidecar.commands, 1):
        meta = f"#{i} · {e(c.step)} · {e(c.tool)} · {c.seconds:.2f} s · exit {c.returncode} · {e(c.started_at.isoformat(' ', 'seconds'))}"
        body = f"<pre>{e(' '.join(_quote(a) for a in c.argv))}</pre>"
        if c.pipeline is not None:
            body += f"<pre>{e(json.dumps(c.pipeline, separators=(',', ':')))}</pre>"
        out.append(f'<div class="cmd" id="cmd-{i}"><div class="meta">{meta}</div>{body}</div>')
    return "\n".join(out)


def _quote(arg: str) -> str:
    return f"'{arg}'" if any(ch in arg for ch in " ()[]{}*?&|;<>$\"'") else arg


def report(job: Job) -> Path:
    """Write ``report.html`` for *job* and register it as a product."""
    if not job.sidecar.steps_done:
        raise SurveyError(f"job {job.id!r} has no steps yet — nothing to report")
    with job.step(STEP):
        body = "\n".join(
            s
            for s in (
                section_summary(job),
                section_crs(job),
                section_qa(job),
                section_ground(job),
                section_surfaces(job),
                section_volumes(job),
                section_accuracy(job),
                section_profiles(job),
                section_products(job),
                section_commands(job),
            )
            if s
        )
        doc = (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Survey report {e(job.id)}</title><style>{CSS}</style></head>"
            f"<body>\n{body}\n<p class='note'>argus-survey {e(__version__)} · job.json is the machine-readable record "
            "of everything above.</p></body></html>\n"
        )
        out = job.path("report.html")
        out.write_text(doc, encoding="utf-8")
        job.add_product("report", "report.html", step=STEP, description="Survey report (HTML, printable)")
    return out
