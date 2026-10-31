"""Surfaces and contours against the analytic scene."""

from __future__ import annotations

import json
import math

import pytest

from argus_survey import rasters, synthetic
from argus_survey.contours import contours
from argus_survey.ground import ground
from argus_survey.jobs import JobStore
from argus_survey.qa import qa
from argus_survey.surfaces import surfaces

pytestmark = pytest.mark.tools

RES = 0.5


@pytest.fixture(scope="module")
def surf_job(las_mga_ahd, tmp_path_factory):  # type: ignore[no-untyped-def]
    from argus_survey.ingest import ingest

    job = JobStore(tmp_path_factory.mktemp("surf")).open("s", create=True)
    ingest(job, [las_mga_ahd])
    qa(job)
    ground(job)
    surfaces(job, res=RES)
    return job


def _xy(scene: synthetic.Scene, x: float, y: float) -> tuple[float, float]:
    return scene.origin[0] + x, scene.origin[1] + y


def test_grid_origin_is_snapped(surf_job) -> None:  # type: ignore[no-untyped-def]
    for kind in ("dtm", "dsm", "chm", "hillshade"):
        g = rasters.grid_of(surf_job.product_path(kind))
        assert g.res == RES
        assert math.isclose(g.origin_x / RES, round(g.origin_x / RES), abs_tol=1e-9), kind
        assert math.isclose(g.origin_y / RES, round(g.origin_y / RES), abs_tol=1e-9), kind
        assert g == surf_job.sidecar.surfaces.grid


def test_dtm_matches_plane_on_open_ground(surf_job, scene) -> None:  # type: ignore[no-untyped-def]
    cps = synthetic.check_points(scene, n_open=25, n_vegetated=0)
    vals = rasters.sample(surf_job.product_path("dtm"), [(c.id, c.x, c.y) for c in cps], crs="EPSG:7850")
    errs = [vals[c.id] - c.z for c in cps if vals[c.id] is not None]
    assert len(errs) >= 20
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
    assert rmse < 0.03, rmse  # noise σ 0.02 m, averaged by IDW


def test_dsm_sees_roof_and_chm_sees_trees(surf_job, scene) -> None:  # type: ignore[no-untyped-def]
    x0, y0, x1, y1 = scene.building
    bx, by = _xy(scene, (x0 + x1) / 2, (y0 + y1) / 2)
    t = scene.trees[2]
    tx, ty = _xy(scene, t.x, t.y)
    dsm = rasters.sample(
        surf_job.product_path("dsm"), [("roof", bx, by), ("tree", tx, ty)], crs="EPSG:7850", resampling="nearest"
    )
    assert dsm["roof"] == pytest.approx(scene.roof_z(), abs=0.1)
    chm = rasters.sample(
        surf_job.product_path("chm"), [("tree", tx, ty), ("roof", bx, by)], crs="EPSG:7850", resampling="nearest"
    )
    assert chm["tree"] == pytest.approx(t.height, abs=0.3)
    assert chm["roof"] == pytest.approx(scene.building_height, abs=0.5)


def test_voids_are_measured(surf_job, scene) -> None:  # type: ignore[no-untyped-def]
    rep = surf_job.sidecar.surfaces.surfaces
    # no ground under the 10 x 8 m building: about 80 m² of the ~4800 m² site, less the IDW reach
    assert 0.5 < rep["dtm"].void_percent < 2.5
    assert rep["dsm"].void_percent < 0.5
    assert surf_job.product_path("dtm_voids").is_file()
    assert "r = 0.707 m" in rep["dtm"].method


def test_fill_voids_and_resolution_warning(las_mga_ahd, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from argus_survey.ingest import ingest

    job = JobStore(tmp_path).open("f", create=True)
    ingest(job, [las_mga_ahd])
    qa(job)
    ground(job)
    r = surfaces(job, res=0.2, dsm=False, chm=False, fill_voids=40)
    assert r.resolution_warning and "finer than the ground supports" in r.resolution_warning
    dtm = r.surfaces["dtm"]
    assert dtm.void_percent > 0 and dtm.void_percent_after_fill == 0 and dtm.filled_max_distance == 40


def test_surfaces_refuse_without_vertical(las_mga_only, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from argus_survey.ingest import ingest
    from argus_survey.models import SurveyError

    job = JobStore(tmp_path).open("nov", create=True)
    ingest(job, [las_mga_only])
    with pytest.raises(SurveyError, match="no vertical datum"):
        surfaces(job, res=1.0)


def test_contours(surf_job) -> None:  # type: ignore[no-untyped-def]
    r = contours(surf_job, interval=0.5, index=5)
    assert r.features > 10
    dtm = surf_job.sidecar.surfaces.surfaces["dtm"].stats
    assert dtm.minimum - 0.5 <= r.min_elev and r.max_elev <= dtm.maximum + 0.5
    gj = json.loads(surf_job.product_path("contours_geojson").read_text())
    elevs = {round(f["properties"]["ELEV"], 6) for f in gj["features"]}
    assert all(abs(e / 0.5 - round(e / 0.5)) < 1e-6 for e in elevs)
    for f in gj["features"]:
        e = f["properties"]["ELEV"]
        assert f["properties"]["idx"] == (1 if abs(e / 2.5 - round(e / 2.5)) < 1e-6 else 0)
    assert any(f["properties"]["idx"] == 1 for f in gj["features"])
