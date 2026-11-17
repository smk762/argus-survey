"""CLI plumbing that needs no tools: dependencies and how errors are printed."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from rich.console import Console

from argus_survey import cli
from argus_survey.tools import ToolError

ROOT = Path(__file__).resolve().parents[1]


def test_cli_dependencies_are_in_the_base_install() -> None:
    """The `argus-survey` script is installed for everyone, so what cli.py imports must not hide in an extra."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["scripts"]["argus-survey"] == "argus_survey.cli:app"
    # PEP 508 name, normalised per PEP 503 ("rich~=13", "Typer<1,>=0.9", "structlog; python_version>'3'" all parse)
    base = {
        re.sub(r"[-_.]+", "-", re.match(r"\s*([A-Za-z0-9._-]+)", dep).group(1)).lower()
        for dep in project["dependencies"]
    }
    assert {"typer", "rich", "pydantic", "structlog"} <= base


def test_errors_print_brackets_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rich markup must not swallow `[server]` in an install hint or PDAL's `[readers.las]` in stderr."""
    console = Console(record=True, width=200)
    monkeypatch.setattr(cli, "err", console)
    cli.fail(cli.SurveyError("the server needs: pip install argus-survey[server]"))
    exc = ToolError(["pdal", "pipeline"], 1, "PDAL: [readers.las] bad header [/oops]")
    cli.fail(exc)
    text = console.export_text()
    assert "argus-survey[server]" in text
    assert "[readers.las] bad header [/oops]" in text
