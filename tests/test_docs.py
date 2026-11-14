"""Published docs: the generated CLI reference is current, and the headings other repos link to still exist.

argus-sightline imports README.md and docs/*.md and deep-links to their headings (src/lib/surveyDocs.ts), so a
renamed heading breaks its links. Rename one only together with a Sightline change.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# file -> GitHub heading anchors linked from outside this repo
LINKED_ANCHORS = {
    "README.md": ["wa-datums-in-one-table", "commands", "how-the-numbers-are-made", "documentation"],
    "docs/runbook.md": ["reading-refusals", "stockpiles"],
    "docs/data-sources.md": ["synthetic-data", "demo-kit"],
    "docs/datums.md": ["no-silent-datums-rule-1", "what-is-refused", "what-counts-as-assumed", "why-red"],
    "docs/steps.md": [
        "ingest",
        "qa",
        "ground",
        "surfaces",
        "contours",
        "volume",
        "cutfill",
        "change",
        "accuracy",
        "profile",
        "export",
        "report",
        "run",
    ],
    "docs/methods.md": ["vertical-accuracy-nva-and-vva", "volume-bases"],
    "docs/provenance.md": ["what-jobjson-records"],
    "docs/api.md": ["read-only-by-default", "routes", "request-bodies", "server-sent-events"],
}


def _anchor(heading: str) -> str:
    """GitHub's slug: lower-case, drop punctuation except - and _, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    out, fenced = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            out.add(_anchor(line.lstrip("#")))
    return out


@pytest.mark.parametrize("name", sorted(LINKED_ANCHORS))
def test_linked_headings_exist(name: str) -> None:
    missing = set(LINKED_ANCHORS[name]) - _anchors(ROOT / name)
    assert not missing, f"{name} lost heading(s) other repos link to: {sorted(missing)}"


def test_cli_reference_is_current(tmp_path: Path) -> None:
    out = tmp_path / "cli.md"
    subprocess.run(
        [sys.executable, "-m", "typer", "argus_survey.cli", "utils", "docs", "--name", "argus-survey", "--output", out],
        check=True,
        capture_output=True,
    )
    committed = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert committed == out.read_text(encoding="utf-8"), "docs/cli.md is stale: run `make docs` and commit it"
