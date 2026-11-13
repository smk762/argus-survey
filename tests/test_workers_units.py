"""Default tile parallelism (pure Python)."""

from __future__ import annotations

from argus_survey import tiling


def test_default_workers_fit_memory(monkeypatch) -> None:
    monkeypatch.delenv("SURVEY_WORKERS", raising=False)
    gib = 1 << 30
    assert tiling.default_workers(memory=32 * gib, cpus=16) == 4  # the measured host
    assert tiling.default_workers(memory=8 * gib, cpus=8) == 1  # a laptop: 4 tiles would need ~16 GiB
    assert tiling.default_workers(memory=10 * gib, cpus=8) == 2
    assert tiling.default_workers(memory=1 * gib, cpus=8) == 1  # never zero
    assert tiling.default_workers(memory=64 * gib, cpus=2) == 2
    monkeypatch.setenv("SURVEY_WORKERS", "6")
    assert tiling.default_workers(memory=8 * gib, cpus=2) == 6  # an explicit choice wins
