import numpy as np

from app.scheduler import update_heat, willingness_select


def test_update_heat_decays_then_adds():
    heat = [0.0, 1.0, 2.0]
    new_heat = update_heat(heat, 1, gamma=0.7)
    assert new_heat[0] == 0.0
    assert abs(new_heat[1] - (0.7 * 1.0 + 1.0)) < 1e-9
    assert abs(new_heat[2] - (0.7 * 2.0)) < 1e-9


def test_willingness_select_never_returns_forbidden():
    scores = [50.0, 50.0, 50.0]
    heat = [0.0, 0.0, 0.0]
    import random

    rng = random.Random(1)
    for _ in range(200):
        idx = willingness_select(scores, heat, forbid_consecutive=True, last_speaker=0, rng=rng)
        assert idx != 0


def test_willingness_select_greedy_when_tiny_tau():
    scores = [10.0, 90.0, 20.0]
    heat = [0.0, 0.0, 0.0]
    import random

    rng = random.Random(2)
    idx = willingness_select(scores, heat, tau=1e-3, forbid_consecutive=False, rng=rng)
    assert idx == 1

