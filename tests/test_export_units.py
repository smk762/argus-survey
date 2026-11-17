"""Pure-Python export behaviour (the PDAL run is stubbed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from argus_survey import export
from argus_survey.jobs import JobStore
from argus_survey.models import CrsRecord, CrsRef, SurveyError


def test_a_failed_pdal_run_leaves_no_partial_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = JobStore(tmp_path).open("x", create=True)
    job.sidecar.crs = CrsRecord(definition="EPSG:7850", name="MGA", horizontal=CrsRef(name="MGA", kind="ProjectedCRS"))
    job.path("points.copc.laz").write_bytes(b"copc")
    job.add_product("copc", "points.copc.laz", step="ingest")

    def boom(stages: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        Path(stages[-1]["filename"]).write_bytes(b"truncated")  # the writer got part-way
        raise SurveyError("pdal failed: No space left on device")

    monkeypatch.setattr(export, "pdal_pipeline", boom)
    with pytest.raises(SurveyError, match="No space"):
        export.export_points(job, "las")
    assert not job.path("points.las").exists()
    assert "points_las" not in job.sidecar.products and job.sidecar.exports == []
