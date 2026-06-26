"""WindMouse path generator — geometry, exact landing, monotonic bursty timing."""

from __future__ import annotations

import math
import random

from pikvm_agent.pikvm.windmouse import WindMouseOptions, wind_mouse_path


def _rng(seed: int):
    r = random.Random(seed)
    return r.random


def test_zero_distance_is_single_sample() -> None:
    pts = wind_mouse_path((100.0, 100.0), (100.0, 100.0), rng=_rng(1))
    assert pts == [(100.0, 100.0, 0.0)]


def test_lands_exactly_on_target() -> None:
    # The final sample must be the exact target (+ the deterministic end scatter),
    # but with humanize off there is no scatter so it is dead-on.
    end = (640.0, 480.0)
    pts = wind_mouse_path((0.0, 0.0), end,
                          WindMouseOptions(end_scatter=0.0, tremor=0.0), rng=_rng(2))
    assert math.isclose(pts[-1][0], end[0], abs_tol=1e-6)
    assert math.isclose(pts[-1][1], end[1], abs_tol=1e-6)


def test_starts_near_start_and_is_a_real_path() -> None:
    pts = wind_mouse_path((10.0, 10.0), (500.0, 300.0), rng=_rng(3))
    assert len(pts) > 5  # a curve, not a teleport
    assert math.hypot(pts[0][0] - 10.0, pts[0][1] - 10.0) < 5.0  # begins at the start


def test_timestamps_are_monotonic_and_bounded() -> None:
    pts = wind_mouse_path((0.0, 0.0), (800.0, 600.0), rng=_rng(4))
    ts = [t for _, _, t in pts]
    assert ts[0] == 0.0
    assert all(b >= a for a, b in zip(ts, ts[1:]))  # never goes backwards
    # Duration follows Fitts's law (~hundreds of ms for this distance), not seconds.
    assert 50.0 < ts[-1] < 5000.0


def test_faster_speed_is_shorter() -> None:
    slow = wind_mouse_path((0.0, 0.0), (700.0, 0.0),
                           WindMouseOptions(speed=1.0), rng=_rng(5))
    fast = wind_mouse_path((0.0, 0.0), (700.0, 0.0),
                           WindMouseOptions(speed=4.0), rng=_rng(5))
    assert fast[-1][2] < slow[-1][2]


def test_humanize_off_stays_on_the_straight_line() -> None:
    # tremor + scatter at 0: points should hug the straight x-axis path closely.
    pts = wind_mouse_path((0.0, 0.0), (600.0, 0.0),
                          WindMouseOptions(tremor=0.0, end_scatter=0.0), rng=_rng(6))
    max_off = max(abs(y) for _, y, _ in pts)
    assert max_off < 40.0  # wind walk still curves it, but no broadband jitter


def test_ease_makes_the_cursor_start_slow_then_speed_up() -> None:
    # The ease envelope should make the FIRST quarter of the move slower (lower velocity)
    # than the middle for most paths — the human slow-start/accelerate reach.
    import statistics
    slower_starts = 0
    trials = 0
    for seed in range(60):
        pts = wind_mouse_path((0.0, 0.0), (900.0, 0.0),
                              WindMouseOptions(tremor=0.0, end_scatter=0.0), rng=_rng(seed))
        if len(pts) < 12:
            continue
        trials += 1
        vel = [math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
               / max(1e-3, pts[i][2] - pts[i - 1][2]) for i in range(1, len(pts))]
        q = max(1, len(vel) // 4)
        if statistics.mean(vel[:q]) < statistics.mean(vel[q:3 * q]):
            slower_starts += 1
    assert slower_starts >= int(trials * 0.8)  # the vast majority start slower than the middle
