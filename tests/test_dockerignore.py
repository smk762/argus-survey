"""The image derives its version from git (hatch-vcs), so its build context must hold every tracked file.

A tracked file that .dockerignore drops looks deleted to git inside the build; hatch-vcs then versions a dirty
tree, and the v0.1.0 image reported 0.1.1.dev0+g087758b45.d20261113 (stamped into every job.json it wrote).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _rules() -> list[tuple[bool, re.Pattern[str]]]:
    """(excludes?, regex) per .dockerignore line, in order: Docker's last matching rule wins."""
    rules = []
    for raw in (ROOT / ".dockerignore").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keep = line.startswith("!")
        pattern = line.lstrip("!").strip("/")
        regex = ""
        i = 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                regex, i = regex + "(?:.*/)?", i + 3
            elif pattern.startswith("**", i):
                regex, i = regex + ".*", i + 2
            elif pattern[i] == "*":
                regex, i = regex + "[^/]*", i + 1
            elif pattern[i] == "?":
                regex, i = regex + "[^/]", i + 1
            elif pattern[i] == "[":
                end = pattern.index("]", i)
                regex, i = regex + pattern[i : end + 1], end + 1
            else:
                regex, i = regex + re.escape(pattern[i]), i + 1
        # a pattern that names a directory drops everything under it
        rules.append((not keep, re.compile(regex + "(?:/.*)?")))
    return rules


def _excluded(path: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    excluded = False
    for excludes, rx in rules:
        if rx.fullmatch(path):
            excluded = excludes
    return excluded


def test_matcher_follows_docker_rules() -> None:
    rules = _rules()
    assert _excluded("data/in/site.laz", rules)  # local data never reaches the image
    assert _excluded("src/argus_survey/__pycache__/cli.cpython-314.pyc", rules)
    assert not _excluded("data/in/.gitkeep", rules)
    assert not _excluded("src/argus_survey/cli.py", rules)


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_build_context_keeps_every_tracked_file() -> None:
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    rules = _rules()
    dropped = [p for p in tracked.splitlines() if _excluded(p, rules)]
    assert not dropped, f".dockerignore drops tracked files, so the image's version would be dirty: {dropped}"
