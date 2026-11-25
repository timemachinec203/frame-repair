"""Retime plan: estimate when each real game frame was rendered, then decide what
every output frame shows. A 1:1 port of the Mac engine's buildRetimePlan."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

# A step is ("copy", k) or ("blend", k, s): real frame k as-is, or a RIFE frame
# between real frames k and k+1 at position s in (0, 1).
Step = tuple


@dataclass
class RetimePlan:
    steps: list[Step]          # one per moment; `group` consecutive moments make one output frame
    out_count: int             # output frames
    group: int                 # moments averaged per output frame (1 = no motion blur)
    unique_index: list[int]    # input frame -> real-frame index k, or -1 for a repeat
    summary: str


def grid(start: float, duration: float, rate: Fraction) -> list[float]:
    """Output times on an even grid at `rate` covering [start, start + duration)."""
    count = int(duration * float(rate) + 1e-6)
    return [start + float(Fraction(i) / rate) for i in range(count)]


def build_retime_plan(pts: list[float], unique: list[bool], *, max_gap: int, smooth: int,
                      out_rate: Fraction | None = None, shutter: float | None = None,
                      samples: int | None = None) -> RetimePlan:
    """The recorder grabs a picture every tick and shows the newest game frame, so a
    frame first seen at capture time c was rendered somewhere in (c - tick, c]. Using c
    as-is leaves up to a tick of timing error per frame (the judder of e.g. a 45 fps
    game in a 60 fps recording). A local straight line through the capture times of
    nearby real frames (the game's local frame rate) gives a better estimate, clamped
    into that window and kept in order. Runs of repeats longer than max_gap are real
    freezes: no smoothing or blending across them."""
    secs = pts
    gaps = sorted(b - a for a, b in zip(secs, secs[1:]) if b - a > 0) or [1 / 60]
    tick = gaps[len(gaps) // 2]

    cap: list[float] = []
    seg: list[int] = []
    unique_index = [-1] * len(pts)
    seg_id = run = freeze_frames = 0
    for i, u in enumerate(unique):
        if u:
            if run > max_gap:
                seg_id += 1
                freeze_frames += run
            run = 0
            unique_index[i] = len(cap)
            cap.append(secs[i])
            seg.append(seg_id)
        else:
            run += 1
    n = len(cap)

    # Local linear fit of capture time vs real-frame index, within the segment.
    t = list(cap)
    w = max(1, smooth)
    for k in range(n):
        lo, hi = max(0, k - w), min(n - 1, k + w)
        while seg[lo] != seg[k]:
            lo += 1
        while seg[hi] != seg[k]:
            hi -= 1
        m = hi - lo + 1
        if m < 3:
            continue
        sx = sy = sxx = sxy = 0.0
        for j in range(lo, hi + 1):
            x, y = float(j - k), cap[j]
            sx += x; sy += y; sxx += x * x; sxy += x * y
        den = m * sxx - sx * sx
        slope = (m * sxy - sx * sy) / den if den != 0 else 0.0
        est = (sy - slope * sx) / m                    # the fit at x = 0 (this frame)
        t[k] = min(cap[k], max(cap[k] - tick, est))
    for k in range(1, n):
        if seg[k] == seg[k - 1]:
            t[k] = min(cap[k], max(t[k], t[k - 1] + 0.05 * tick))

    # Output ticks: the input timestamps, or an even grid at out_rate over the same span.
    if out_rate is not None and pts:
        out_pts = grid(pts[0], secs[-1] - secs[0] + tick, out_rate)
    else:
        out_pts = list(pts)

    # Moments to render: one per output frame, or with a shutter `group` moments spread
    # across the exposure (opens at the frame's time, lasts angle/360 of a frame).
    group = 1
    if shutter is not None:
        rate = float(out_rate) if out_rate is not None else 1 / tick
        exposure = shutter / 360 / rate
        group = samples or max(2, math.ceil(exposure * 240 - 1e-9))
        moments = [tp + i * exposure / group for tp in out_pts for i in range(group)]
    else:
        moments = out_pts

    steps: list[Step] = []
    k = blends = 0
    for T in moments:
        if n == 0:
            break
        while k + 1 < n and t[k + 1] <= T:
            k += 1
        if T <= t[k] or k + 1 >= n or seg[k + 1] != seg[k]:
            steps.append(("copy", k))
            continue
        s = (T - t[k]) / (t[k + 1] - t[k])
        if s < 0.02:
            steps.append(("copy", k))
        elif s > 0.98:
            steps.append(("copy", k + 1))
        else:
            steps.append(("blend", k, s))
            blends += 1

    # Game frame-rate estimate per second of recording (the last second is partial).
    per_sec: dict[int, int] = {}
    for c in cap:
        per_sec[int(c)] = per_sec.get(int(c), 0) + 1
    rates = sorted(per_sec[s] for s in sorted(per_sec)[:-1])
    rate_text = f"min {rates[0]}  median {rates[len(rates) // 2]}  max {rates[-1]}" if rates else "n/a"
    blur = f" x {group} moments each for motion blur" if group > 1 else ""
    frozen = f", {freeze_frames} in freezes left alone" if freeze_frames else ""
    summary = (f"  real game frames: {n} of {len(pts)}   game fps per second: {rate_text}\n"
               f"  plan: {len(out_pts)} output frames{blur}: {blends} generated, "
               f"{len(moments) - blends} kept{frozen}")
    return RetimePlan(steps, len(out_pts), group, unique_index, summary)
