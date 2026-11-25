"""Builds the Windows (.zip) and Linux (.tar.gz) downloads of the portable edition.

    python release/build.py OUT_DIR [--rife-metal PATH]

Both archives hold the same Python engine, the RIFE weights and an installer that
sets up PyTorch for the machine's graphics card. Archives are reproducible: fixed
timestamps and no owner names inside.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORTABLE = HERE.parent
sys.path.insert(0, str(PORTABLE))
from frame_repair import __version__  # noqa: E402
from frame_repair.model import WEIGHTS_SHA256  # noqa: E402

STAMP = (2026, 11, 25, 0, 0, 0)
EPOCH = 1795564800          # 2026-11-25 00:00:00 UTC

LAUNCHERS = [
    ("Analyze", "--dry-run",
     "shows the game's real frame rate and how many frames were repeated. Writes nothing."),
    ("Film look (24 fps)", "--preset film",
     '24 fps with a 180-degree shutter for motion blur, ProRes, saved as "clip [24fps 180deg].mov".'),
    ("Fix dropped frames", "--preset fix",
     'replaces only the repeated frames, saved as "clip [fixed].mov".'),
    ("Smooth 60 fps", "--preset smooth60",
     'even timing on a steady 60 fps grid, ProRes, saved as "clip [60fps].mov".'),
]

# Words that must never appear in a download (one regex per line), kept outside the
# repository: --forbid FILE, default ~/.config/frame-repair/forbidden-words.txt.
DEFAULT_FORBID = Path.home() / ".config" / "frame-repair" / "forbidden-words.txt"


def app_files(rife_metal: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for p in sorted((PORTABLE / "frame_repair").glob("*.py")):
        files[f"app/frame_repair/{p.name}"] = p.read_bytes()
    files["app/tools/setup_env.py"] = (PORTABLE / "tools" / "setup_env.py").read_bytes()
    w = (rife_metal / "Sources" / "RifeMetal" / "Resources" / "rife-v4.25.rmw").read_bytes()
    if hashlib.sha256(w).hexdigest() != WEIGHTS_SHA256:
        raise SystemExit("weights checksum mismatch")
    files["app/weights/rife-v4.25.rmw"] = w
    notices = ((rife_metal / "THIRD-PARTY.md").read_text().rstrip() + "\n\n"
               "## RifeMetal\n\nThe weight file is RifeMetal's `.rmw` conversion of the checkpoint above.\n\n"
               "- **Project**: https://github.com/cinemore/rife-metal\n- **License**: Apache-2.0\n\n"
               "```\n" + (rife_metal / "LICENSE").read_text().rstrip() + "\n```\n")
    files["THIRD-PARTY-NOTICES.md"] = notices.encode()
    return files


def windows_files() -> dict[str, bytes]:
    w = HERE / "windows"
    files = {name: (w / name).read_bytes() for name in ("Install.cmd", "rife-repair.cmd", "README.txt")}
    files["app/tools/env.cmd"] = (w / "env.cmd").read_bytes()
    template = (w / "launcher.cmd.in").read_text()
    for title, args, help_text in LAUNCHERS:
        files[f"{title}.cmd"] = (template.replace("@TITLE@", title).replace("@ARGS@", args)
                                 .replace("@HELP@", help_text)).encode()
    # Batch files need CRLF; so does Notepad on older Windows.
    return {k: (v.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") if k.endswith((".cmd", ".txt")) else v)
            for k, v in files.items()}


def linux_files() -> dict[str, bytes]:
    lx = HERE / "linux"
    return {name: (lx / name).read_bytes() for name in ("install.sh", "rife-repair", "README.txt")}


def check_clean(files: dict[str, bytes], forbid: Path) -> None:
    if not forbid.exists():
        print(f"note: {forbid} not found, skipping the forbidden-words check")
        return
    words = [l.strip() for l in forbid.read_text().splitlines() if l.strip() and not l.startswith("#")]
    pattern = re.compile("|".join(f"(?:{w})" for w in words), re.I)
    for name, data in files.items():
        if name.endswith(".rmw"):
            continue
        m = pattern.search(data.decode("utf-8", "replace")) or pattern.search(name)
        if m:
            raise SystemExit(f"refusing to build: {name} contains {m.group(0)!r}")


def write_zip(path: Path, root: str, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"{root}/{name}", date_time=STAMP)
            info.create_system = 0                    # MS-DOS: no Unix owner or mode bits
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name])


def write_tar(path: Path, root: str, files: dict[str, bytes], executable: set[str]) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as t:
        dirs = sorted({str(Path(root, n).parent) for n in files} | {root})
        for d in dirs:
            parts = Path(d).parts
            for i in range(1, len(parts) + 1):
                sub = "/".join(parts[:i])
                if sub in t.getnames():
                    continue
                ti = tarfile.TarInfo(sub)
                ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, EPOCH
                t.addfile(ti)
        for name in sorted(files):
            ti = tarfile.TarInfo(f"{root}/{name}")
            ti.size, ti.mtime = len(files[name]), EPOCH
            ti.mode = 0o755 if name in executable else 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            t.addfile(ti, io.BytesIO(files[name]))
    import gzip
    with open(path, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=EPOCH, compresslevel=9) as g:
        g.write(raw.getvalue())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--rife-metal", type=Path, default=PORTABLE.parent.parent / "rife-metal")
    ap.add_argument("--forbid", type=Path, default=DEFAULT_FORBID)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    common = app_files(a.rife_metal)

    win = {**common, **windows_files()}
    lin = {**common, **linux_files()}
    check_clean(win, a.forbid)
    check_clean(lin, a.forbid)

    wroot = f"FrameRepair-{__version__}-windows"
    lroot = f"FrameRepair-{__version__}-linux"
    wzip = a.out / f"{wroot}.zip"
    ltar = a.out / f"{lroot}.tar.gz"
    write_zip(wzip, wroot, win)
    write_tar(ltar, lroot, lin, executable={"install.sh", "rife-repair"})
    for p in (wzip, ltar):
        print(f"{p}  {p.stat().st_size:,} bytes  sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
