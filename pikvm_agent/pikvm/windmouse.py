"""WindMouse human-mouse path generator.

A faithful port of ``src/windmouse.ts`` from ``~/dev/pikvm-desktop-agentic`` (in
turn ben.land's WindMouse gravity+wind force integration plus the shared
speed-scaled jitter / endpoint-scatter / bursty-timing post-processing).

The force constants (gravity, wind, max_step, the D0=12 transition distance) are
tuned for PIXEL-scale coordinates, so callers must run this in pixel space and
convert the resulting points into whatever the device wants. Output is a list of
``(x, y, t)`` samples where ``t`` is an absolute millisecond timestamp from the
start of the move.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass

Pt = tuple[float, float]
Sample = tuple[float, float, float]  # (x, y, t_ms)

Rng = Callable[[], float]


@dataclass(frozen=True)
class WindMouseOptions:
    speed: float = 1.9        # higher is faster (shorter duration)
    gravity: float = 8.0      # pull toward the target (lower = more wander)
    wind: float = 4.2         # random-walk magnitude (higher = curvier / less robotic)
    max_step: float = 15.0    # maximum per-step velocity
    hz: float = 120.0         # sample rate the bursty timing is built around
    tremor: float = 0.7       # perpendicular tremor amplitude (0 disables jitter)
    end_scatter: float = 2.0  # std-dev of the off-centre landing scatter (px)
    hes: float = 0.04         # hesitation rate (chance of a small mid-move pause)
    dtjit: float = 0.35       # per-step dt jitter fraction
    ease: float = 1.1         # slow-start + slow-land envelope strength (0 = uniform pace)


def _gauss(rng: Rng) -> float:
    """Box-Muller standard normal (matches the TS port's gauss())."""
    u = 0.0
    v = 0.0
    while u == 0.0:
        u = rng()
    while v == 0.0:
        v = rng()
    return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)


def _geometry(start: Pt, end: Pt, o: WindMouseOptions, rng: Rng) -> list[Pt]:
    """WindMouse force integration — the raw geometric pixel path."""
    sqrt3 = math.sqrt(3)
    sqrt5 = math.sqrt(5)
    g = o.gravity
    w = o.wind
    d0 = 12.0
    m = o.max_step
    cx, cy = start
    vx = vy = wx = wy = 0.0
    pts: list[Pt] = [(cx, cy)]
    guard = 0
    while True:
        dx = end[0] - cx
        dy = end[1] - cy
        d = math.hypot(dx, dy)
        if d < 1:
            break
        wm = min(w, d)
        if d >= d0:
            wx = wx / sqrt3 + (2 * rng() - 1) * wm / sqrt5
            wy = wy / sqrt3 + (2 * rng() - 1) * wm / sqrt5
        else:
            wx /= sqrt3
            wy /= sqrt3
            if m < 3:
                m = 3 + rng() * 3
            else:
                m /= sqrt5
        vx += wx + (g * dx) / d
        vy += wy + (g * dy) / d
        vmag = math.hypot(vx, vy)
        if vmag > m:
            vc = m / 2 + (rng() * m) / 2
            vx = (vx / vmag) * vc
            vy = (vy / vmag) * vc
        cx += vx
        cy += vy
        pts.append((cx, cy))
        guard += 1
        if guard > 8000:
            break
    pts.append((end[0], end[1]))
    return pts


def wind_mouse_path(start: Pt, end: Pt, opts: WindMouseOptions | None = None,
                    rng: Rng | None = None) -> list[Sample]:
    """Geometry + speed-scaled perpendicular jitter + endpoint scatter + bursty,
    duration-preserving timestamps. Duration follows Fitts's law scaled by ``speed``."""
    o = opts or WindMouseOptions()
    rng = rng or random.random
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    if dist < 1:
        return [(start[0], start[1], 0.0)]

    # Fitts's law: total duration from distance + a virtual target width.
    vw = 12.0
    idx = math.log2(dist / vw + 1)
    dur = (120 + 200 * idx) / o.speed
    dur *= 1 + _gauss(rng) * 0.08
    dur = max(70.0, dur)

    geo = _geometry(start, end, o, rng)
    n = len(geo)
    steps = [0.0] + [math.hypot(geo[i][0] - geo[i - 1][0], geo[i][1] - geo[i - 1][1])
                     for i in range(1, n)]
    max_step = max(steps) or 1.0

    samples: list[list[float]] = []  # mutable [x, y, t]
    lf = 0.0
    for i in range(n):
        x, y = geo[i]
        spd = steps[i] / max_step
        nx = ny = 0.0
        if i > 0:
            ddx = x - geo[i - 1][0]
            ddy = y - geo[i - 1][1]
            dl = math.hypot(ddx, ddy) or 1.0
            nx = -ddy / dl
            ny = ddx / dl
        lf = lf * 0.6 + _gauss(rng) * 0.4
        hf = _gauss(rng)
        amp = o.tremor * (0.15 + spd) * 0.6
        perp = (hf * 0.3 + lf * 1.0) * amp
        samples.append([x + nx * perp, y + ny * perp, 0.0])

    # Endpoint scatter: blend an off-centre landing across the final approach.
    if len(samples) > 3:
        ox = _gauss(rng) * o.end_scatter
        oy = _gauss(rng) * o.end_scatter
        k = max(2, round(len(samples) * 0.12))
        for i in range(len(samples) - k, len(samples)):
            w = (i - (len(samples) - k)) / ((k - 1) or 1)
            samples[i][0] += ox * w
            samples[i][1] += oy * w
        samples[-1][0] = end[0] + ox
        samples[-1][1] = end[1] + oy

    # Bursty, duration-preserving timestamps (matches a real dt distribution:
    # ~6% coalesced bursts, occasional hesitations, bulk near the poll interval).
    dt_base = 1000.0 / o.hz
    raw_dt = [0.0]
    nseg = max(1, len(samples) - 1)
    for i in range(1, len(samples)):
        roll = rng()
        if roll < 0.06:
            dt = rng() * 1.5
        elif roll < 0.06 + o.hes * 0.3:
            dt = 25 + rng() * 110
        else:
            dt = dt_base * (1 + (rng() * 2 - 1) * o.dtjit)
        # Velocity envelope: spend MORE time per step near the start and the landing, less in
        # the middle — so the cursor visibly starts slow, accelerates, then eases into the
        # target (the classic human reach), instead of a near-uniform pace. Renormalised
        # below so total duration is unchanged. (1 - sin(pi*f)) is 1 at the ends, 0 mid.
        f = i / nseg
        dt *= 1.0 + o.ease * (1.0 - math.sin(math.pi * f))
        raw_dt.append(dt)
    sum_dt = sum(raw_dt) or 1.0
    scale = dur / sum_dt
    t_abs = 0.0
    for i in range(len(samples)):
        t_abs += raw_dt[i] * scale
        samples[i][2] = t_abs

    return [(s[0], s[1], s[2]) for s in samples]
