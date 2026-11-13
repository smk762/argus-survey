"""Ingest against real PDAL/PROJ on synthetic scenes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_survey import synthetic
from argus_survey.ingest import info_report, ingest, summarize
from argus_survey.jobs import JobStore
from argus_survey.models import SurveyError

pytestmark = pytest.mark.tools


def test_info_reads_compound_crs(las_mga_ahd: Path) -> None:
    report = info_report(las_mga_ahd)
    assert report["crs"]["definition"] == "EPSG:7850+5711"
    assert report["crs"]["vertical"]["epsg"] == 5711


def test_ingest_compound_no_transform(las_mga_ahd: Path, jobs_root: Path, scene: synthetic.Scene) -> None:
    job = JobStore(jobs_root).open("site1", create=True)
    rec = ingest(job, [las_mga_ahd])
    src, _ = summarize(las_mga_ahd)
    assert rec.transform is None and rec.output_count == src.count == rec.inputs_count
    assert job.sidecar.crs is not None and job.sidecar.crs.definition == "EPSG:7850+5711"
    assert job.sidecar.crs.height_reference == "gravity" and job.sidecar.crs.vertical_assumed is None
    copc = job.product_path("copc")
    out, srs = summarize(copc)
    assert out.copc and out.count == src.count
    assert len(job.sidecar.products["copc"].sha256) == 64
    tools = [c.tool for c in job.sidecar.commands]
    assert tools[0] == "pdal" and "untwine" in tools
    assert any(c.pipeline for c in job.sidecar.commands), "the PDAL pipeline JSON must be recorded"
    assert rec.source_type == "lidar"
    cls = next(s for s in rec.stats if s.name == "Classification")
    assert cls.counts == {"1": src.count}
    inp = job.sidecar.inputs[0]
    assert len(inp.sha256) == 64 and inp.size == las_mga_ahd.stat().st_size
    # the sidecar on disk is the one in memory
    assert json.loads((job.dir / "job.json").read_text())["ingest"]["output_count"] == src.count


def test_ingest_ellipsoidal_to_mga_ahd_matches_original(
    las_ellipsoidal: Path, las_mga_ahd: Path, jobs_root: Path
) -> None:
    job = JobStore(jobs_root).open("ell", create=True)
    rec = ingest(job, [las_ellipsoidal], t_srs="EPSG:7850+5711")
    assert rec.transform is not None
    assert rec.transform.grids == ["au_ga_AUSGeoid2020_20180201.tif"]
    assert job.sidecar.crs is not None and job.sidecar.crs.geoid == "au_ga_AUSGeoid2020_20180201.tif"
    assert rec.source_crs.height_reference == "ellipsoidal"
    out, _ = summarize(job.product_path("copc"))
    orig, _ = summarize(las_mga_ahd)
    for k in ("minx", "miny", "minz", "maxx", "maxy", "maxz"):
        assert getattr(out.bounds, k) == pytest.approx(getattr(orig.bounds, k), abs=0.003), k
    # the geoid really was applied: ellipsoidal heights near Perth sit ~30 m below AHD
    ell, _ = summarize(las_ellipsoidal)
    assert orig.bounds.minz - ell.bounds.minz > 25


def test_ingest_horizontal_only_records_no_vertical(las_mga_only: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("h", create=True)
    ingest(job, [las_mga_only])
    assert job.sidecar.crs is not None and job.sidecar.crs.vertical is None
    with pytest.raises(SurveyError, match="no vertical datum"):
        job.require_crs("volume", height=True)


def test_ingest_refuses_vertical_target_from_unknown_heights(las_mga_only: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("h2", create=True)
    with pytest.raises(SurveyError, match="unknown datum"):
        ingest(job, [las_mga_only], t_srs="EPSG:7850+5711")


def test_ingest_assume_vertical_is_recorded(las_mga_only: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("assumed", create=True)
    ingest(job, [las_mga_only], assume_vertical="EPSG:5711")
    crs = job.sidecar.crs
    assert crs is not None and crs.vertical is not None and crs.vertical.epsg == 5711
    assert crs.vertical_assumed and "EPSG:5711" in crs.vertical_assumed
    assert any("ASSUMED" in w for w in job.sidecar.warnings)
    _, srs = summarize(job.product_path("copc"))
    assert "AHD" in json.dumps(srs["json"])


def test_ingest_assume_vertical_conflicts_with_declared(las_mga_ahd: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("c", create=True)
    with pytest.raises(SurveyError, match="already has a vertical datum"):
        ingest(job, [las_mga_ahd], assume_vertical="EPSG:5711")


def test_ingest_without_crs_refuses_then_accepts_s_srs(las_no_srs: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("nosrs", create=True)
    with pytest.raises(SurveyError, match="--s-srs"):
        ingest(job, [las_no_srs])
    rec = ingest(job, [las_no_srs], s_srs="EPSG:7850+5711")
    assert rec.source_crs.source == "user"
    _, srs = summarize(job.product_path("copc"))
    assert srs["json"]["name"] == "GDA2020 / MGA zone 50 + AHD height"


def test_ingest_feet_refused(scene: synthetic.Scene, synth_dir: Path, jobs_root: Path) -> None:
    feet = synthetic.write_scene(scene, synth_dir / "feet.laz", srs="EPSG:2992")
    job = JobStore(jobs_root).open("ft", create=True)
    with pytest.raises(SurveyError, match="metres"):
        ingest(job, [feet])


def test_crop_polygon_needs_a_believable_crs(
    las_mga_ahd: Path, jobs_root: Path, scene: synthetic.Scene, tmp_path: Path
) -> None:
    e0, n0 = scene.origin
    ring = [[e0 + 10, n0 + 20], [e0 + 30, n0 + 20], [e0 + 30, n0 + 40], [e0 + 10, n0 + 40], [e0 + 10, n0 + 20]]
    poly = tmp_path / "pile.geojson"
    poly.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
                ],
            }
        )
    )
    job = JobStore(jobs_root).open("crop", create=True)
    with pytest.raises(SurveyError, match="--crop-srs"):
        ingest(job, [las_mga_ahd], crop=poly)
    rec = ingest(job, [las_mga_ahd], crop=poly, crop_srs="EPSG:7850")
    assert rec.cropped and 0 < rec.output_count < rec.inputs_count
    out, _ = summarize(job.product_path("copc"))
    assert out.bounds.minx >= e0 + 10 - 1e-3 and out.bounds.maxx <= e0 + 30 + 1e-3


def test_photogrammetry_detected(las_photogrammetry: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("photo", create=True)
    assert ingest(job, [las_photogrammetry]).source_type == "photogrammetry"


def test_reingest_needs_force(las_mga_ahd: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("again", create=True)
    ingest(job, [las_mga_ahd])
    with pytest.raises(SurveyError, match="already ingested"):
        ingest(job, [las_mga_ahd])
    ingest(job, [las_mga_ahd], force=True)
    assert job.sidecar.steps_done.count("ingest") == 1


def test_directory_of_tiles(scene: synthetic.Scene, tmp_path: Path, jobs_root: Path) -> None:
    from dataclasses import replace

    tiles = tmp_path / "tiles"
    a = synthetic.write_scene(replace(scene, width=30, height=20), tiles / "a.laz")
    b = synthetic.write_scene(
        replace(scene, width=30, height=20, origin=(391030.0, 6464000.0), seed=5), tiles / "b.las"
    )
    job = JobStore(jobs_root).open("tiles", create=True)
    rec = ingest(job, [tiles])
    assert len(job.sidecar.inputs) == 2
    assert rec.output_count == summarize(a)[0].count + summarize(b)[0].count


def test_e57_ingest_needs_s_srs_and_keeps_coordinates(
    scene: synthetic.Scene, synth_dir: Path, jobs_root: Path, las_mga_ahd: Path
) -> None:
    from argus_survey.ground import ground

    e57 = synthetic.write_scene(scene, synth_dir / "scan.e57")
    store = JobStore(jobs_root)
    with pytest.raises(SurveyError, match="no CRS"):
        ingest(store.open("e57-nocrs", create=True), [e57])

    job = store.open("e57", create=True)
    rec = ingest(job, [e57], s_srs="EPSG:7850+5711")
    ref, _ = summarize(las_mga_ahd)
    out, _ = summarize(job.product_path("copc"))
    assert rec.output_count == ref.count
    assert job.sidecar.inputs[0].summary.copc is False
    # double precision through E57 and mm scale into the COPC: the bounds agree with the LAS to the mm
    for k in ("minx", "miny", "minz", "maxx", "maxy", "maxz"):
        assert getattr(out.bounds, k) == pytest.approx(getattr(ref.bounds, k), abs=0.0015), k
    assert rec.source_type == "terrestrial"
    assert any("E57 declares no CRS" in w for w in job.sidecar.warnings)
    assert any(c.pipeline and c.pipeline[0]["type"] == "readers.e57" for c in job.sidecar.commands)
    # no return numbering in E57: the parts carry 1-of-1, which SMRF's return filter accepts
    g = ground(job)
    assert g.ground_percent > 50 and not g.normalised_returns


def test_unsupported_input_suffix(tmp_path: Path, jobs_root: Path) -> None:
    f = tmp_path / "scan.ply"
    f.write_text("ply\n")
    with pytest.raises(SurveyError, match="unsupported input format"):
        ingest(JobStore(jobs_root).open("ply", create=True), [f])


def test_scale_factors_mga_ahd(las_mga_ahd: Path, jobs_root: Path, scene: synthetic.Scene) -> None:
    import math

    job = JobStore(jobs_root).open("scale", create=True)
    sf = ingest(job, [las_mga_ahd]).scale_factors
    assert sf is not None
    # UTM: k ≈ k0 (1 + Δx² / 2R²) with Δx the offset from the central meridian's 500 km false easting
    dx = sf.easting - 500000.0
    assert sf.point_scale == pytest.approx(0.9996 * (1 + dx * dx / (2 * sf.radius_m**2)), abs=2e-6)
    assert sf.point_scale_min <= sf.point_scale <= sf.point_scale_max
    # AUSGeoid2020 around Perth: the ellipsoid lies some 30 m from AHD
    assert sf.geoid_separation_m is not None and 25 < abs(sf.geoid_separation_m) < 35
    assert sf.operation and sf.ellipsoidal_height_m == pytest.approx(sf.height_m + sf.geoid_separation_m, abs=2e-3)
    assert sf.height_factor == pytest.approx(sf.radius_m / (sf.radius_m + sf.ellipsoidal_height_m), abs=1e-8)
    assert sf.combined == pytest.approx(sf.point_scale * sf.height_factor, abs=1e-8)
    assert math.isclose(sf.latitude, -31.9, abs_tol=0.2) and math.isclose(sf.longitude, 115.8, abs_tol=0.2)


def test_scale_factor_pcg2020_is_near_one(las_mga_ahd: Path, jobs_root: Path) -> None:
    job = JobStore(jobs_root).open("pcg", create=True)
    sf = ingest(job, [las_mga_ahd], t_srs="EPSG:8031+5711").scale_factors
    assert sf is not None and sf.combined is not None
    assert abs(sf.point_scale - 1) < 2e-5  # a low-distortion grid: what LSG2020 is for


def test_scale_factor_without_vertical_has_no_height_factor(las_mga_only: Path, jobs_root: Path) -> None:
    sf = ingest(JobStore(jobs_root).open("scale-nov", create=True), [las_mga_only]).scale_factors
    assert sf is not None and sf.height_factor is None and sf.combined is None and "no height factor" in sf.method
