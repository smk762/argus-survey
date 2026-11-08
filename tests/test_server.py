"""The HTTP API: posture, path fence, SSE processing, range-served products, web views."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus_survey import synthetic
from argus_survey.server import create_app


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    data, jobs = tmp_path / "in", tmp_path / "jobs"
    data.mkdir()
    jobs.mkdir()
    (data / "site").mkdir()
    (data / "site" / "a.laz").write_bytes(b"x")
    (data / ".hidden.laz").write_bytes(b"x")
    (data / "pile.geojson").write_text("{}")
    (tmp_path / "secret.laz").write_bytes(b"x")
    return data, jobs


def client(roots: tuple[Path, Path], allow: bool) -> TestClient:
    data, jobs = roots
    return TestClient(create_app(data_dir=data, jobs_dir=jobs, allow_process=allow))


def sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_health_reflects_posture(roots) -> None:  # type: ignore[no-untyped-def]
    assert client(roots, False).get("/health").json()["read_only"] is True
    assert client(roots, True).get("/health").json()["read_only"] is False


def test_process_routes_403_when_read_only(roots) -> None:  # type: ignore[no-untyped-def]
    r = client(roots, False).post("/jobs/site1/ingest", json={"inputs": ["site/a.laz"]})
    assert r.status_code == 403 and "SURVEY_ALLOW_PROCESS" in r.json()["detail"]


def test_inputs_lists_names_not_paths(roots) -> None:  # type: ignore[no-untyped-def]
    body = client(roots, False).get("/inputs").json()
    assert body["points"] == ["site/a.laz"] and body["vectors"] == ["pile.geojson"]


@pytest.mark.parametrize("bad", ["../secret.laz", "/etc/passwd", "site/../../secret.laz"])
def test_path_fence(roots, bad: str) -> None:  # type: ignore[no-untyped-def]
    events = sse(client(roots, True).post("/jobs/x/ingest", json={"inputs": [bad]}).text)
    assert events[-1][0] == "error" and "escapes" in events[-1][1]["message"]


def test_ids_and_steps_validated(roots) -> None:  # type: ignore[no-untyped-def]
    c = client(roots, True)
    assert c.get("/jobs/..%2Fx").status_code in {400, 404}
    assert c.post("/jobs/.x/qa", json={}).status_code == 400
    assert c.post("/jobs/x/bogus", json={}).status_code == 404
    assert c.post("/jobs/x/qa", json={"nope": 1}).status_code == 422
    assert c.get("/jobs/missing").status_code == 404


def test_health_matches_its_model(roots) -> None:  # type: ignore[no-untyped-def]
    from argus_survey.models import HealthInfo

    body = client(roots, False).get("/health").json()
    assert HealthInfo.model_validate(body).service == "argus-survey"


def test_volume_refuses_a_file_and_a_drawn_polygon_together(roots) -> None:  # type: ignore[no-untyped-def]
    from argus_survey.jobs import JobStore

    JobStore(roots[1]).open("j1", create=True)
    ring = [[115.9, -31.9], [115.901, -31.9], [115.901, -31.901], [115.9, -31.9]]
    body = {"polygon": "pile.geojson", "polygon_geojson": {"type": "Polygon", "coordinates": [ring]}}
    events = sse(client(roots, True).post("/jobs/j1/volume", json=body).text)
    assert events[-1][0] == "error" and "not both" in events[-1][1]["message"]
    events = sse(client(roots, True).post("/jobs/j1/volume", json={}).text)
    assert events[-1][0] == "error" and "polygon_geojson" in events[-1][1]["message"]
    drawn = {"type": "Polygon", "coordinates": [ring]}
    events = sse(
        client(roots, True).post("/jobs/j1/volume", json={"polygon_geojson": drawn, "polygon_srs": "EPSG:7850"}).text
    )
    assert events[-1][0] == "error" and "always WGS 84" in events[-1][1]["message"]
    events = sse(client(roots, True).post("/jobs/j1/volume", json={"polygon": "pile.geojson", "name": "A"}).text)
    assert events[-1][0] == "error" and "drawn polygon" in events[-1][1]["message"]


def test_viewer_is_served(roots) -> None:  # type: ignore[no-untyped-def]
    r = client(roots, False).get("/viewer")
    assert r.status_code == 200 and "<html" in r.text.lower()


@pytest.mark.tools
def test_run_over_sse_then_read_products(tmp_path: Path) -> None:
    data, jobs = tmp_path / "in", tmp_path / "jobs"
    data.mkdir()
    synthetic.write_scene(synthetic.Scene(), data / "site.laz")
    c = TestClient(create_app(data_dir=data, jobs_dir=jobs, allow_process=True))
    events = sse(c.post("/jobs/demo/run", json={"inputs": ["site.laz"], "res": 1.0, "interval": 1.0}).text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "done", events[-1]
    assert "command" in kinds and ("step", {"step": "ingest", "state": "start"}) in events
    done = events[-1][1]
    assert "copc" in done["products"] and "report" in done["products"]

    assert c.get("/jobs").json()["jobs"][0]["job_id"] == "demo"
    assert c.get("/jobs/demo").json()["crs"]["definition"] == "EPSG:7850+5711"
    full = c.get("/jobs/demo/copc")
    part = c.get("/jobs/demo/copc", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == full.content[:100]
    assert part.headers["content-range"].startswith("bytes 0-99/")
    assert c.get("/jobs/demo/report").headers["content-type"].startswith("text/html")
    assert c.get("/jobs/demo/nope").status_code == 404

    view = c.get("/jobs/demo/web/hillshade").json()
    assert len(view["coordinates"]) == 4 and 115 < view["coordinates"][0][0] < 116
    assert c.get(view["url"]).headers["content-type"] == "image/png"
    contours = c.get("/jobs/demo/web/contours_geojson").json()
    lon, lat = contours["features"][0]["geometry"]["coordinates"][0]
    assert 115 < lon < 116 and -33 < lat < -31
    n_commands = len(c.get("/jobs/demo").json()["commands"])

    line = {"type": "LineString", "coordinates": [[lon, lat], [lon + 0.0003, lat + 0.0002]]}
    prof = sse(c.post("/jobs/demo/profile", json={"line": line, "step": 1.0}).text)
    assert prof[-1][0] == "done", prof[-1]
    assert c.get("/jobs/demo/profile_svg").status_code == 200
    # web views never touch the provenance log; the profile step does
    assert len(c.get("/jobs/demo").json()["commands"]) > n_commands


@pytest.mark.tools
def test_demo_kit_known_answers_over_the_api(tmp_path: Path) -> None:
    """The UI's demo path: the kit's stockpile (file and drawn) and pad cut/fill come back at their known answers."""
    from argus_survey.tools import run

    data, jobs = tmp_path / "in", tmp_path / "jobs"
    truth = synthetic.write_kit(data)
    c = TestClient(create_app(data_dir=data, jobs_dir=jobs, allow_process=True))
    events = sse(c.post("/jobs/kit/run", json={"inputs": ["site-e1.laz"], "res": 0.5, "interval": 0.5}).text)
    assert events[-1][0] == "done", events[-1]

    vol = sse(c.post("/jobs/kit/volume", json={"polygon": "stockpile.geojson", "surface": "dsm"}).text)
    assert vol[-1][0] == "done", vol[-1]
    pile = vol[-1][1]["result"]["zones"][0]
    assert pile["name"] == "stockpile"
    assert pile["above_m3"] == pytest.approx(truth["stockpile_volume_m3"], rel=0.03)

    lonlat = tmp_path / "stockpile-4326.geojson"
    run(["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", str(lonlat), str(data / "stockpile.geojson")], step="t")
    geom = json.loads(lonlat.read_text())["features"][0]["geometry"]
    drawn = sse(c.post("/jobs/kit/volume", json={"polygon_geojson": geom, "name": "drawn pile", "surface": "dsm"}).text)
    assert drawn[-1][0] == "done", drawn[-1]
    zone = drawn[-1][1]["result"]["zones"][0]
    assert zone["name"] == "drawn pile" and zone["above_m3"] == pytest.approx(pile["above_m3"], rel=0.01)

    cf = sse(c.post("/jobs/kit/cutfill", json={"design": "design.tif", "polygon": "pad.geojson"}).text)
    assert cf[-1][0] == "done", cf[-1]
    pad = cf[-1][1]["result"]["zones"][0]
    assert pad["below_m3"] - pad["above_m3"] == pytest.approx(truth["pad_net_cut_m3"], rel=0.03)
