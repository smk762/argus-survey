"""Pure-Python parts of the grid-to-ground scale factor."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from argus_survey.models import SurveyError
from argus_survey.scale import ellipsoid_of, gaussian_radius, parse_proj_verbose

DATA = Path(__file__).parent / "data"
A, RF = 6378137.0, 298.257222101  # GRS80


def test_gaussian_radius_at_equator_and_pole() -> None:
    f = 1 / RF
    e2 = f * (2 - f)
    assert gaussian_radius(A, RF, 0.0) == pytest.approx(A * math.sqrt(1 - e2), abs=1e-6)  # √(M·N), M = a(1−e²), N = a
    assert gaussian_radius(A, RF, 90.0) == pytest.approx(A / math.sqrt(1 - e2), abs=1e-6)  # M = N = a/√(1−e²)
    assert 6_368_000 < gaussian_radius(A, RF, -31.95) < 6_369_500


def test_parse_proj_verbose_two_points() -> None:
    rows = parse_proj_verbose((DATA / "proj_verbose_utm50.txt").read_text())
    assert len(rows) == 2
    h, k, conv = rows[0]
    assert h == k == pytest.approx(0.99975079)  # conformal: meridian scale = parallel scale
    assert conv == pytest.approx(0.6192023)
    with pytest.raises(SurveyError):
        parse_proj_verbose("nothing here")


def test_ellipsoid_from_projjson_datum_or_ensemble() -> None:
    ell = {"semi_major_axis": A, "inverse_flattening": RF}
    assert ellipsoid_of({"base_crs": {"datum": {"ellipsoid": ell}}}) == (A, RF)
    assert ellipsoid_of({"base_crs": {"datum_ensemble": {"ellipsoid": ell}}}) == (A, RF)
    with pytest.raises(SurveyError):
        ellipsoid_of({"base_crs": {"name": "x", "datum": {}}})


def test_horizontal_projjson_unwraps_a_bound_horizontal_component(monkeypatch: pytest.MonkeyPatch) -> None:
    from argus_survey import scale
    from argus_survey.models import CrsRecord

    projected = {"type": "ProjectedCRS", "name": "GDA94 / MGA zone 50"}
    compound = {
        "type": "CompoundCRS",
        "components": [{"type": "BoundCRS", "source_crs": projected}, {"type": "VerticalCRS"}],
    }
    monkeypatch.setattr(scale, "run_json", lambda *a, **k: compound)
    crs = CrsRecord.model_construct(definition="WKT", name="MGA94 + AHD")
    assert scale.horizontal_projjson(crs, sink=None, step="t") is projected


def test_ellipsoid_defined_by_semi_minor_axis() -> None:
    """Clarke 1866 and Airy 1830 are defined by a and b in EPSG; PROJJSON then has no inverse_flattening."""
    a, b = 6378206.4, 6356583.8
    got = ellipsoid_of({"base_crs": {"datum": {"ellipsoid": {"semi_major_axis": a, "semi_minor_axis": b}}}})
    assert got == (a, pytest.approx(294.978698214))
