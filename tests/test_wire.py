"""The wire contract UIs generate their types from: committed schema, request bodies, SSE events."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from argus_survey import models

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "survey-wire.schema.json"


def test_committed_schema_is_current() -> None:
    pytest.importorskip("argus_cortex")
    assert SCHEMA.read_text(encoding="utf-8") == models.wire_schema_text(), (
        "schema/survey-wire.schema.json is stale: run `argus-survey schema` and commit it"
    )


def test_schema_names_every_wire_model() -> None:
    pytest.importorskip("argus_cortex")
    import json

    defs = json.loads(models.wire_schema_text())["$defs"]
    for model in models.WIRE_MODELS:
        assert model.__name__ in defs


def test_volume_body_takes_a_file_or_a_drawn_polygon() -> None:
    ring = [[115.9, -31.9], [115.901, -31.9], [115.901, -31.901], [115.9, -31.9]]
    body = models.VolumeBody(polygon_geojson={"type": "Polygon", "coordinates": [ring]}, name="pile A")
    assert body.polygon is None and body.name == "pile A"
    assert models.VolumeBody(polygon="pile.geojson").polygon_geojson is None
    with pytest.raises(ValidationError):
        models.VolumeBody(polygon="p.geojson", bogus=1)  # type: ignore[call-arg]


def test_event_payloads_round_trip() -> None:
    assert models.StepEvent(step="qa", state="start").model_dump(exclude_none=True) == {"step": "qa", "state": "start"}
    cmd = models.CommandEvent(index=3, step="ingest", tool="pdal", seconds=1.25, returncode=0)
    assert models.CommandEvent.model_validate(cmd.model_dump()) == cmd
    assert models.ErrorEvent(message="no").model_dump(exclude_none=True) == {"message": "no"}
