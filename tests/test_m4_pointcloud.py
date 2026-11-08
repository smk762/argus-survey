"""Volumes, change, accuracy and profiles end to end from synthetic point clouds."""

from __future__ import annotations

import csv
import json
import math
import subprocess
from pathlib import Path

import pytest

from argus_survey import synthetic
from argus_survey.accuracy import accuracy
from argus_survey.change import change
from argus_survey.ground import ground
from argus_survey.ingest import ingest
from argus_survey.jobs import JobStore
from argus_survey.models import SurveyError
from argus_survey.profile import profile
from argus_survey.qa import qa
from argus_survey.surfaces import surfaces
from argus_survey.tools import child_env
from argus_survey.volume import volume

pytestmark = pytest.mark.tools

RES = 0.25


def processed(root: Path, job_id: str, las: Path, res: float = RES):  # type: ignore[no-untyped-def]
    job = JobStore(root).open(job_id, create=True)
    ingest(job, [las])
    qa(job)
    ground(job)
    surfaces(job, res=res, chm=False)
    return job


@pytest.fixture(scope="module")
def epochs(tmp_path_factory, synth_dir):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("epochs")
    s1 = synthetic.Scene()
    s2 = synthetic.epoch2(s1)
    l1 = synthetic.write_scene(s1, synth_dir / "epoch1.laz")
    l2 = synthetic.write_scene(s2, synth_dir / "epoch2.laz")
    return s1, s2, processed(root, "epoch1", l1), processed(root, "epoch2", l2)


def pile_polygon(scene: synthetic.Scene, path: Path, margin: float = 3.0) -> Path:
    e0, n0 = scene.origin
    cx, cy = e0 + scene.cone_centre[0], n0 + scene.cone_centre[1]
    r = scene.cone_radius + margin
    ring = [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r], [cx - r, cy - r]]
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": "EPSG:7850"}},
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "ROM pad 1"},
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                    }
                ],
            }
        )
    )
    return path


def test_stockpile_volume_from_points(epochs, tmp_path) -> None:  # type: ignore[no-untyped-def]
    s1, _, j1, _ = epochs
    r = volume(j1, pile_polygon(s1, tmp_path / "pile.geojson"), base="plane", surface="dsm")
    z = r.zones[0]
    assert z.name == "ROM pad 1"
    # ~55k noisy points → IDW DSM at 0.25 m → plane base: within 3 % of πr²h/3
    assert z.above_m3 == pytest.approx(s1.cone_volume, rel=0.03), z
    assert z.base_fit_rms_m < 0.05
    assert json.loads(j1.product_path("volume").read_text())["zones"][0]["name"] == "ROM pad 1"
    # grid → ground: MGA zone 50 west of the central meridian shrinks grid lengths (k < 1), so ground is larger
    c = r.combined_scale_factor
    assert c is not None and c == j1.sidecar.ingest.scale_factors.combined and c < 1
    assert z.ground_above_m3 == pytest.approx(z.above_m3 / c**2, abs=2e-3) and z.ground_above_m3 > z.above_m3
    assert z.ground_area_m2 == pytest.approx(z.area_m2 / c**2, abs=2e-3)


def test_change_matches_injected_growth(epochs, tmp_path) -> None:  # type: ignore[no-untyped-def]
    s1, s2, j1, j2 = epochs
    grown = s2.cone_volume - s1.cone_volume  # π r² Δh / 3
    pad = pile_polygon(s1, tmp_path / "pad.geojson")
    r0 = change(j2, j1, surface="dsm", lod=0.0, polygon=pad)
    assert r0.gain_m3 - r0.loss_m3 == pytest.approx(grown, rel=0.06), r0
    r = change(j2, j1, surface="dsm", lod=0.10, polygon=pad)
    # cells where Δ < 0.10 m (the outer tenth of the cone's radius) drop out: 2π r² Δh ∫₀.₉¹ (1−t)t dt
    rim = 2 * math.pi * s1.cone_radius**2 * 1.0 * ((0.5 - 1 / 3) - (0.9**2 / 2 - 0.9**3 / 3))
    assert r.gain_m3 == pytest.approx(grown - rim, rel=0.06), r
    assert r.loss_m3 < 1.0  # noise alone (σ 2 cm per epoch) never reaches a 10 cm LoD in bulk
    # site-wide, the few outliers each epoch's QA missed (different birds each time) show up as change:
    # the DoD reports them rather than hiding them
    site = change(j2, j1, surface="dsm", lod=0.10)
    assert site.gain_m3 > r.gain_m3 and site.area.startswith("whole")
    assert r.against_sidecar_sha256 and r.this_sidecar_sha256 and r.lod_rule == "--lod 0.1"
    assert j2.product_path("dod_lod").is_file()


