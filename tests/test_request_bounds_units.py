"""API request bodies refuse resolutions/intervals/methods the workers can't (or shouldn't) run."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from argus_survey.models import ContoursBody, QaBody, RunBody, SurfacesBody


def RunBody_(**kw: object) -> RunBody:  # noqa: N802 - stands in for the model class
    return RunBody(inputs=["site.laz"], **kw)


def _rejects(body: type | object, **kw: object) -> None:
    """*body*(**kw) fails validation on exactly the field given (not on some other missing field)."""
    with pytest.raises(ValidationError) as info:
        body(**kw)  # type: ignore[operator]
    assert {err["loc"][0] for err in info.value.errors()} == set(kw)


@pytest.mark.parametrize("res", [0, 1e-6, 0.009, math.nan, math.inf, -1])
def test_tiny_or_bad_resolutions_are_rejected(res: float) -> None:
    for body in (SurfacesBody, RunBody_):
        _rejects(body, res=res)
    _rejects(QaBody, density_res=res)


@pytest.mark.parametrize("interval", [0, 1e-9, math.nan, math.inf])
def test_tiny_or_bad_intervals_are_rejected(interval: float) -> None:
    for body in (ContoursBody, RunBody_):
        _rejects(body, interval=interval)


def test_survey_scale_values_pass() -> None:
    assert SurfacesBody(res=0.01).res == 0.01 and RunBody_(res=0.05, interval=0.01).interval == 0.01
    assert ContoursBody(interval=0.1, index=None).index is None and ContoursBody(index=0).index == 0
    assert ContoursBody(index=10).index == 10


@pytest.mark.parametrize("index", [-1, 1])
def test_meaningless_index_is_rejected(index: int) -> None:
    _rejects(ContoursBody, index=index)


def test_surface_methods_are_validated() -> None:
    assert SurfacesBody(dsm_method="max", chm_method="radius").chm_method == "radius"
    _rejects(SurfacesBody, dsm_method="mean")
    _rejects(SurfacesBody, chm_method="idw")


@pytest.mark.parametrize("interval", [math.nan, math.inf, 0.0, 1e-6])
def test_contours_refuse_a_nonfinite_or_tiny_interval(interval: float) -> None:
    """The CLI path (and any direct caller) gets a SurveyError before gdal_contour sees `-i nan`."""
    from types import SimpleNamespace

    from argus_survey.contours import contours
    from argus_survey.models import SurveyError

    job = SimpleNamespace(require_crs=lambda *_a, **_k: None)
    with pytest.raises(SurveyError, match="interval"):
        contours(job, interval=interval)  # type: ignore[arg-type]
