"""Video in and out through ffmpeg: probe, decode to RGB frames, encode RGB frames.

Decoding uses `-hwaccel auto` (NVDEC, VAAPI, D3D11VA, VideoToolbox... when present).
Encoding picks the first hardware encoder that actually works on this machine
(Nvidia NVENC, AMD AMF, Intel QSV, VAAPI on Linux, VideoToolbox on macOS) and falls
back to the CPU encoders (libx265 / libx264). ProRes uses prores_ks_vulkan (GPU) when
it works, else prores_ks.

On Windows the frame pipes get a 16 MB buffer (see big_pipe).
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np


class VideoError(RuntimeError):
    pass


PIPE_BUFFER = 16 << 20


def big_pipe() -> tuple[int, int] | None:
    """(read fd, write fd) of a pipe with a 16 MB buffer, on Windows only. Python's
    default pipes there have a small buffer, so a 1440p RGB stream (11 MB a frame) goes
    through in hundreds of small reads and tops out near 140 fps; this reaches ~220."""
    if sys.platform != "win32":
        return None
    import _winapi
    import msvcrt
    r, w = _winapi.CreatePipe(None, PIPE_BUFFER)
    return msvcrt.open_osfhandle(r, os.O_RDONLY), msvcrt.open_osfhandle(w, 0)


def tool(name: str) -> str:
    """Path to ffmpeg / ffprobe: $FFMPEG / $FFPROBE, else PATH."""
    env = os.environ.get(name.upper())
    if env:
        return env
    found = shutil.which(name)
    if not found:
        raise VideoError(f"{name} not found. Install ffmpeg (Windows: winget install ffmpeg; "
                         f"Ubuntu/Debian: sudo apt install ffmpeg; macOS: brew install ffmpeg) "
                         f"or set ${name.upper()} to its path.")
    return found


# --------------------------------------------------------------------------- probe

@dataclass
class Probe:
    path: Path
    width: int
    height: int
    rate: Fraction            # constant output rate for this source
    duration: float
    start: float              # video stream start time (s)
    container_start: float    # earliest start of any stream (s)
    codec: str
    bit_rate: float           # bits/s (0 if unknown)
    pix_fmt: str
    matrix: str               # "bt709" | "bt601"  (for decoding and output tags)
    primaries: str            # ffmpeg name: bt709 | smpte170m | bt470bg
    in_range: str             # "tv" | "pc"
    ten_bit: bool
    audio: list[str] = field(default_factory=list)   # audio codec names

    @property
    def fps(self) -> float:
        return float(self.rate)

    @property
    def est_frames(self) -> int:
        return int(round(self.duration * self.fps))

    @property
    def is_prores(self) -> bool:
        return self.codec == "prores"


def _frac(s: str | None) -> Fraction | None:
    try:
        f = Fraction(s) if s else None
        return f if f and f > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path: Path) -> Probe:
    cmd = [tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoError(f"ffprobe could not read {path.name}: {r.stderr.strip()}")
    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    if v is None:
        raise VideoError(f"No video track in {path.name}")
    fmt = info.get("format", {})

    # Rate: r_frame_rate unless it disagrees with the average (variable-rate source).
    r_rate, avg = _frac(v.get("r_frame_rate")), _frac(v.get("avg_frame_rate"))
    rate = r_rate or avg or Fraction(60)
    if r_rate and avg and abs(float(r_rate) / float(avg) - 1) > 0.005:
        rate = avg.limit_denominator(1001)

    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) % 180 == 90:                 # ffmpeg auto-rotates while decoding
        w, h = h, w

    duration = float(v.get("duration") or fmt.get("duration") or 0)
    start = float(v.get("start_time") or 0)
    starts = [float(s["start_time"]) for s in streams if s.get("start_time") not in (None, "N/A")]
    bit_rate = float(v.get("bit_rate") or fmt.get("bit_rate") or 0)
    pix_fmt = v.get("pix_fmt", "")

    # Keep the source colour standard (older OBS defaults to BT.601, newer to 709).
    # Anything else (HDR, unknown) falls back to BT.709: the pipeline is 8-bit SDR.
    cs = v.get("color_space", "")
    matrix = "bt601" if cs in ("smpte170m", "bt470bg") else "bt709"
    prim = v.get("color_primaries", "")
    if prim not in ("bt709", "smpte170m", "bt470bg"):
        prim = "smpte170m" if matrix == "bt601" else "bt709"
    in_range = "pc" if v.get("color_range") == "pc" or pix_fmt.startswith("yuvj") else "tv"
    ten_bit = bool(re.search(r"(p10|p12|p16|10le|12le|10be|12be)", pix_fmt))
    audio = [s.get("codec_name", "?") for s in streams if s.get("codec_type") == "audio"]
    return Probe(path, w, h, rate, duration, start, min(starts) if starts else 0.0,
                 v.get("codec_name", "?"), bit_rate, pix_fmt, matrix, prim, in_range, ten_bit, audio)


# --------------------------------------------------------------------------- decode

class Decoder:
    """Yields (index, pts_seconds, frame) with frame a writable uint8 HxWx3 RGB array.
    A background thread reads ahead so decoding overlaps the GPU work."""

    def __init__(self, p: Probe, hwaccel: bool = True, readahead: int = 6) -> None:
        self.p = p
        vf = (f"showinfo,scale=in_color_matrix={p.matrix}:in_range={p.in_range}"
              f":flags=bicubic+accurate_rnd+full_chroma_int,format=rgb24")
        cmd = [tool("ffmpeg"), "-nostdin", "-hide_banner", "-v", "info"]
        if hwaccel:
            cmd += ["-hwaccel", "auto"]
        cmd += ["-i", str(p.path), "-map", "0:v:0", "-vf", vf, "-fps_mode", "passthrough",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        pipe = big_pipe()
        if pipe is None:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self._out = self.proc.stdout
        else:
            r, w = pipe
            try:
                self.proc = subprocess.Popen(cmd, stdout=w, stderr=subprocess.PIPE, bufsize=0)
            except BaseException:
                os.close(r)
                raise
            finally:
                os.close(w)                      # the child has its own copy
            self._out = open(r, "rb", buffering=0)
        self._pts: list[float] = []
        self._errors: list[str] = []
        self._cv = threading.Condition()
        self._stderr_done = False
        self._q: queue.Queue = queue.Queue(maxsize=readahead)
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._read_frames, daemon=True).start()

    _PTS = re.compile(r"\bpts_time:\s*(-?[0-9.]+)")

    def _read_stderr(self) -> None:
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace")
            if "Parsed_showinfo" in line:
                m = self._PTS.search(line)
                if m:
                    with self._cv:
                        self._pts.append(float(m.group(1)))
                        self._cv.notify_all()
            elif "error" in line.lower() or "invalid" in line.lower():
                self._errors.append(line.strip())
        with self._cv:
            self._stderr_done = True
            self._cv.notify_all()

    def _pts_for(self, i: int) -> float:
        with self._cv:
            self._cv.wait_for(lambda: len(self._pts) > i or self._stderr_done, timeout=10)
            if len(self._pts) > i:
                return self._pts[i] - self.p.start
        return i / self.p.fps

    def _read_frames(self) -> None:
        n = self.p.width * self.p.height * 3
        i = 0
        try:
            while True:
                buf = bytearray(n)
                view, got = memoryview(buf), 0
                while got < n:
                    k = self._out.readinto(view[got:])
                    if not k:
                        break
                    got += k
                if got < n:
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(self.p.height, self.p.width, 3)
                self._q.put((i, self._pts_for(i), frame))
                i += 1
        except (OSError, ValueError):
            pass                                 # ffmpeg was stopped (close() kills it)
        finally:
            if self._out is not self.proc.stdout:
                self._out.close()                # our own big pipe; nothing else reads it
            self._q.put(None)

    def __iter__(self):
        while True:
            item = self._q.get()
            if item is None:
                break
            yield item
        code = self.proc.wait()
        if code != 0:
            msg = "; ".join(self._errors[-3:]) or f"ffmpeg exited with {code}"
            raise VideoError(f"Decoding failed: {msg}")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


# --------------------------------------------------------------------------- encode

_FAMILIES = {
    # codec -> hardware encoders to try in order, then the CPU encoder
    "hevc": ["hevc_nvenc", "hevc_amf", "hevc_qsv", "hevc_vaapi", "hevc_videotoolbox", "libx265"],
    "h264": ["h264_nvenc", "h264_amf", "h264_qsv", "h264_vaapi", "h264_videotoolbox", "libx264"],
    # prores_ks_vulkan (ffmpeg 8+) encodes on the GPU: ~5x faster than prores_ks at 1440p
    # on an RTX 5070 Ti, same ProRes 422 HQ stream (PSNR 63 dB between the two).
    "prores": ["prores_ks_vulkan", "prores_ks"],
}
_VAAPI_DEVICE = os.environ.get("FRAME_REPAIR_VAAPI_DEVICE", "/dev/dri/renderD128")
# Vulkan device for prores_ks_vulkan: an index or part of the name ("NVIDIA"); default: ffmpeg's pick.
_VULKAN_DEVICE = os.environ.get("FRAME_REPAIR_VULKAN_DEVICE", "")
_working: dict[str, bool] = {}


def _encoder_args(enc: str, bps: float) -> tuple[list[str], list[str], str]:
    """(global args before the inputs, encoder args, pixel format for the filter chain)."""
    b, mx, buf = f"{int(bps)}", f"{int(bps * 1.5)}", f"{int(bps * 2)}"
    if enc.endswith("_nvenc"):
        return [], ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-b:v", b, "-maxrate", mx, "-bufsize", buf], "yuv420p"
    if enc.endswith("_amf"):
        return [], ["-quality", "quality", "-rc", "vbr_peak", "-b:v", b, "-maxrate", mx], "yuv420p"
    if enc.endswith("_qsv"):
        return [], ["-preset", "slower", "-b:v", b, "-maxrate", mx], "nv12"
    if enc.endswith("_vaapi"):
        return ["-vaapi_device", _VAAPI_DEVICE], ["-b:v", b, "-maxrate", mx], "vaapi"
    if enc.endswith("_videotoolbox"):
        return [], ["-b:v", b, "-allow_sw", "0"], "yuv420p"
    if enc in ("libx265", "libx264"):
        extra = ["-x265-params", "log-level=error"] if enc == "libx265" else []
        return [], ["-preset", "medium", "-b:v", b, "-maxrate", mx, "-bufsize", buf] + extra, "yuv420p"
    if enc == "prores_ks_vulkan":
        dev = f"vulkan=vk:{_VULKAN_DEVICE}" if _VULKAN_DEVICE else "vulkan=vk"
        return ["-init_hw_device", dev, "-filter_hw_device", "vk"], ["-profile:v", "3", "-vendor", "apl0"], "vulkan"
    if enc == "prores_ks":
        return [], ["-profile:v", "3", "-vendor", "apl0"], "yuv422p10le"
    return [], ["-b:v", b], "yuv420p"


# Hardware-frame encoders: (software format to convert to, then hwupload)
_UPLOAD = {"vaapi": "nv12", "vulkan": "yuv422p10le"}


def _pix_chain(pix: str, matrix: str, primaries: str) -> str:
    """RGB -> the encoder's YUV format, video range, with the colour tags set on the
    frames themselves (recent ffmpeg takes them from the frames, not from -color_*)."""
    space = "smpte170m" if matrix == "bt601" else "bt709"
    chain = (f"scale=out_color_matrix={matrix}:out_range=tv:flags=bicubic+accurate_rnd+full_chroma_int,"
             f"format={_UPLOAD.get(pix, pix)},"
             f"setparams=range=tv:color_primaries={primaries}:color_trc=bt709:colorspace={space}")
    return chain + (",hwupload" if pix in _UPLOAD else "")


def encoder_works(enc: str) -> bool:
    """Encodes a short test clip; an encoder can be compiled in without the hardware."""
    if enc in _working:
        return _working[enc]
    if enc.endswith("_vaapi") and not (sys.platform.startswith("linux") and os.path.exists(_VAAPI_DEVICE)):
        _working[enc] = False
        return False
    if enc.endswith("_videotoolbox") and sys.platform != "darwin":
        _working[enc] = False
        return False
    pre, args, pix = _encoder_args(enc, 4e6)
    cmd = [tool("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", *pre,
           "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=30:d=0.2",
           "-vf", _pix_chain(pix, "bt709", "bt709"), "-c:v", enc, *args, "-f", "null", "-"]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    _working[enc] = ok
    return ok


def pick_encoder(codec: str, override: str | None = None) -> str:
    if override:
        return override
    for enc in _FAMILIES[codec]:
        if encoder_works(enc):
            return enc
    raise VideoError(f"No working {codec} encoder in this ffmpeg build.")


def rate_for(fps: float) -> Fraction:
    """Exact rate for whole and NTSC (x/1.001) frame rates."""
    if abs(fps - round(fps)) < 0.001:
        return Fraction(round(fps))
    if abs(fps * 1.001 - round(fps * 1.001)) < 0.01:
        return Fraction(round(fps * 1.001) * 1000, 1001)
    return Fraction(fps).limit_denominator(1001)


class Encoder:
    """Takes uint8 HxWx3 RGB frames at a constant rate and copies the source's audio.
    A background thread feeds ffmpeg so encoding overlaps the GPU work."""

    def __init__(self, p: Probe, out: Path, rate: Fraction, codec: str, bitrate_mbps: float | None,
                 encoder: str | None = None, queue_size: int = 6) -> None:
        self.encoder = pick_encoder(codec, encoder)
        bps = bitrate_mbps * 1e6 if bitrate_mbps else max(p.bit_rate * 1.25, 8e6)
        pre, args, pix = _encoder_args(self.encoder, bps)
        offset = max(0.0, p.start - p.container_start)
        r = f"{rate.numerator}/{rate.denominator}"

        ext = out.suffix.lower()
        audio_args = ["-c:a", "copy"]
        self.note = ""
        if ext in (".mp4", ".m4v") and any(a.startswith("pcm_") for a in p.audio):
            audio_args = ["-c:a", "aac", "-b:a", "320k"]
            self.note = "uncompressed audio can't go in .mp4, so it is converted to AAC 320 kb/s"
        colour = ["-colorspace", "smpte170m" if p.matrix == "bt601" else "bt709",
                  "-color_primaries", p.primaries, "-color_trc", "bt709", "-color_range", "tv"]
        hevc = self.encoder.startswith("hevc") or self.encoder == "libx265"
        tag = ["-tag:v", "hvc1"] if hevc and ext in (".mov", ".mp4", ".m4v") else []

        cmd = [tool("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-y", *pre,
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{p.width}x{p.height}", "-framerate", r]
        if offset > 0.0005:
            cmd += ["-itsoffset", f"{offset:.6f}"]
        cmd += ["-i", "pipe:0", "-i", str(p.path), "-map", "0:v:0", "-map", "1:a?",
                "-vf", _pix_chain(pix, p.matrix, p.primaries), "-c:v", self.encoder, *args, *tag, *colour,
                *audio_args, "-r", r]
        if ext in (".mov", ".mp4", ".m4v"):
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(out))
        pipe = big_pipe()
        if pipe is None:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self._in = self.proc.stdin
        else:
            r, w = pipe
            try:
                self.proc = subprocess.Popen(cmd, stdin=r, stderr=subprocess.PIPE, bufsize=0)
            except BaseException:
                os.close(w)
                raise
            finally:
                os.close(r)                      # the child has its own copy
            self._in = open(w, "wb", buffering=0)
        self._err = bytearray()
        threading.Thread(target=lambda: self._err.extend(self.proc.stderr.read()), daemon=True).start()
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._failed: BaseException | None = None
        self.frames = 0
        self._t = threading.Thread(target=self._write, daemon=True)
        self._t.start()

    def _write(self) -> None:
        try:
            while True:
                f = self._q.get()
                if f is None:
                    break
                view = memoryview(np.ascontiguousarray(f)).cast("B")
                while view:                        # unbuffered writes may be partial
                    view = view[self._in.write(view):]
        except (BrokenPipeError, OSError) as e:
            self._failed = e
            while self._q.get() is not None:      # drain so put() never blocks forever
                pass

    def write(self, frame: np.ndarray) -> None:
        if self._failed is not None:
            raise VideoError(f"Encoding failed ({self.encoder}): {self._error_text()}")
        self._q.put(frame)
        self.frames += 1

    def _error_text(self) -> str:
        lines = self._err.decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else "ffmpeg stopped"

    def close(self) -> None:
        self._q.put(None)
        self._t.join()
        try:
            self._in.close()
        except OSError:
            pass
        code = self.proc.wait()
        if code != 0 or self._failed is not None:
            raise VideoError(f"Encoding failed ({self.encoder}): {self._error_text()}")

    def abort(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        try:
            self._in.close()
        except OSError:
            pass