def test_change_refuses_misaligned_grids(epochs, tmp_path, las_mga_ahd) -> None:  # type: ignore[no-untyped-def]
    _, _, j1, _ = epochs
    other = processed(tmp_path, "coarse", las_mga_ahd, res=0.3)
    with pytest.raises(SurveyError, match="don't align"):
        change(other, j1)


def _checks_csv(path: Path, pts: list[synthetic.CheckPoint]) -> Path:
    return synthetic.write_check_points(pts, path)


def test_accuracy_rmse_matches_injected_noise(epochs, tmp_path) -> None:  # type: ignore[no-untyped-def]
    s1, _, j1, _ = epochs
    cps = synthetic.check_points(s1, n_open=40, n_vegetated=10, z_noise=0.05)
    path = _checks_csv(tmp_path / "checks.csv", cps)
    with pytest.raises(SurveyError, match="--crs"):
        accuracy(j1, path, checks_crs=None)
    with pytest.raises(SurveyError, match="no vertical datum"):
        accuracy(j1, path, checks_crs="EPSG:7850")
    r = accuracy(j1, path, checks_crs="EPSG:7850+5711", checkpoint_rmse=0.01, target_class_cm=10)
    nva = next(g for g in r.groups if g.group == "NVA")
    vva = next(g for g in r.groups if g.group == "VVA")
    assert nva.n == 40 and nva.meets_minimum_count and nva.pass_fail_applies
    assert 0.035 < nva.rmse_data_m < 0.07, nva  # ≈ the 5 cm injected into the check heights
    assert nva.rmse_m == pytest.approx(math.sqrt(nva.rmse_data_m**2 + 0.01**2), abs=1e-4)
    assert not vva.pass_fail_applies and not vva.meets_minimum_count
    assert r.statement.startswith("This data set was tested to meet ASPRS") and "95" not in r.statement
    assert "Version 2 (2024)" in r.statement and "for a 10 (cm) RMSEV Vertical Accuracy Class" in r.statement
    assert "ONLY 10 checkpoints" in r.statement  # VVA has 10: §7.16.1's reduced-checkpoint wording
    assert nva.min_m <= nva.median_m <= nva.max_m
    assert any("at least 30" in n for n in r.notes)
    with j1.product_path("residuals").open() as fh:
        assert len(list(csv.DictReader(fh))) == 50


def test_accuracy_transforms_check_points(epochs, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Check points given as GDA2020 lat/lon + ellipsoidal height reach the same residuals via cct."""
    s1, _, j1, _ = epochs
    cps = synthetic.check_points(s1, n_open=30, n_vegetated=0, z_noise=0.05)
    direct = accuracy(j1, _checks_csv(tmp_path / "mga.csv", cps), checks_crs="EPSG:7850+5711")
    stdin = "".join(f"{c.x} {c.y} {c.z}\n" for c in cps)
    out = subprocess.run(
        ["cs2cs", "EPSG:7850+5711", "EPSG:7843", "-f", "%.10f"],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        env=child_env(),
    ).stdout.split("\n")
    geo = tmp_path / "geo.csv"
    with geo.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "x", "y", "z", "description"])
        for c, line in zip(cps, out, strict=False):
            lat, lon, h = line.split()
            w.writerow([c.id, lon, lat, h, "open"])  # x = longitude: GIS axis order, as the docs say
    moved = accuracy(j1, geo, checks_crs="EPSG:7843")
    assert moved.transform is not None and "au_ga_AUSGeoid2020_20180201.tif" in moved.transform.grids
    a = {r.id: r.residual for r in direct.residuals}
    b = {r.id: r.residual for r in moved.residuals}
    assert all(abs(a[k] - b[k]) < 0.002 for k in a if a[k] is not None)


def test_profile_across_the_stockpile(epochs, tmp_path) -> None:  # type: ignore[no-untyped-def]
    s1, _, j1, _ = epochs
    e0, n0 = s1.origin
    cx, cy = e0 + s1.cone_centre[0], n0 + s1.cone_centre[1]
    line = tmp_path / "line.geojson"
    line.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": "EPSG:7850"}},
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "XS-1"},
                        "geometry": {"type": "LineString", "coordinates": [[cx - 15, cy], [cx + 15, cy]]},
                    }
                ],
            }
        )
    )
    r = profile(j1, line, step=0.25)
    assert r.length_m == pytest.approx(30) and r.samples > 100
    rows = list(csv.DictReader(j1.product_path("profile").open()))
    top = max(float(row["dsm"]) for row in rows if row["dsm"])
    assert top == pytest.approx(s1.ground_z(*s1.cone_centre) + s1.cone_height, abs=0.25)
    assert "<polyline" in j1.product_path("profile_svg").read_text()
