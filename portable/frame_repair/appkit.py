"""App logic without any UI: settings -> engine arguments, presets, output names, and
parsing of the engine's output. A port of the Mac app's FrameRepairKit, so both apps
make the same choices and the same file names. Imported by gui.py; testable without Qt.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Callable, Optional

# OBS on Windows records .mkv by default, so the app takes more than the Mac's .mov/.mp4/.m4v.
MOVIE_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".ts", ".m2ts", ".mts", ".flv"}

QUALITY_LABELS = {"auto": "Auto", "hq": "Best", "balanced": "Balanced", "fast": "Fast"}
FORMAT_LABELS = {"prores": "ProRes 422 HQ (for editing)", "hevc": "HEVC", "h264": "H.264", "auto": "Match source"}
TIMING_LABELS = {"fill": "Fill repeated frames", "even": "Even out timing"}
FRAME_RATES: list[Optional[float]] = [None, 24, 25, 30, 48, 50, 59.94, 60, 120]


def num(v: float) -> str:
    """24.0 -> "24", 59.94 -> "59.94" (like the Mac app)."""
    return str(int(v)) if float(v) == round(v) else f"{v:g}"


def rate_label(r: Optional[float]) -> str:
    if r is None:
        return "Same as source"
    return f"{int(r)} fps" if float(r) == round(r) else f"{r:.3g} fps"


@dataclass
class RepairSettings:
    frame_rate: Optional[float] = 24.0      # None = same as source
    timing: str = "even"                    # "fill" | "even"
    motion_blur: bool = True
    shutter: float = 180.0
    quality: str = "auto"                   # auto | hq | balanced | fast
    format: str = "prores"                  # prores | hevc | h264 | auto
    output_folder: Optional[str] = None     # None = next to the original
    threshold: float = 1.5
    max_gap: int = 4
    samples: Optional[int] = None           # None = engine default

    @property
    def effective_timing(self) -> str:
        """Changing the frame rate or adding blur always uses the retime engine."""
        return "even" if (self.frame_rate is not None or self.motion_blur) else self.timing

    def arguments(self, input: Path, output: Path) -> list[str]:
        """Arguments for the engine (after `python -m frame_repair`)."""
        a = [str(input), "-o", str(output), "--machine-progress"]
        if self.frame_rate is not None:
            a += ["--fps", num(self.frame_rate)]
        if self.effective_timing == "even" and self.frame_rate is None and not self.motion_blur:
            a.append("--retime")
        if self.motion_blur:
            a += ["--shutter", num(self.shutter)]
        if self.samples is not None and self.motion_blur:
            a += ["--samples", str(self.samples)]
        if self.quality != "auto":
            a += ["--tier", self.quality]
        a += ["--codec", self.format]
        if self.threshold != 1.5:
            a += ["--threshold", num(self.threshold)]
        if self.max_gap != 4:
            a += ["--max-gap", str(self.max_gap)]
        return a

    @staticmethod
    def analysis_arguments(input: Path) -> list[str]:
        return [str(input), "--dry-run", "--machine-progress"]

    @property
    def file_tag(self) -> str:
        """Short tag for file names: "24fps 180deg", "60fps", "fixed", "smoothed"."""
        parts = []
        if self.frame_rate is not None:
            parts.append(f"{num(self.frame_rate)}fps")
        if self.motion_blur:
            parts.append(f"{num(self.shutter)}deg")
        if not parts:
            parts.append("smoothed" if self.effective_timing == "even" else "fixed")
        return " ".join(parts)

    def output_path(self, input: Path, exists: Callable[[Path], bool] = Path.exists) -> Path:
        """"<name> [<tag>].mov" next to the original (or in the chosen folder), adding
        " 2", " 3" ... rather than overwriting anything."""
        folder = Path(self.output_folder) if self.output_folder else input.parent
        base = f"{input.stem} [{self.file_tag}]"
        out, n = folder / f"{base}.mov", 2
        while exists(out):
            out, n = folder / f"{base} {n}.mov", n + 1
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RepairSettings":
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in (d or {}).items() if k in known})
        # Sanitise anything a hand-edited or older settings file might hold.
        if s.timing not in TIMING_LABELS:
            s.timing = "even"
        if s.quality not in QUALITY_LABELS:
            s.quality = "auto"
        if s.format not in FORMAT_LABELS:
            s.format = "prores"
        return s

    def copy(self) -> "RepairSettings":
        return replace(self)


FILM_LOOK = RepairSettings()
FIX_ONLY = RepairSettings(frame_rate=None, timing="fill", motion_blur=False, format="auto")


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str

    def apply(self, s: RepairSettings) -> RepairSettings:
        """This preset's look applied to s, keeping the output choices (format, folder)."""
        if self.key == "manual":
            return s.copy()
        t = replace(s, threshold=1.5, max_gap=4, samples=None, quality="auto")
        if self.key == "film":
            t = replace(t, frame_rate=24.0, timing="even", motion_blur=True, shutter=180.0)
        elif self.key == "fix":
            t = replace(t, frame_rate=None, timing="fill", motion_blur=False)
        elif self.key == "smooth60":
            t = replace(t, frame_rate=60.0, timing="even", motion_blur=False)
        return t


