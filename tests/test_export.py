"""Point-cloud exports for ReCap / Civil 3D."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_survey import synthetic
from argus_survey.export import export_points
from argus_survey.ground import ground
from argus_survey.ingest import ingest, summarize
from argus_survey.jobs import JobStore
from argus_survey.models import SurveyError
from argus_survey.tools import run_json

pytestmark = pytest.mark.tools


@pytest.fixture(scope="module")
def classified(las_mga_ahd, tmp_path_factory):  # type: ignore[no-untyped-def]
    job = JobStore(tmp_path_factory.mktemp("export")).open("x", create=True)
    ingest(job, [las_mga_ahd])
    ground(job)
    return job


def test_las_export_is_complete_uncompressed_and_georeferenced(classified) -> None:  # type: ignore[no-untyped-def]
    r = export_points(classified, "las")
    path = classified.path(r.path)
    s, srs = summarize(path)
    meta = run_json(["pdal", "info", "--metadata", str(path)], step="test")["metadata"]
    assert s.count == r.points == classified.sidecar.ingest.output_count
    assert s.las_version == "1.4" and meta["compressed"] is False
    assert srs and "7850" in json.dumps(srs) and r.crs_in_file
    assert r.source == "classified" and r.classes.get(2, 0) == classified.sidecar.ground.ground_points
    ref, _ = summarize(classified.product_path("classified"))
    for k in ("minx", "miny", "maxx", "maxy", "minz", "maxz"):
        assert getattr(s.bounds, k) == pytest.approx(getattr(ref.bounds, k), abs=0.0015), k
    notes = classified.path(r.notes).read_text()
    assert "GDA2020 / MGA zone 50" in notes and "Ground" in notes and "LAS 1.4" in notes
    assert classified.sidecar.exports[-1].format == "las" and "points_las" in classified.sidecar.products


def test_e57_export_keeps_coordinates_and_says_crs_is_not_in_file(classified) -> None:  # type: ignore[no-untyped-def]
    r = export_points(classified, "e57")
    s, srs = summarize(classified.path(r.path))
    assert s.count == r.points and not srs.get("wkt") and not r.crs_in_file
    ref, _ = summarize(classified.product_path("classified"))
    for k in ("minx", "miny", "maxx", "maxy"):
        assert getattr(s.bounds, k) == pytest.approx(getattr(ref.bounds, k), abs=0.0015), k  # double precision
    assert "NO — E57 has no CRS field" in classified.path(r.notes).read_text()
    assert any("points.e57 carries no CRS" in w for w in classified.sidecar.warnings)


def test_thin_and_crop(classified, scene: synthetic.Scene, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    total = classified.sidecar.ingest.output_count
    thinned = export_points(classified, "las", thin=1.0)
    assert 0 < thinned.points < total / 5 and thinned.thin_m == 1.0  # ~11 pts/m² → about one per m³ voxel
    e0, n0 = scene.origin
    poly = tmp_path / "box.geojson"
    ring = [[e0 + 10, n0 + 10], [e0 + 30, n0 + 10], [e0 + 30, n0 + 30], [e0 + 10, n0 + 30], [e0 + 10, n0 + 10]]
    poly.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": "EPSG:7850"}},
                "features": [
                    {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
                ],
            }
        )
    )
    cropped = export_points(classified, "las", polygon=poly)
    assert cropped.crop and 0.05 * total < cropped.points < 0.2 * total  # 400 of 4800 m²
    with pytest.raises(SurveyError, match="--thin"):
        export_points(classified, "las", thin=0)
    with pytest.raises(SurveyError, match="las or e57"):
        export_points(classified, "ply")  # type: ignore[arg-type]
