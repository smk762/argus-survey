"""Web views (pure Python): size capping and the cache's all-or-nothing writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from argus_survey import overlays
from argus_survey.jobs import JobStore


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((500, 300), []),  # small rasters are not upscaled
        ((2048, 2048), []),
        ((4000, 1000), ["-ts", "2048", "0"]),
        ((1000, 9000), ["-ts", "0", "2048"]),  # a tall corridor is capped on its height
    ],
)
def test_web_size_caps_the_longer_side(size: tuple[int, int], expected: list[str]) -> None:
    assert overlays.web_size(*size) == expected


def _job_with_contours(tmp_path: Path):  # type: ignore[no-untyped-def]
    job = JobStore(tmp_path).open("x", create=True)
    (job.dir / "contours.geojson").write_text('{"type": "FeatureCollection", "features": []}')
    job.add_product("contours_geojson", "contours.geojson", step="contours")
    return job


def test_failed_vector_view_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job_with_contours(tmp_path)

    def half_written(argv, **_kw):  # type: ignore[no-untyped-def]
        Path(argv[-2]).write_text('{"type": "Feature')
        raise overlays.SurveyError("ogr2ogr exited 1")

    monkeypatch.setattr(overlays, "run", half_written)
    with pytest.raises(overlays.SurveyError):
        overlays.vector_wgs84(job, "contours_geojson")
    assert list((job.dir / ".web").iterdir()) == []  # nothing a later request would serve

    def ok(argv, **_kw):  # type: ignore[no-untyped-def]
        assert Path(argv[-2]).name.startswith(".")  # never the cache path itself
        Path(argv[-2]).write_text('{"type": "FeatureCollection", "features": []}')

    monkeypatch.setattr(overlays, "run", ok)
    out = overlays.vector_wgs84(job, "contours_geojson")
    assert out.is_file() and [p.name for p in (job.dir / ".web").iterdir()] == [out.name]