PRESETS = [
    Preset("film", "Film look (24 fps, motion blur)",
           "24 fps with a 180° shutter, so motion smears like a film camera. Timing is evened out "
           "first. For cinematic edits."),
    Preset("fix", "Fix dropped frames only",
           "Replaces only the repeated frames. Frame rate, length and everything else stay as recorded."),
    Preset("smooth60", "Smooth 60 fps",
           "Evens out timing and rebuilds every frame on a steady 60 fps grid, with no blur. For "
           "gameplay that should feel fluid."),
    Preset("manual", "Manual", "Set frame rate, timing, blur, quality and repeat detection yourself."),
]
PRESET_BY_KEY = {p.key: p for p in PRESETS}


def matching_preset(s: RepairSettings) -> Optional[Preset]:
    """The named preset whose look s currently has, if any."""
    for p in PRESETS:
        if p.key != "manual" and p.apply(s) == s:
            return p
    return None


# --------------------------------------------------------------------------- engine output

def parse_event(line: str) -> tuple:
    """("progress", phase, done, total) | ("output", path) | ("text", line)."""
    if line.startswith("@progress "):
        p = line.split(" ")
        if len(p) == 4 and p[2].isdigit() and p[3].isdigit():
            return ("progress", p[1], int(p[2]), int(p[3]))
    if line.startswith("@output "):
        return ("output", line[8:])
    return ("text", line)


class LineBuffer:
    """Splits a stream into lines on \\n or \\r, keeping any partial line for later."""

    def __init__(self) -> None:
        self.pending = ""

    def feed(self, chunk: str) -> list[str]:
        self.pending += chunk
        parts = re.split(r"[\r\n]", self.pending)
        self.pending = parts.pop()
        return [p for p in parts if p.strip()]

    def flush(self) -> list[str]:
        rest, self.pending = self.pending, ""
        return [rest] if rest.strip() else []


def overall_fraction(phase: str, done: int, total: int, two_pass: bool) -> float:
    """0..1 overall progress. The retime engine reads the clip twice: a quick scan, then the render."""
    f = min(1.0, max(0.0, done / total)) if total > 0 else 0.0
    if not two_pass:
        return f
    return f * 0.1 if phase == "scan" else 0.1 + f * 0.9


@dataclass
class ClipAnalysis:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: str = ""
    game_fps_min: Optional[int] = None
    game_fps_median: Optional[int] = None
    game_fps_max: Optional[int] = None
    repeated: int = 0
    fixable: int = 0
    freezes: int = 0
    unsteady: bool = False

    @property
    def summary(self) -> str:
        fps = f"{round(self.fps)} fps" if abs(self.fps - round(self.fps)) < 0.01 else f"{self.fps:.2f} fps"
        s = f"{self.width}x{self.height}  {fps}  {self.duration}"
        if self.game_fps_median is not None:
            s += f"  |  game ~{self.game_fps_median} fps"
        if self.game_fps_min is not None and self.game_fps_max is not None and self.game_fps_min != self.game_fps_max:
            s += f" ({self.game_fps_min}-{self.game_fps_max})"
        s += f"  |  {self.repeated} repeated frame{'' if self.repeated == 1 else 's'}"
        if self.freezes > 0:
            s += f", {self.freezes} in pauses"
        return s

    @property
    def advice(self) -> str:
        if self.repeated == 0:
            return "No repeated frames found."
        return ("Game ran below the recording rate: even out timing (or a frame-rate change) works best."
                if self.unsteady else "Mostly full rate: filling the repeated frames is enough.")

    @classmethod
    def parse(cls, text: str) -> "ClipAnalysis":
        a = cls()
        for raw in re.split(r"[\r\n]+", text):
            line = raw.strip()
            m = None
            if a.width == 0 and (m := re.match(r"^(\d+)x(\d+)\s+([\d.]+) fps\s+\S+\s+[\d.]+ Mbps\s+(\S+)", line)):
                a.width, a.height, a.fps, a.duration = int(m[1]), int(m[2]), float(m[3]), m[4]
            elif m := re.match(r"^Game frame rate.*min (\d+)\s+median (\d+)\s+max (\d+)", line):
                a.game_fps_min, a.game_fps_median, a.game_fps_max = int(m[1]), int(m[2]), int(m[3])
            elif "-> unsteady/low" in line:
                a.unsteady = True
            elif m := re.match(r"^Below threshold [\d.]+: (\d+) repeated frames", line):
                a.repeated = int(m[1])
            elif m := re.match(r"^would repair: (\d+) frames", line):
                a.fixable = int(m[1])
            elif m := re.match(r"^would leave:\s+(\d+) frames", line):
                a.freezes = int(m[1])
        return a
