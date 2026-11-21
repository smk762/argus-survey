"""Tile plans (pure Python): the half-open cores must cover every point the cloud can hold."""

from __future__ import annotations

from pathlib import Path

import pytest

from argus_survey import tiling
from argus_survey.models import Bounds3D


def _inside(tile: tiling.Tile, x: float, y: float) -> bool:
    x0, y0, x1, y1 = tile.core
    return x0 <= x < x1 and y0 <= y < y1


def test_rounded_bounds_keep_the_minimum_point() -> None:
    """Floreat f1 2022: PDAL recorded minx 382260.0003, but three points sit at X = 382260.00025.

    The cores started exactly at the recorded min, so ground lost them and refused the job
    ("tiles hold 11860659 points but the source has 11860662").
    """
    b = Bounds3D(minx=382260.0003, miny=6466330.406, minz=-0.738, maxx=382446.6873, maxy=6466764.875, maxz=16.086)
    for count in (11_860_662, 1_000):  # many tiles, and one
        p = tiling.plan(b, count, buffer=5.0, target_points=500_000)
        for x, y in [
            (382260.00025, 6466764.4065),  # the real stragglers: below the recorded minx
            (b.minx - 0.0005, b.miny - 0.0005),  # worst case of rounding to ~10 significant digits
            (b.maxx + 0.0005, b.maxy + 0.0005),
            (b.minx, b.maxy),
        ]:
            assert sum(_inside(t, x, y) for t in p.tiles) == 1, (count, x, y)


def test_cores_partition_the_padded_extent() -> None:
    b = Bounds3D(minx=0.0, miny=0.0, minz=0.0, maxx=300.0, maxy=200.0, maxz=1.0)
    p = tiling.plan(b, 6_000_000, buffer=10.0, target_points=1_000_000)
    assert len(p.tiles) > 4
    xs = sorted({t.core[0] for t in p.tiles} | {t.core[2] for t in p.tiles})
    ys = sorted({t.core[1] for t in p.tiles} | {t.core[3] for t in p.tiles})
    assert xs[0] == pytest.approx(-tiling.EDGE) and xs[-1] == pytest.approx(300.0 + tiling.EDGE)
    assert ys[0] == pytest.approx(-tiling.EDGE) and ys[-1] == pytest.approx(200.0 + tiling.EDGE)
    # every grid sample lands in exactly one core
    for i in range(0, 301, 7):
        for j in range(0, 201, 7):
            assert sum(_inside(t, float(i), float(j)) for t in p.tiles) == 1


def _fake_untwine(monkeypatch: pytest.MonkeyPatch, *, fail: bool) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        out, tmp = Path(argv[argv.index("-o") + 1]), Path(argv[argv.index("--temp_dir") + 1])
        tmp.mkdir()
        (tmp / "scratch.bin").write_bytes(b"x" * 10)
        out.write_bytes(b"half a cop" if fail else b"new copc")
        if fail:
            raise tiling.SurveyError("untwine died")

    monkeypatch.setattr(tiling, "run", fake_run)
    return calls


def test_untwine_failure_keeps_the_old_output_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_untwine(monkeypatch, fail=True)
    out, scratch = tmp_path / "points.copc.laz", tmp_path / ".untwine-tmp"
    out.write_bytes(b"old copc")
    with pytest.raises(tiling.SurveyError, match="untwine died"):
        tiling.untwine(tmp_path / "parts", out, temp_dir=scratch, step="t", sink=None)
    assert out.read_bytes() == b"old copc"  # neither deleted nor truncated
    assert sorted(p.name for p in tmp_path.iterdir()) == ["points.copc.laz"]  # no partial file, no scratch


def test_untwine_success_replaces_the_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_untwine(monkeypatch, fail=False)
    out, scratch = tmp_path / "points.copc.laz", tmp_path / ".untwine-tmp"
    out.write_bytes(b"old copc")
    tiling.untwine(tmp_path / "parts", out, temp_dir=scratch, step="t", sink=None)
    assert out.read_bytes() == b"new copc"
    assert calls[0][calls[0].index("-o") + 1].endswith(".copc.laz")  # untwine keys COPC output on the suffix
    assert sorted(p.name for p in tmp_path.iterdir()) == ["points.copc.laz"]


def test_parts_that_miss_points_are_refused_before_the_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tiles must add up to the source's count; a short sum is refused and untwine never runs."""
    from argus_survey.jobs import JobStore
    from argus_survey.models import Bounds3D, SurveyError

    job = JobStore(tmp_path).open("t", create=True)
    plan = tiling.plan(
        Bounds3D(minx=0, miny=0, minz=0, maxx=100, maxy=100, maxz=1), 1_000, buffer=5.0, target_points=300
    )
    assert len(plan.tiles) > 1

    def fake_pipeline(stages, **_kw):  # type: ignore[no-untyped-def]
        Path(stages[-1]["filename"]).write_bytes(b"")
        return {}

    merged: list[Path] = []
    monkeypatch.setattr(tiling, "pdal_pipeline", fake_pipeline)
    monkeypatch.setattr(tiling, "run_json", lambda *_a, **_kw: {"summary": {"num_points": 100}})
    monkeypatch.setattr(tiling, "untwine", lambda parts, out, **_kw: merged.append(out))
    expected = 100 * len(plan.tiles)

    tiling.run_tiles(
        job,
        tmp_path / "src.copc.laz",
        plan,
        lambda _t: [],
        step="ground",
        out=tmp_path / "o.laz",
        expected_count=expected,
    )
    assert merged == [tmp_path / "o.laz"]

    with pytest.raises(SurveyError, match=f"tiles hold {expected} points but the source has {expected + 3}"):
        tiling.run_tiles(
            job,
            tmp_path / "src.copc.laz",
            plan,
            lambda _t: [],
            step="ground",
            out=tmp_path / "o2.laz",
            expected_count=expected + 3,
        )
    assert merged == [tmp_path / "o.laz"]  # no merge after the refusal
    assert not job.path(".ground-parts").exists()


def test_a_failed_tile_stops_the_tiles_not_yet_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from argus_survey.jobs import JobStore
    from argus_survey.models import SurveyError

    job = JobStore(tmp_path).open("t", create=True)
    plan = tiling.plan(
        Bounds3D(minx=0, miny=0, minz=0, maxx=100, maxy=100, maxz=1), 10_000, buffer=5.0, target_points=300
    )
    assert len(plan.tiles) >= 9
    started: list[str] = []

    def failing_pipeline(stages, **_kw):  # type: ignore[no-untyped-def]
        started.append(stages[-1]["filename"])
        raise SurveyError("tile failed")

    monkeypatch.setattr(tiling, "pdal_pipeline", failing_pipeline)
    parts = tmp_path / "parts"
    with pytest.raises(SurveyError, match="tile failed"):
        tiling.run_tiles(
            job,
            tmp_path / "s.copc.laz",
            plan,
            lambda _t: [],
            step="ground",
            out=None,
            expected_count=0,
            workers=1,
            parts_dir=parts,
        )
    # the worker may pick up one more tile before the cancel lands, never the whole plan
    assert len(started) <= 2 < len(plan.tiles)
    assert not parts.exists()
