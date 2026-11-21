"""Pure-Python checks of the synthetic scenes (the fixtures' analytic truth)."""

from __future__ import annotations

from argus_survey import synthetic
from argus_survey.synthetic import TRUTH_GROUND, TRUTH_STOCKPILE, TRUTH_VEGETATION, Scene, Tree


def test_steps_keep_every_interval_of_an_exact_multiple() -> None:
    assert len(synthetic._steps(0.0, 0.3, 0.1)) == 4
    assert len(synthetic._steps(0.0, 1.0, 0.3)) == 4  # 3.33 intervals → 3
    assert synthetic._steps(1.0, 1.0, 0.5) == [1.0, 1.0]


def test_check_points_without_trees_are_open_only() -> None:
    pts = synthetic.check_points(Scene(trees=()), n_open=5)
    assert len(pts) == 5 and {p.description for p in pts} == {"open"}


def test_under_canopy_return_on_a_stockpile_is_labelled_stockpile() -> None:
    # one tree right over the cone's apex, nothing else
    scene = Scene(trees=(Tree(20.0, 30.0, 2.0, 6.0),), noise=0.0, noise_points=0, width=40.0, height=50.0)
    rows = synthetic.points(scene)
    e0, n0 = scene.origin
    near = [r for r in rows if abs(r[0] - e0 - 20.0) < 1.0 and abs(r[1] - n0 - 30.0) < 1.0]
    under = [r for r in near if r[4] == 2]
    crowns = [r for r in near if r[7] == TRUTH_VEGETATION]
    assert under and all(r[7] == TRUTH_STOCKPILE for r in under)
    assert not any(r[7] == TRUTH_GROUND for r in near)
    for r in crowns:  # a crown stands on the pile, above it
        x, y = r[0] - e0, r[1] - n0
        assert r[2] > scene.surface_z(x, y) + 3.0


def test_vegetated_check_points_keep_clear_of_the_pile_building_and_edges() -> None:
    """Trees beside the building, on the pile's flank and at the border: their check points still obey the exclusions."""
    scene = Scene(trees=(Tree(49.0, 40.0, 3.0, 8.0), Tree(29.5, 30.0, 3.0, 8.0), Tree(2.0, 2.0, 3.0, 8.0)))
    e0, n0 = scene.origin
    veg = [c for c in synthetic.check_points(scene, n_open=0, n_vegetated=60) if c.description == "vegetated"]
    assert veg
    for c in veg:
        x, y = c.x - e0, c.y - n0
        assert not scene.in_building(x, y) and not synthetic._near_building(scene, x, y), c
        assert not synthetic._near_cone(scene, x, y), c
        assert 3 <= x <= scene.width - 3 and 3 <= y <= scene.height - 3, c
    buried = Scene(trees=(Tree(55.0, 40.0, 1.0, 8.0),))  # wholly over the building: no usable ground, no point
    assert synthetic.check_points(buried, n_open=0, n_vegetated=5) == []
