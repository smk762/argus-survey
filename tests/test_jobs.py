from __future__ import annotations

from pathlib import Path

import pytest

from argus_survey.jobs import JobStore, is_safe_id, open_job
from argus_survey.models import CrsRecord, CrsRef, JobSidecar, SurveyError


@pytest.mark.parametrize("good", ["site1", "a.b_c-d", "2026-10-31"])
def test_safe_ids(good: str) -> None:
    assert is_safe_id(good)


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b", "-x", ".hidden", "a b", "a\x00"])
def test_unsafe_ids(bad: str) -> None:
    assert not is_safe_id(bad)


def test_store_create_open_list(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    with pytest.raises(SurveyError):
        store.open("site1")
    store.open("site1", create=True)
    assert (tmp_path / "site1" / "job.json").is_file()
    assert store.open("site1").id == "site1"
    assert [d.job_id for d in store.list()] == ["site1"]


def test_sidecar_roundtrip(tmp_path: Path) -> None:
    job = JobStore(tmp_path).open("x", create=True)
    job.sidecar.crs = CrsRecord(definition="EPSG:7850", name="MGA", horizontal=CrsRef(name="MGA", kind="ProjectedCRS"))
    job.save()
    again = JobSidecar.model_validate_json((tmp_path / "x" / "job.json").read_text())
    assert again.crs == job.sidecar.crs and again.schema_version == 1


def test_product_hashing_and_missing(tmp_path: Path) -> None:
    job = JobStore(tmp_path).open("x", create=True)
    (job.dir / "a.txt").write_text("hello")
    p = job.add_product("thing", "a.txt", step="t")
    assert p.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" and p.size == 5
    assert job.product_path("thing") == job.dir / "a.txt"
    with pytest.raises(SurveyError, match="no 'other'"):
        job.product_path("other")
    with pytest.raises(SurveyError):
        job.add_product("x", "missing.tif", step="t")


def test_height_steps_refuse_without_vertical(tmp_path: Path) -> None:
    job = JobStore(tmp_path).open("x", create=True)
    with pytest.raises(SurveyError, match="ingest"):
        job.require_crs("volume", height=True)
    job.sidecar.crs = CrsRecord(definition="EPSG:7850", name="MGA", horizontal=CrsRef(name="MGA", kind="ProjectedCRS"))
    job.require_crs("qa", height=False)
    with pytest.raises(SurveyError, match="assume-vertical"):
        job.require_crs("volume", height=True)


def test_step_records_timing(tmp_path: Path) -> None:
    job = JobStore(tmp_path).open("x", create=True)
    with job.step("qa"):
        pass
    assert "qa" in job.sidecar.timings and job.sidecar.steps_done == ["qa"]


def test_open_job_job_or_out(tmp_path: Path) -> None:
    with pytest.raises(SurveyError, match="exactly one"):
        open_job(None, None, jobs_root=tmp_path)
    j = open_job(None, tmp_path / "out1", jobs_root=tmp_path / "unused", create=True)
    assert j.id == "out1" and j.dir == (tmp_path / "out1").resolve()
