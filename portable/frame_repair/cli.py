"""Command line: the same flags as the Mac engine, plus --device / --precision / --encoder."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .engine import Engine, Options, RepairError
from .video import VideoError

DESCRIPTION = """\
Replace duplicated (dropped) frames in a recording with RIFE-interpolated frames.

A game that drops a frame leaves the recorder showing the previous frame again. This
finds those repeated frames and replaces them with frames generated between the last
real frame and the next one. Timestamps, frame count and audio are unchanged. Run with
--dry-run first to see what it finds.
"""

EXAMPLES = """\
examples:
  rife-repair clip.mov --dry-run                       analyse only
  rife-repair clip.mov                                 fix repeated frames
  rife-repair clip.mov --retime                        even out timing
  rife-repair clip.mov --fps 60                        change frame rate
  rife-repair clip.mov --fps 24 --shutter 180 --codec prores   film look
"""


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rife-repair", description=DESCRIPTION, epilog=EXAMPLES,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Input video (.mov / .mp4 / .mkv).")
    ap.add_argument("-o", "--output", help="Output file. Default: <input>-repaired.<ext>.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Only analyse: list duplicate runs and the difference histogram. Writes nothing.")
    ap.add_argument("--threshold", type=float, default=1.5,
                    help="A frame counts as a duplicate when its worst-cell difference from the last "
                         "real frame is below this (0-255 scale). Default 1.5.")
    ap.add_argument("--max-gap", type=int, default=4,
                    help="Longest run of duplicates to repair. Longer runs are real freezes (pause, "
                         "loading) and are left alone. Default 4.")
    ap.add_argument("--tier", choices=["hq", "balanced", "fast"],
                    help="RIFE quality. Default: hq, or balanced with --shutter (blur hides the "
                         "difference and it is ~4x faster).")
    ap.add_argument("--shutter", type=float, metavar="DEG",
                    help="Add camera motion blur: shutter angle in degrees (180 = classic film look). "
                         "Each output frame averages several RIFE-generated moments across the "
                         "exposure. Use with --fps, e.g. --fps 24 --shutter 180.")
    ap.add_argument("--samples", type=int,
                    help="Moments averaged per output frame with --shutter. Default: one per 1/240 s "
                         "of exposure (5 for 180 degrees at 24 fps).")
    ap.add_argument("--codec", choices=["auto", "hevc", "h264", "prores"], default="auto",
                    help="Output codec: auto (ProRes if the source is ProRes, else HEVC). Default auto.")
    ap.add_argument("--bitrate", type=float, metavar="MBPS",
                    help="Video bitrate in Mbps for hevc/h264. Default: 1.25x the source.")
    ap.add_argument("-m", "--model", help="Path to a .rmw weight file. Default: downloaded once "
                                          "(checksum-verified) to the user cache folder.")
    ap.add_argument("-y", "--overwrite", action="store_true", help="Overwrite the output file if it exists.")
    ap.add_argument("--retime", action="store_true",
                    help="For games running below / unsteady vs the recording rate: estimate when each "
                         "real game frame was rendered and rebuild EVERY output frame on an even grid. "
                         "Slower (most frames go through RIFE).")
    ap.add_argument("--smooth", type=int, default=3,
                    help="Retime smoothing: real frames on each side used to estimate render timing.")
    ap.add_argument("--fps", type=float,
                    help="Output frame rate, e.g. 60 or 59.94. Uses the retime engine: repeats are "
                         "removed, timing is evened out, and every output frame is placed on an even "
                         "grid at this rate. Length and audio are unchanged.")
    ap.add_argument("--machine-progress", action="store_true", help=argparse.SUPPRESS)

    g = ap.add_argument_group("hardware")
    g.add_argument("--device", default="auto",
                   help="auto | cuda | cuda:1 | xpu | mps | cpu. auto picks an Nvidia or AMD (ROCm) GPU, "
                        "then an Intel GPU, then Apple, then the CPU.")
    g.add_argument("--precision", choices=["auto", "fp16", "fp32"], default="auto",
                   help="auto = fp16 on Nvidia/AMD/Intel GPUs, fp32 elsewhere.")
    g.add_argument("--batch", type=int, default=8,
                   help="Most in-between frames per GPU call. Lower it if the GPU runs out of memory.")
    g.add_argument("--encoder", help="Force an ffmpeg video encoder, e.g. hevc_nvenc, hevc_amf, "
                                     "hevc_qsv, hevc_vaapi, libx265. Default: first one that works.")
    g.add_argument("--no-hwaccel", dest="hwaccel", action="store_false",
                   help="Decode on the CPU instead of ffmpeg's -hwaccel auto.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__} (portable)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True, errors="replace")   # lines reach a parent app live
    except (AttributeError, ValueError):
        pass
    try:
        Engine(Options(**vars(args))).run()
    except (RepairError, VideoError, RuntimeError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
