from __future__ import annotations

from pathlib import Path

import pytest

from argus_survey import config
from argus_survey.models import SurveyError


def test_resolve_under_root_joins_relative(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert config.resolve_under_root(tmp_path, "a/site.laz") == (tmp_path / "a" / "site.laz").resolve()


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../x", "/etc/passwd", "", "a\x00b"])
def test_resolve_under_root_refuses_escape(tmp_path: Path, bad: str) -> None:
    with pytest.raises(SurveyError):
        config.resolve_under_root(tmp_path, bad)


def test_resolve_under_root_refuses_symlink_out(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(SurveyError):
        config.resolve_under_root(root, "link/secret")


def test_resolve_under_root_accepts_absolute_inside(tmp_path: Path) -> None:
    inside = tmp_path / "x.laz"
    assert config.resolve_under_root(tmp_path, str(inside)) == inside.resolve()


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("yes", True), ("ON", True), ("0", False), ("", False)])
def test_process_allowed(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv(config.ENV_ALLOW_PROCESS, value)
    assert config.process_allowed() is expected


def test_process_allowed_unset_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ENV_ALLOW_PROCESS, raising=False)
    assert config.process_allowed() is False
