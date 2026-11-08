"""QA and ground classification on the synthetic scene."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import class_counts_by_truth

from argus_survey import synthetic
from argus_survey.ground import ground
from argus_survey.qa import qa

pytestmark = pytest.mark.tools


def qa_job_scene_noise() -> int:
    return synthetic.Scene().noise_points


@pytest.fixture(scope="module")
def qa_job(ingested):  # type: ignore[no-untyped-def]
    qa(ingested, density_res=1.0)
    return ingested


def test_qa_figures(qa_job) -> None:  # type: ignore[no-untyped-def]
    r = qa_job.sidecar.qa
    assert r.points == qa_job.sidecar.ingest.output_count
    # 0.3 m jittered lattice ≈ 11.1 pts/m², more under trees (two returns) and along walls
    assert 9.0 < r.density.p50 < 14.0
    assert r.density.p5 <= r.density.p50 <= r.density.p95
    assert [c.code for c in r.classes] == [1] and r.classes[0].name == "Unclassified"
    assert {v.value for v in r.returns} == {1, 2}
    assert 0.9 * 80 * 60 < r.coverage_area_m2 < 1.1 * 80 * 60
    # the injected outliers are found, and few real points are flagged
    assert r.outliers.count >= 0.8 * qa_job_scene_noise()
    assert r.outliers.count < 0.01 * r.points
    assert r.gaps.percent_of_coverage < 5


def test_qa_products_and_commands(qa_job) -> None:  # type: ignore[no-untyped-def]
    for kind in ("qa", "density", "boundary", "coverage", "gaps"):
        assert qa_job.product_path(kind).is_file(), kind
    boundary = json.loads(qa_job.product_path("boundary").read_text())
    assert boundary["features"][0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert any(c.step == "qa" and c.pipeline for c in qa_job.sidecar.commands)
    assert json.loads(qa_job.product_path("qa").read_text())["points"] == qa_job.sidecar.qa.points


def test_ground_separates_objects(qa_job) -> None:  # type: ignore[no-untyped-def]
    r = ground(qa_job)
    copc = qa_job.product_path("classified")
    assert r.ground_points > 0 and r.points == qa_job.sidecar.ingest.output_count
    # the bottom of a wall is within SMRF's 0.5 m threshold of the terrain — inherently "ground";
    # everything clearly above it must not be
    for truth in (synthetic.TRUTH_BUILDING, synthetic.TRUTH_VEGETATION):
        counts = class_counts_by_truth(copc, truth, min_height=0.6)
        n = sum(counts.values())
        assert n > 0
        assert counts.get("2", 0) / n < 0.05, (truth, counts)
    bare = class_counts_by_truth(copc, synthetic.TRUTH_GROUND)
    assert bare.get("2", 0) / sum(bare.values()) > 0.9, bare
    # every point kept
    total = sum(sum(class_counts_by_truth(copc, t).values()) for t in range(5))
    assert total == r.points
    assert r.ground_spacing < 0.5
    noise = class_counts_by_truth(copc, synthetic.TRUTH_NOISE)
    assert noise.get("7", 0) >= 0.8 * sum(noise.values()), noise  # outliers marked class 7, not dropped


def test_ground_unknown_param(ingested) -> None:  # type: ignore[no-untyped-def]
    from argus_survey.models import SurveyError

    with pytest.raises(SurveyError, match="unknown smrf"):
        ground(ingested, params={"rigidness": 3})


def test_tiled_ground_partition_is_exact(las_mga_ahd: Path, jobs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force many small tiles: the parts must still add up to the source count (no duplicates at edges)."""
    from argus_survey import tiling
    from argus_survey.ingest import ingest
    from argus_survey.jobs import JobStore

    monkeypatch.setattr(tiling, "TARGET_POINTS", 8000)
    monkeypatch.setattr(tiling, "MIN_TILE", 10.0)
    job = JobStore(jobs_root).open("tiled", create=True)
    ingest(job, [las_mga_ahd])
    r = ground(job, params={"window": 6.0})
    assert r.tiles > 4
    bld = class_counts_by_truth(job.product_path("classified"), synthetic.TRUTH_BUILDING, min_height=0.6)
    assert bld.get("2", 0) / sum(bld.values()) < 0.05


def test_stockpile_preset_keeps_a_steep_pile(synth_dir: Path, jobs_root: Path) -> None:
    """A 39° cone (angle of repose): default SMRF cuts its crest off, the stockpile preset keeps it."""
    from argus_survey.ingest import ingest
    from argus_survey.jobs import JobStore

    steep = synthetic.Scene(cone_height=6.5)  # radius 8 m → slope 0.81
    las = synthetic.write_scene(steep, synth_dir / "steep.laz", srs="EPSG:7850+5711")
    job = JobStore(jobs_root).open("steep", create=True)
    ingest(job, [las])

    def ground_fraction(truth: int, min_height: float | None = None) -> float:
        counts = class_counts_by_truth(job.product_path("classified"), truth, min_height=min_height)
        return counts.get("2", 0) / sum(counts.values())

    ground(job)
    assert ground_fraction(synthetic.TRUTH_STOCKPILE) < 0.85  # the default loses the crest

    r = ground(job, preset="stockpile")
    assert r.preset == "stockpile" and r.params["slope"] == 1.0
    assert ground_fraction(synthetic.TRUTH_STOCKPILE) > 0.99
    # walls and canopy are still not ground
    assert ground_fraction(synthetic.TRUTH_BUILDING, 0.6) < 0.05
    assert ground_fraction(synthetic.TRUTH_VEGETATION, 0.6) < 0.05
    # an explicit parameter still wins over the preset
    assert ground(job, preset="stockpile", params={"slope": 0.8}).params["slope"] == 0.8


def test_preset_refusals(ingested) -> None:  # type: ignore[no-untyped-def]
    from argus_survey.models import SurveyError

    with pytest.raises(SurveyError, match="unknown ground preset"):
        ground(ingested, preset="quarry")
    with pytest.raises(SurveyError, match="smrf only"):
        ground(ingested, method="csf", preset="stockpile")
