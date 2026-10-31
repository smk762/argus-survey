"""The report: self-contained, complete, and loud about assumptions."""

from __future__ import annotations

import re

import pytest

from argus_survey import synthetic
from argus_survey.accuracy import accuracy
from argus_survey.ingest import ingest
from argus_survey.jobs import JobStore
from argus_survey.pipeline import run_all
from argus_survey.report import report

pytestmark = pytest.mark.tools


@pytest.fixture(scope="module")
def full_job(las_mga_ahd, tmp_path_factory, scene):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("rep")
    job = JobStore(root).open("full", create=True)
    run_all(job, [las_mga_ahd], res=0.5, interval=0.5, echo=lambda _m: None)
    checks = synthetic.write_check_points(synthetic.check_points(scene, z_noise=0.03), root / "checks.csv")
    accuracy(job, checks, checks_crs="EPSG:7850+5711")
    report(job)
    return job


def test_report_is_self_contained(full_job) -> None:  # type: ignore[no-untyped-def]
    doc = full_job.product_path("report").read_text()
    assert doc.startswith("<!doctype html>") and "<title>Survey report full</title>" in doc
    assert "<script" not in doc.lower()
    assert not re.search(r'(src|href)="https?://', doc), "no external resources"
    assert doc.count('src="data:image/png;base64,') >= 2  # density + hillshade


def test_report_covers_every_step_and_product(full_job) -> None:  # type: ignore[no-untyped-def]
    doc = full_job.product_path("report").read_text()
    for heading in (
        "Coordinate reference",
        "Quality assessment",
        "Ground classification",
        "Surfaces",
        "Vertical accuracy",
        "Products",
        "Command log",
    ):
        assert f">{heading}<" in doc, heading
    assert "GDA2020 / MGA zone 50 + AHD height" in doc
    for kind, p in full_job.sidecar.products.items():
        if kind != "report":
            assert p.sha256 in doc, kind
    n = len(full_job.sidecar.commands)
    assert f'id="cmd-{n}"' in doc or f'id="cmd-{n - 1}"' in doc  # the log is complete up to the report itself
    links = set(re.findall(r'href="#cmd-(\d+)"', doc))
    anchors = set(re.findall(r'id="cmd-(\d+)"', doc))
    assert links and links <= anchors, "every figure links to a command that is in the log"
    # ASPRS ed. 2 dropped the 95 % confidence figure: no such claim anywhere (numbers like 41.96 are fine)
    assert not re.search(r"95\s?%\s*confidence|1\.96\s*[×x*·]\s*RMSE|NVA\s*\(?95", doc, re.IGNORECASE)


def test_assumed_vertical_gets_a_red_banner(las_mga_only, tmp_path) -> None:  # type: ignore[no-untyped-def]
    job = JobStore(tmp_path).open("assumed", create=True)
    ingest(job, [las_mga_only], assume_vertical="EPSG:5711")
    doc = report(job).read_text()
    assert 'class="banner bad">Vertical datum ASSUMED' in doc


def test_no_vertical_banner(las_mga_only, tmp_path) -> None:  # type: ignore[no-untyped-def]
    job = JobStore(tmp_path).open("nov", create=True)
    ingest(job, [las_mga_only])
    assert "No vertical datum" in report(job).read_text()
