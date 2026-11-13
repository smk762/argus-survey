"""Change between two epochs: a DEM of difference (later − earlier) with a level of detection.

Both jobs must share CRS (horizontal *and* vertical) and a grid lattice — which the snapped
grids of §6.4 make the normal case for two surveys processed at the same resolution — so the
difference is cell for cell, never resampled. Only the overlap of the two grids is compared.

Cells with |Δ| below the LoD are "no detectable change" (0 in ``dod_<surface>_lod.tif``). The LoD is
either given (``--lod``, default 0.10 m) or derived from both epochs' accuracy statements:
LoD95 = 1.96 · √(σ₁² + σ₂²), with σ the NVA RMSE of each epoch (the usual DoD
error-propagation rule; unrelated to ASPRS reporting, which no longer uses 95 %).
Both sidecars are pinned by SHA256 so the comparison is reproducible.

Outputs are per surface (``dod_dtm.tif``, ``dod_dsm_lod.tif``, ``change_dtm.json`` …), so a DTM
and a DSM comparison live side by side; ``sidecar.changes`` holds the latest run per surface.
The ``change``/``dod``/``dod_lod`` products and ``sidecar.change`` name the most recent run.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from argus_survey import crs as crs_mod
from argus_survey import grid as grid_mod
from argus_survey import rasters
from argus_survey.jobs import Job
from argus_survey.models import SIDECAR_NAME, ChangeReport, GridSpec, SurveyError
from argus_survey.qa import raster_srs
from argus_survey.tools import run, sha256_file
from argus_survey.vectors import read_polygons, write_zones
from argus_survey.volume import difference_volumes

STEP = "change"
DEFAULT_LOD = 0.10


def overlap(a: GridSpec, b: GridSpec) -> GridSpec:
    """The common window of two aligned grids."""
    ax0, ay0, ax1, ay1 = a.extent
    bx0, by0, bx1, by1 = b.extent
    x0, y0, x1, y1 = max(ax0, bx0), max(ay0, by0), min(ax1, bx1), min(ay1, by1)
    if x1 <= x0 or y1 <= y0:
        raise SurveyError("the two epochs do not overlap")
    return GridSpec(
        origin_x=x0, origin_y=y0, res=a.res, width=round((x1 - x0) / a.res), height=round((y1 - y0) / a.res)
    )


def _nva_rmse(job: Job) -> float | None:
    acc = job.sidecar.accuracy
    if acc is None:
        return None
    nva = next((g for g in acc.groups if g.group == "NVA"), None)
    return nva.rmse_m if nva else None


def change(
    job: Job,
    against: Job,
    *,
    surface: str = "dtm",
    lod: float | None = None,
    lod_from_accuracy: bool = False,
    polygon: Path | None = None,
    polygon_srs: str | None = None,
) -> ChangeReport:
    """DoD of *job* (later) minus *against* (earlier); over *polygon* if given, else the whole overlap."""
    job.require_crs(STEP, height=True)
    against.require_crs(STEP, height=True)
    if job.id == against.id:
        raise SurveyError("compare two different jobs")
    if surface not in ("dtm", "dsm"):
        raise SurveyError(f"--surface must be dtm or dsm, not {surface!r}")
    a_crs, b_crs = job.sidecar.crs, against.sidecar.crs
    assert a_crs is not None and b_crs is not None
    if not crs_mod.same_crs(a_crs, b_crs):
        raise SurveyError(
            f"the epochs are in different CRSs ({a_crs.name} vs {b_crs.name}); ingest both with the same --t-srs"
        )
    if job.sidecar.surfaces is None or against.sidecar.surfaces is None:
        raise SurveyError("both jobs need surfaces")
    ga, gb = job.sidecar.surfaces.grid, against.sidecar.surfaces.grid
    if not grid_mod.aligned(ga, gb):
        raise SurveyError(
            f"the epochs' grids don't align ({ga.res} m at {ga.origin_x}, {ga.origin_y} vs {gb.res} m at "
            f"{gb.origin_x}, {gb.origin_y}); run surfaces at the same --res for both"
        )
    later = job.product_path(surface, needed_by=STEP)
    earlier = against.product_path(surface, needed_by=STEP)

    if lod_from_accuracy:
        s1, s2 = _nva_rmse(job), _nva_rmse(against)
        if s1 is None or s2 is None:
            raise SurveyError("--lod-from-accuracy needs an accuracy statement (NVA) in both jobs")
        lod_m = 1.96 * math.sqrt(s1 * s1 + s2 * s2)
        rule = f"LoD95 = 1.96·√({s1:.3f}² + {s2:.3f}²) from both epochs' NVA RMSE"
    else:
        lod_m = DEFAULT_LOD if lod is None else lod
        rule = f"--lod {lod_m}" if lod is not None else f"default {DEFAULT_LOD} m"
    if lod_m < 0:
        raise SurveyError("--lod must be ≥ 0")

    this_digest = sha256_file(job.path(SIDECAR_NAME))
    other_digest = sha256_file(against.path(SIDECAR_NAME))
    sink = job.sink
    srs = raster_srs(job)
    work = job.path(".change-work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        with job.step(STEP):
            win = overlap(ga, gb)
            up, lo = work / "later.tif", work / "earlier.tif"
            for src, dst in ((later, up), (earlier, lo)):
                run(
                    [
                        "gdalwarp",
                        "-q",
                        "-overwrite",
                        "-r",
                        "near",
                        *grid_mod.gdal_te_tr(win),
                        "-dstnodata",
                        repr(rasters.NODATA),
                        "-co",
                        "COMPRESS=DEFLATE",
                        str(src),
                        str(dst),
                    ],
                    step=STEP,
                    sink=sink,
                )
            x0, y0, x1, y1 = win.extent
            if polygon is not None:  # all features count as one compared area
                zone = [
                    ("compared area", w)
                    for _, w in read_polygons(polygon, srs, src_srs=polygon_srs, sink=sink, step=STEP)
                ]
            else:
                zone = [("compared area", f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))")]
            zfile = write_zones(zone, work / "zone.gpkg", srs, sink=sink, step=STEP)
            v = difference_volumes(up, lo, zfile, win, work, lod=lod_m, sink=sink, step=STEP)["compared area"]

            void = "isnodata(A) || isnodata(B)"
            dod, dod_lod, report_name = f"dod_{surface}.tif", f"dod_{surface}_lod.tif", f"change_{surface}.json"
            rasters.calc(
                job.path(dod), f"{void} ? {rasters.NODATA!r} : A - B", {"A": up, "B": lo}, sink=sink, step=STEP
            )
            rasters.calc(
                job.path(dod_lod),
                f"{void} ? {rasters.NODATA!r} : (abs(A - B) < {lod_m!r} ? 0 : A - B)",
                {"A": up, "B": lo},
                sink=sink,
                step=STEP,
            )
            for p in (job.path(dod), job.path(dod_lod)):
                rasters.set_crs(p, srs, sink=sink, step=STEP)

            area = v["area_m2"]
            valid = area - v["void_m2"]
            report = ChangeReport(
                against_job=against.id,
                against_sidecar_sha256=other_digest,
                this_sidecar_sha256=this_digest,
                surface=surface,
                lod_m=round(lod_m, 4),
                lod_rule=rule,
                gain_m3=round(v["above_m3"], 3),
                loss_m3=round(v["below_m3"], 3),
                net_m3=round(v["above_m3"] - v["below_m3"], 3),
                gain_area_m2=round(v["area_above_m2"], 3),
                loss_area_m2=round(v["area_below_m2"], 3),
                no_change_area_m2=round(valid - v["area_above_m2"] - v["area_below_m2"], 3),
                compared_area_m2=round(valid, 3),
                void_percent=round(100.0 * v["void_m2"] / area, 3) if area else 100.0,
                res=win.res,
                area=polygon.name if polygon is not None else "whole overlap of the two epochs",
            )
            job.sidecar.change = report
            job.sidecar.changes = [c for c in job.sidecar.changes if c.surface != surface] + [report]
            job.path(report_name).write_text(report.model_dump_json(indent=2), encoding="utf-8")
            desc = f"{surface.upper()} change against {against.id}"
            dod_desc = f"{surface.upper()} of difference (later − earlier)"
            lod_desc = f"{surface.upper()} DoD with |Δ| < {lod_m:.3f} m set to 0 (no detectable change)"
            outputs = (
                ("change", f"change_{surface}", report_name, desc),
                ("dod", f"dod_{surface}", dod, dod_desc),
                ("dod_lod", f"dod_{surface}_lod", dod_lod, lod_desc),
            )
            for kind, per_surface, name, description in outputs:
                # per surface, and the latest run under the plain kind; the file is hashed once
                product = job.add_product(per_surface, name, step=STEP, description=description)
                job.sidecar.products[kind] = product.model_copy(update={"kind": kind})
            job.save()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report
