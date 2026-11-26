"""Repeated-frame detection: the same worst-cell metric as the Mac engine."""

from __future__ import annotations

import numpy as np

COLS, ROWS = 32, 18


class Differ:
    """Splits both frames into a 32x18 grid and returns the largest per-cell mean
    absolute difference (0-255). A true repeat is ~0 in every cell; a real frame
    where only a small HUD element changed still scores high in that cell, which a
    whole-frame average would miss. Samples ~360 lines on the short side."""

    def __init__(self, width: int, height: int) -> None:
        self.step = step = max(1, min(width, height) // 360)
        ys, xs = np.arange(0, height, step), np.arange(0, width, step)
        cy = np.minimum(ROWS - 1, ys * ROWS // height)
        cx = np.minimum(COLS - 1, xs * COLS // width)
        # Sampled rows/columns are in cell order, so each cell is a contiguous block.
        self.rstart = np.flatnonzero(np.r_[True, cy[1:] != cy[:-1]])
        self.cstart = np.flatnonzero(np.r_[True, cx[1:] != cx[:-1]])
        rlen = np.diff(np.r_[self.rstart, len(ys)])
        clen = np.diff(np.r_[self.cstart, len(xs)])
        self.counts = (rlen[:, None] * clen[None, :] * 3).astype(np.float64)

        self.cstart3 = self.cstart * 3

    def __call__(self, a: np.ndarray, b: np.ndarray) -> float:
        s = self.step
        d = a[::s, ::s].astype(np.int16)
        d -= b[::s, ::s]
        np.abs(d, out=d)
        # Rows x (columns * RGB): a cell's three channels sit side by side, so the
        # per-cell sums need no separate channel sum. Integer sums, so exact.
        d = d.reshape(d.shape[0], -1)
        cells = np.add.reduceat(np.add.reduceat(d, self.rstart, axis=0, dtype=np.int32), self.cstart3, axis=1)
        return float((cells / self.counts).max())
