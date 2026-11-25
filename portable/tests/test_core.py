"""Tests for the parts that need no GPU or video files: detection, retime plan, rates.
Run: python -m unittest discover -s tests"""

import random
import unittest
from fractions import Fraction

import numpy as np

from frame_repair.detect import COLS, ROWS, Differ
from frame_repair.plan import build_retime_plan, grid
from frame_repair.video import rate_for


def naive_difference(a: np.ndarray, b: np.ndarray) -> float:
    """Straight transcription of the Mac engine's frameDifference loop."""
    h, w = a.shape[:2]
    step = max(1, min(w, h) // 360)
    sums = [0] * (COLS * ROWS)
    counts = [0] * (COLS * ROWS)
    for y in range(0, h, step):
        cy = min(ROWS - 1, y * ROWS // h)
        for x in range(0, w, step):
            d = int(abs(int(a[y, x, 0]) - int(b[y, x, 0])) + abs(int(a[y, x, 1]) - int(b[y, x, 1]))
                    + abs(int(a[y, x, 2]) - int(b[y, x, 2])))
            c = cy * COLS + min(COLS - 1, x * COLS // w)
            sums[c] += d
            counts[c] += 3
    return max(s / c for s, c in zip(sums, counts) if c > 0)


def obs_capture(game_fps: float, seconds: float, rec_fps: float = 60, jitter: float = 0.3, seed: int = 1):
    """A game rendering at an unsteady rate, captured by a recorder at rec_fps: the
    recorder shows the newest finished game frame at each tick."""
    rnd = random.Random(seed)
    renders, t = [], 0.0
    while t < seconds + 1:
        renders.append(t)
        t += (1 / game_fps) * (1 + rnd.uniform(-jitter, jitter))
    pts, unique, shown, j = [], [], -1, 0
    for i in range(int(seconds * rec_fps)):
        c = i / rec_fps
        while j + 1 < len(renders) and renders[j + 1] <= c:
            j += 1
        pts.append(c)
        unique.append(j != shown)
        shown = j
    return pts, unique


class DetectTests(unittest.TestCase):
    def test_matches_reference_loop(self):
        rng = np.random.default_rng(0)
        for w, h in [(320, 180), (853, 479), (1280, 720)]:
            a = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
            b = a.copy()
            b[h // 3:h // 3 + 40, w // 2:w // 2 + 40] = rng.integers(0, 256, (40, 40, 3), dtype=np.uint8)
            self.assertAlmostEqual(Differ(w, h)(a, b), naive_difference(a, b), places=9)

    def test_repeat_is_zero_and_small_change_counts(self):
        a = np.full((720, 1280, 3), 90, np.uint8)
        d = Differ(1280, 720)
        self.assertEqual(d(a, a.copy()), 0.0)
        b = a.copy()
        b[10:30, 10:30] = 200                 # a small HUD change in one cell
        self.assertGreater(d(a, b), 1.5)


class PlanTests(unittest.TestCase):
    def test_retime_45_in_60(self):
        pts, unique = obs_capture(45, 4)
        plan = build_retime_plan(pts, unique, max_gap=4, smooth=3)
        self.assertEqual(plan.group, 1)
        self.assertEqual(plan.out_count, len(pts))
        self.assertEqual(len(plan.steps), len(pts))
        real = sum(unique)
        self.assertEqual(max(plan.unique_index) + 1, real)
        ks = [s[1] for s in plan.steps]
        self.assertEqual(ks, sorted(ks), "steps must walk forward through real frames")
        for s in plan.steps:
            if s[0] == "blend":
                self.assertTrue(0.02 <= s[2] <= 0.98)
        self.assertGreater(sum(s[0] == "blend" for s in plan.steps), len(pts) // 3)

    def test_film_look_group_and_count(self):
        pts, unique = obs_capture(45, 4)
        plan = build_retime_plan(pts, unique, max_gap=4, smooth=3, out_rate=Fraction(24), shutter=180)
        self.assertEqual(plan.group, 5)               # 1/48 s exposure, one moment per 1/240 s
        self.assertEqual(plan.out_count, 96)
        self.assertEqual(len(plan.steps), 96 * 5)

    def test_freeze_is_not_blended_across(self):
        pts = [i / 60 for i in range(60)]
        unique = [True] * 20 + [False] * 10 + [True] * 30     # 10 repeats > max_gap
        plan = build_retime_plan(pts, unique, max_gap=4, smooth=3)
        k_before = plan.unique_index[19]
        for s in plan.steps:
            self.assertFalse(s[0] == "blend" and s[1] == k_before, "blended across a freeze")

    def test_ntsc_grid(self):
        self.assertEqual(len(grid(0.0, 4.0, rate_for(59.94))), 239)
        self.assertEqual(len(grid(0.0, 4.0, Fraction(24))), 96)


class RateTests(unittest.TestCase):
    def test_rates(self):
        self.assertEqual(rate_for(60), Fraction(60))
        self.assertEqual(rate_for(59.94), Fraction(60000, 1001))
        self.assertEqual(rate_for(23.976), Fraction(24000, 1001))
        self.assertEqual(rate_for(29.97), Fraction(30000, 1001))
        self.assertEqual(rate_for(24), Fraction(24))


if __name__ == "__main__":
    unittest.main()
