"""The repair engine: fill mode, retime mode (even timing, frame-rate change, motion
blur) and the --dry-run analysis. Mirrors the Mac engine's behaviour and output."""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from .detect import Differ
from .plan import RetimePlan, build_retime_plan
from .video import Decoder, Encoder, Probe, VideoError, probe, rate_for


class RepairError(RuntimeError):
    pass


def fmt_time(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s) // 60}:{int(s) % 60:02d}.{int((s - math.floor(s)) * 1000):03d}"


class Progress:
    """--machine-progress (used by the Frame Repair app) prints "@progress <phase> <done>
    <total>" lines to stdout; otherwise a percentage line on stderr."""
    machine = False

    def __init__(self, phase: str) -> None:
        self.phase, self.last = phase, 0.0

    def tick(self, done: int, total: int, extra: str = "") -> None:
        now = time.monotonic()
        if now - self.last <= (0.25 if Progress.machine else 1.0):
            return
        self.last = now
        if Progress.machine:
            print(f"@progress {self.phase} {done} {total}", flush=True)
            return
        pct = min(100, done * 100 // total) if total > 0 else 0
        sys.stderr.write(f"\r  {pct}%  frame {done}/{total}  {extra}    ")
        sys.stderr.flush()

    def done(self) -> None:
        if Progress.machine:
            print(f"@progress {self.phase} 1 1", flush=True)
        else:
            sys.stderr.write("\n")


class Clock:
    """RIFE_REPAIR_TIMING=1: wall-clock totals per stage, printed at the end."""
    on = os.environ.get("RIFE_REPAIR_TIMING") == "1"
    totals: dict[str, float] = {}

    def __init__(self, key: str) -> None:
        self.key = key

    def __enter__(self):
        self.t0 = time.perf_counter()

    sync = None          # set to the device's synchronize() so GPU time lands in the right stage

    def __exit__(self, *exc):
        if Clock.on:
            if Clock.sync is not None:
                Clock.sync()
            Clock.totals[self.key] = Clock.totals.get(self.key, 0.0) + time.perf_counter() - self.t0

    @staticmethod
    def report() -> None:
        if Clock.on:
            print("Timing:")
            for k, v in sorted(Clock.totals.items(), key=lambda kv: -kv[1]):
                print(f"  {k:<16} {v:7.2f} s")


@dataclass
class Stats:
    frames: int = 0
    fixed_frames: int = 0
    fixed_runs: int = 0
    freezes: int = 0
    trailing: int = 0


@dataclass
class Options:
    input: str
    output: str | None = None
    dry_run: bool = False
    threshold: float = 1.5
    max_gap: int = 4
    tier: str | None = None
    shutter: float | None = None
    samples: int | None = None
    codec: str = "auto"
    bitrate: float | None = None
    model: str | None = None
    overwrite: bool = False
    retime: bool = False
    machine_progress: bool = False
    smooth: int = 3
    fps: float | None = None
    device: str = "auto"
    precision: str = "auto"
    encoder: str | None = None
    batch: int = 8
    hwaccel: bool = True
    tag: str | None = None


class Engine:
    def __init__(self, o: Options) -> None:
        self.o = o

    # ------------------------------------------------------------------ main

    def run(self) -> None:
        o = self.o
        Progress.machine = o.machine_progress
        src = Path(o.input).expanduser()
        if not src.is_file():
            raise RepairError(f"No such file: {src}")
        p = probe(src)
        print(f"Input:  {src.name}")
        print(f"        {p.width}x{p.height}  {p.fps:.3f} fps  {p.codec}  {p.bit_rate / 1e6:.1f} Mbps  "
              f"{fmt_time(p.duration)}  ~{p.est_frames} frames  {len(p.audio)} audio track(s)")
        if p.ten_bit:
            print("Note:   source is >8-bit. RIFE works in 8-bit, so the output is 8-bit (HDR, if any, becomes SDR).")
        if o.dry_run:
            self.analyse(p)
            return

        if o.output:
            out = Path(o.output).expanduser()
        elif o.tag:
            # Like the Mac app: "clip [24fps 180deg].mov", adding " 2", " 3" ... rather
            # than overwriting anything.
            base = f"{src.stem} [{o.tag}]"
            out, n = src.with_name(base + ".mov"), 2
            while out.exists():
                out, n = src.with_name(f"{base} {n}.mov"), n + 1
        else:
            out = src.with_name(f"{src.stem}-repaired{src.suffix or '.mov'}")
        if out.exists():
            if not o.overwrite:
                raise RepairError(f"{out} exists. Pass -y to overwrite.")
            out.unlink()

        if o.shutter is not None and not (0 < o.shutter <= 360):
            raise RepairError("--shutter must be between 1 and 360 degrees")
        if o.samples is not None and not (1 <= o.samples <= 64):
            raise RepairError("--samples must be between 1 and 64")
        tier = o.tier or ("balanced" if o.shutter is not None else "hq")
        if tier not in ("hq", "balanced", "fast"):
            raise RepairError("--tier must be hq, balanced or fast")
        if o.fps is not None and not (1 <= o.fps <= 240):
            raise RepairError("--fps must be between 1 and 240")
        codec = o.codec if o.codec != "auto" else ("prores" if p.is_prores else "hevc")
        if codec == "prores" and out.suffix.lower() in (".mp4", ".m4v"):
            raise RepairError("ProRes needs a .mov output; use -o name.mov or --codec hevc.")
        use_retime = o.retime or o.fps is not None or o.shutter is not None

        # Imported here so --help and --dry-run work without loading PyTorch.
        from .model import Interpolator, device_label, pick_device
        device = pick_device(o.device)
        rife = Interpolator(device, tier=tier, precision=o.precision, weights=o.model, max_batch=o.batch)
        prec = "fp16" if str(rife.dtype).endswith("float16") else "fp32"
        if Clock.on:
            Clock.sync = rife.sync

        plan: RetimePlan | None = None
        out_rate = p.rate
        if use_retime:
            print("Pass 1/2: finding real game frames ...")
            with Clock("pass 1 scan"):
                pts, unique = self.scan(p)
            target = rate_for(o.fps) if o.fps is not None else (p.rate if o.shutter is not None else None)
            plan = build_retime_plan(pts, unique, max_gap=o.max_gap, smooth=o.smooth, out_rate=target,
                                     shutter=o.shutter, samples=o.samples)
            print(plan.summary)
            if target is not None:
                out_rate = target
            print("Pass 2/2: rebuilding frames ...")

        enc = Encoder(p, out, out_rate, codec, o.bitrate, o.encoder)
        colour = "BT.601" if p.matrix == "bt601" else "BT.709"
        extra = (", retime" if use_retime else "") + (f", {o.fps:g} fps" if o.fps is not None else "")
        print(f"Output: {out.name}  ({codec} {colour}, RIFE {tier}, threshold {o.threshold:g}, "
              f"max gap {o.max_gap}{extra})")
        print(f"Using:  {device_label(device)} {prec}, encoder {enc.encoder}")
        if enc.note:
            print(f"Note:   {enc.note}")
        started = time.monotonic()
        try:
            if plan is not None:
                st = self.process_retime(p, plan, rife, enc)
            else:
                st = self.process_fill(p, rife, enc)
            with Clock("encode finish"):
                enc.close()
        except BaseException:
            enc.abort()
            try:
                out.unlink()
            except OSError:
                pass
            raise

        secs = time.monotonic() - started
        print(f"Done in {secs:.1f} s ({st.frames / max(secs, 0.001):.1f} fps)")
        print(f"  frames written:        {st.frames}")
        if plan is not None and plan.group > 1:
            print(f"  motion blur:           {o.shutter:g} degree shutter, {plan.group} moments per frame "
                  f"({st.fixed_frames} of {len(plan.steps)} generated by RIFE)")
        elif use_retime:
            print(f"  generated by RIFE:     {st.fixed_frames}   kept as-is: {st.frames - st.fixed_frames - st.freezes}")
        else:
            print(f"  dropped frames fixed:  {st.fixed_frames} in {st.fixed_runs} spot(s)")
        if st.freezes:
            print(f"  freezes left alone:    {st.freezes} (runs longer than --max-gap {o.max_gap})")
        if st.trailing:
            print(f"  trailing repeats kept: {st.trailing} (no next frame to interpolate toward)")
        print(f"  -> {out}")
        Clock.report()
        if Progress.machine:
            print(f"@output {out}", flush=True)

    # ------------------------------------------------------------------ fill

    def process_fill(self, p: Probe, rife, enc: Encoder) -> Stats:
        """Replace each short run of repeats with RIFE frames between the last real
        frame and the next one. Frame count and timing are unchanged."""
        o, st = self.o, Stats()
        differ = Differ(p.width, p.height)
        progress = Progress("render")
        anchor = None                  # (index, pts, frame) of the last real frame
        pending: list[float] = []      # times of repeats after the anchor

        def emit(f: np.ndarray) -> None:
            with Clock("encode"):
                enc.write(f)
            st.frames += 1

        dec = Decoder(p, hwaccel=o.hwaccel)
        try:
            for i, t, frame in dec:
                progress.tick(st.frames + len(pending), p.est_frames, f"fixed {st.fixed_frames}")
                if anchor is None:
                    emit(frame)
                    anchor = (i, t, frame)
                    continue
                with Clock("detect"):
                    d = differ(anchor[2], frame)
                if d < o.threshold:
                    pending.append(t)
                    continue
                if pending:
                    if len(pending) <= o.max_gap:
                        span = t - anchor[1]
                        steps = [(pt - anchor[1]) / span for pt in pending]
                        with Clock("rife"):
                            frames = rife.interpolate(anchor[0], anchor[2], i, frame, steps).cpu().numpy()
                        for f in frames:
                            emit(f)
                        st.fixed_frames += len(pending)
                        st.fixed_runs += 1
                    else:
                        for _ in pending:
                            emit(anchor[2])
                        st.freezes += len(pending)
                    pending.clear()
                emit(frame)
                anchor = (i, t, frame)
        finally:
            dec.close()
        if anchor is not None:
            for _ in pending:
                emit(anchor[2])
            st.trailing = len(pending)
        progress.done()
        return st

    # ------------------------------------------------------------------ retime

    def scan(self, p: Probe) -> tuple[list[float], list[bool]]:
        """Pass 1: decode every frame and mark which ones are new game frames."""
        differ = Differ(p.width, p.height)
        pts: list[float] = []
        unique: list[bool] = []
        anchor = None
        progress = Progress("scan")
        dec = Decoder(p, hwaccel=self.o.hwaccel)
        try:
            for _, t, frame in dec:
                pts.append(t)
                progress.tick(len(pts), p.est_frames)
                if anchor is not None and differ(anchor, frame) < self.o.threshold:
                    unique.append(False)
                else:
                    unique.append(True)
                    anchor = frame
        finally:
            dec.close()
        progress.done()
        return pts, unique

    def process_retime(self, p: Probe, plan: RetimePlan, rife, enc: Encoder) -> Stats:
        """Pass 2: decode again, keep the few real frames the plan still needs, and emit
        every moment in order. Moments between the same pair go to RIFE as one batch;
        each real frame's encoder features are computed once and reused."""
        import torch

        st = Stats()
        progress = Progress("render")
        store: dict[int, np.ndarray] = {}
        have = -1           # highest real-frame index decoded so far
        nxt = 0             # next step to emit
        steps = plan.steps
        acc = None          # motion blur: running sum of the current frame's moments
        in_acc = 0

        def write(f: np.ndarray) -> None:
            with Clock("encode"):
                enc.write(f)
            st.frames += 1

        def deliver(frame) -> None:
            """frame: numpy (a real frame) or a device tensor (RIFE output)."""
            nonlocal acc, in_acc
            if plan.group == 1:
                write(frame if isinstance(frame, np.ndarray) else frame.cpu().numpy())
                return
            with Clock("blur average"):
                t = rife.upload(frame) if isinstance(frame, np.ndarray) else frame
                acc = t.to(torch.int32) if acc is None else acc.add_(t)
                in_acc += 1
                if in_acc < plan.group:
                    return
                n = plan.group
                out = torch.div(acc + n // 2, n, rounding_mode="floor").to(torch.uint8).cpu().numpy()
                acc, in_acc = None, 0
            write(out)

        def needs(s) -> int:
            return s[1] + 1 if s[0] == "blend" else s[1]

        def emit_ready() -> None:
            nonlocal nxt
            while nxt < len(steps) and needs(steps[nxt]) <= have:
                s = steps[nxt]
                if s[0] == "copy":
                    deliver(store[s[1]])
                    nxt += 1
                else:
                    k = s[1]
                    ts = []
                    while nxt < len(steps) and steps[nxt][0] == "blend" and steps[nxt][1] == k:
                        ts.append(steps[nxt][2])
                        nxt += 1
                    with Clock("rife"):
                        frames = rife.interpolate(k, store[k], k + 1, store[k + 1], ts)
                    for f in frames:
                        deliver(f)
                    st.fixed_frames += len(ts)
                if nxt < len(steps):
                    keep = steps[nxt][1]
                    for key in [key for key in store if key < keep]:
                        del store[key]

        n_in = 0
        dec = Decoder(p, hwaccel=self.o.hwaccel)
        try:
            for _, _, frame in dec:
                if n_in < len(plan.unique_index) and plan.unique_index[n_in] >= 0:
                    have = plan.unique_index[n_in]
                    store[have] = frame
                n_in += 1
                emit_ready()
                progress.tick(st.frames, plan.out_count, f"generated {st.fixed_frames}")
        finally:
            dec.close()
        emit_ready()
        progress.done()
        if nxt != len(steps):
            raise RepairError(f"Second read returned a different frame sequence "
                              f"({n_in} frames vs {len(plan.unique_index)}).")
        return st

    # ------------------------------------------------------------------ dry run

    def analyse(self, p: Probe) -> None:
        o = self.o
        differ = Differ(p.width, p.height)
        edges = [0.1, 0.5, 1, 1.5, 2, 3, 5, 10]
        hist = [0] * (len(edges) + 1)
        runs: list[tuple[float, int]] = []
        anchor = None
        run_start, run_len, n = 0.0, 0, 0
        unique_per_sec: dict[int, int] = {}
        progress = Progress("analyse")
        dec = Decoder(p, hwaccel=o.hwaccel)
        try:
            for _, t, frame in dec:
                n += 1
                progress.tick(n, p.est_frames, f"runs {len(runs)}")
                if anchor is None:
                    anchor = frame
                    unique_per_sec[int(t)] = unique_per_sec.get(int(t), 0) + 1
                    continue
                d = differ(anchor, frame)
                hist[next((i for i, e in enumerate(edges) if d < e), len(edges))] += 1
                if d < o.threshold:
                    if run_len == 0:
                        run_start = t
                    run_len += 1
                else:
                    if run_len > 0:
                        runs.append((run_start, run_len))
                    run_len = 0
                    anchor = frame
                    unique_per_sec[int(t)] = unique_per_sec.get(int(t), 0) + 1
        finally:
            dec.close()
        if run_len > 0:
            runs.append((run_start, run_len))
        progress.done()

        print("\nFrame-to-frame difference (worst cell, 0-255) — duplicates sit near 0:")
        lo, total = 0.0, max(1, sum(hist))
        for i, c in enumerate(hist):
            label = f"{lo:5.1f} - {edges[i]:<5.1f}" if i < len(edges) else f"{lo:5.1f} +      "
            print(f"  {label} {c:7d}  {'#' * (c * 50 // total)}")
            if i < len(edges):
                lo = float(edges[i])

        rates = sorted(unique_per_sec[s] for s in sorted(unique_per_sec)[:-1])
        if rates:
            full = int(math.floor(p.fps * 0.95))
            below = sum(1 for r in rates if r < full)
            print(f"\nGame frame rate (real frames per second of recording): min {rates[0]}  "
                  f"median {rates[len(rates) // 2]}  max {rates[-1]}")
            hint = ("  -> unsteady/low: use --retime for even motion" if below * 5 > len(rates)
                    else "  -> mostly full rate: default fill mode is enough")
            print(f"  seconds under {full} fps: {below} of {len(rates)}{hint}")

        fixable = [r for r in runs if r[1] <= o.max_gap]
        freezes = [r for r in runs if r[1] > o.max_gap]
        print(f"\nBelow threshold {o.threshold:g}: {sum(r[1] for r in runs)} repeated frames in {len(runs)} run(s)")
        print(f"  would repair: {sum(r[1] for r in fixable)} frames in {len(fixable)} run(s) (length <= {o.max_gap})")
        print(f"  would leave:  {sum(r[1] for r in freezes)} frames in {len(freezes)} freeze(s)")
        lens: dict[int, int] = {}
        for r in runs:
            lens[r[1]] = lens.get(r[1], 0) + 1
        if lens:
            print("  run lengths:  " + "  ".join(f"{k}x{lens[k]}" for k in sorted(lens)[:12]))
        if runs:
            print("\nFirst runs (time, repeated frames):")
            for start, length in runs[:25]:
                print(f"  {fmt_time(start)}  {length}{'  (freeze, left alone)' if length > o.max_gap else ''}")
            if len(runs) > 25:
                print(f"  ... {len(runs) - 25} more")
