"""Second half of install.cmd / install.sh, run with the new virtual environment's
Python: finds the graphics card, installs the matching PyTorch build and the app's
window toolkit (PySide6), finds (or on Windows offers to install) ffmpeg, writes the
settings the launchers read, makes the app's shortcuts (Windows), and checks that RIFE
runs on the chosen device.

    python setup_env.py [--gpu auto|nvidia|amd|intel|cpu] [--dry-run]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent          # the folder holding frame_repair/
WEIGHTS = APP / "weights" / "rife-v4.25.rmw"
TORCH = "https://download.pytorch.org/whl/"
WINDOWS = sys.platform == "win32"

# Newest first. Each is only used if the driver supports it and the index exists.
CUDA_BUILDS = ["cu130", "cu129", "cu128", "cu126"]      # RTX 50 series needs cu128+
ROCM_BUILDS = ["rocm7.2", "rocm7.1", "rocm7.0", "rocm6.4", "rocm6.3"]


def run(cmd: list[str], dry: bool, check: bool = True) -> int:
    print("  > " + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    if dry:
        return 0
    code = subprocess.call(cmd)
    if check and code != 0:
        raise SystemExit(f"\nThat step failed (exit {code}). See the messages above.")
    return code


def output(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def index_exists(build: str) -> bool:
    try:
        req = urllib.request.Request(f"{TORCH}{build}/torch/", method="HEAD",
                                     headers={"User-Agent": "frame-repair-setup"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- GPU

def gpu_names() -> list[str]:
    if WINDOWS:
        text = output(["powershell", "-NoProfile", "-Command",
                       "(Get-CimInstance Win32_VideoController).Name"])
    else:
        text = "\n".join(l for l in output(["lspci"]).splitlines()
                         if re.search(r"VGA|3D|Display", l))
    return [l.strip() for l in text.splitlines() if l.strip()]


def detect_gpu(names: list[str]) -> str:
    if shutil.which("nvidia-smi") or any("nvidia" in n.lower() for n in names):
        return "nvidia"
    if any(re.search(r"\b(amd|ati|radeon)\b", n.lower()) for n in names):
        return "amd"
    if any("arc" in n.lower() and "intel" in n.lower() for n in names):
        return "intel"
    return "cpu"


def nvidia_driver_cuda() -> tuple[int, int] | None:
    """Highest CUDA version the installed driver supports (from nvidia-smi). Older
    drivers print "CUDA Version: 12.8", newer ones "CUDA UMD Version: 13.5"."""
    m = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", output(["nvidia-smi"]))
    return (int(m.group(1)), int(m.group(2))) if m else None


def torch_build(gpu: str, dry: bool, driver: tuple[int, int] | None) -> tuple[str, str]:
    """(PyTorch index name, note for the user)."""
    if gpu == "nvidia":
        usable = [b for b in CUDA_BUILDS
                  if driver is None or (int(b[2:4]), int(b[4:])) <= driver]
        for b in usable:
            if dry or index_exists(b):
                return b, ""
        return "cu128", ("Your Nvidia driver looks old; if the check at the end fails, "
                         "update it from nvidia.com and run the installer again.")
    if gpu == "amd":
        if WINDOWS:
            return "cpu", ("AMD on Windows: RIFE runs on the CPU (PyTorch's AMD support on Windows "
                           "depends on the card). Video encoding still uses the GPU.")
        for b in ROCM_BUILDS:
            if dry or index_exists(b):
                return b, "AMD: needs ROCm support for your card (see AMD's ROCm compatibility list)."
        return "cpu", "No PyTorch ROCm build found; using the CPU build."
    if gpu == "intel":
        return "xpu", "Intel Arc: needs a current Intel graphics driver."
    return "cpu", "No supported graphics card found: RIFE runs on the CPU (works, but slowly)."


# --------------------------------------------------------------------------- ffmpeg

def find_ffmpeg() -> tuple[str, str] | None:
    def pair(ff: str | None):
        if not ff:
            return None
        probe = str(Path(ff).with_name("ffprobe" + (".exe" if WINDOWS else "")))
        return (ff, probe) if Path(probe).exists() else None

    found = pair(shutil.which("ffmpeg"))
    if found or not WINDOWS:
        return found
    # winget puts portable packages here; the PATH change only reaches new windows.
    local = os.environ.get("LOCALAPPDATA", "")
    for cand in [Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
                 *map(Path, glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                                   "*FFmpeg*", "**", "bin", "ffmpeg.exe"),
                                      recursive=True))]:
        found = pair(str(cand)) if cand.exists() else None
        if found:
            return found
    return None


def ensure_ffmpeg(dry: bool) -> tuple[str, str] | None:
    found = find_ffmpeg()
    if found:
        print(f"  ffmpeg: {found[0]}")
        return found
    if not WINDOWS:
        print("  ffmpeg is missing. Install it, then run ./install.sh again:\n"
              "    Ubuntu/Debian: sudo apt install ffmpeg\n"
              "    Fedora:        sudo dnf install ffmpeg   (from RPM Fusion)\n"
              "    Arch:          sudo pacman -S ffmpeg")
        return None
    if not shutil.which("winget"):
        print("  ffmpeg is missing and winget isn't available. Install ffmpeg from "
              "https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH.")
        return None
    answer = "y" if dry else input("  ffmpeg is missing. Install it now with winget? [Y/n] ").strip().lower()
    if answer not in ("", "y", "yes"):
        return None
    run(["winget", "install", "-e", "--id", "Gyan.FFmpeg", "--accept-source-agreements",
         "--accept-package-agreements"], dry, check=False)
    found = find_ffmpeg()
    if found:
        print(f"  ffmpeg: {found[0]}")
    return found


def write_settings(ff: tuple[str, str] | None) -> None:
    if WINDOWS:
        lines = ["@rem Written by the installer."]
        if ff:
            lines += [f'set "FFMPEG={ff[0]}"', f'set "FFPROBE={ff[1]}"']
        (APP / "settings.cmd").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        lines = ["# Written by install.sh."]
        if ff:
            lines += [f"export FFMPEG='{ff[0]}'", f"export FFPROBE='{ff[1]}'"]
        (APP / "settings.sh").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- app shortcuts

def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def make_shortcuts(dry: bool) -> bool:
    """Windows: "Frame Repair" in the Start menu, on the desktop and in this folder,
    opening the app (pythonw, so no console window) with its own icon."""
    py = Path(sys.executable)
    pythonw = py.with_name("pythonw.exe")
    ico = APP / "frame-repair.ico"
    env = dict(os.environ, PYTHONPATH=str(APP))
    if not dry and subprocess.call([str(py), "-m", "frame_repair.gui", "--write-icon", str(ico)], env=env) != 0:
        print("  Could not draw the app icon; the shortcuts use Python's.")
    # Folders resolved by PowerShell itself: the desktop may be redirected (OneDrive)
    # or localised, e.g. OneDrive\桌面.
    folders = ["([Environment]::GetFolderPath('Programs'))", "([Environment]::GetFolderPath('Desktop'))",
               _ps_quote(str(APP.parent))]
    # WScript.Shell saves through the ANSI code page, so a folder like 桌面 fails: save
    # to a temporary file, then move it into place (PowerShell handles any name).
    script = "$ErrorActionPreference = 'Stop'\n$ws = New-Object -ComObject WScript.Shell\n"
    for i, f in enumerate(folders):
        script += (f"$tmp = Join-Path ([IO.Path]::GetTempPath()) 'frame-repair-{i}.lnk'\n"
                   "$l = $ws.CreateShortcut($tmp)\n"
                   f"$l.TargetPath = {_ps_quote(str(pythonw))}\n"
                   "$l.Arguments = '-m frame_repair.gui'\n"
                   f"$l.WorkingDirectory = {_ps_quote(str(APP))}\n"
                   f"$l.IconLocation = {_ps_quote(str(ico))}\n"
                   "$l.Description = 'Fix dropped frames in gameplay recordings'\n"
                   "$l.Save()\n"
                   f"Move-Item -LiteralPath $tmp -Destination (Join-Path {f} 'Frame Repair.lnk') -Force\n")
    print("  Start menu, desktop and this folder: \"Frame Repair\"")
    if dry:
        return True
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]) == 0


# --------------------------------------------------------------------------- check

CHECK = r"""
import numpy as np, torch
from frame_repair.model import Interpolator, device_label, pick_device
d = pick_device()
rife = Interpolator(d, weights=r"%s")
a = np.random.default_rng(0).integers(0, 255, (144, 256, 3), dtype=np.uint8)
out = rife.interpolate(0, a, 1, a.copy(), [0.5]).cpu().numpy()
assert out.shape == (1, 144, 256, 3)
print("  RIFE runs on: " + device_label(d))
"""


def main() -> None:
    # Our lines must reach the window before the output of the commands we start.
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", choices=["auto", "nvidia", "amd", "intel", "cpu"], default="auto")
    ap.add_argument("--dry-run", action="store_true", help="Print the steps without running them.")
    a = ap.parse_args()

    print("\n[1/5] Graphics card")
    names = gpu_names()
    for n in names:
        print(f"  found: {n}")
    gpu = detect_gpu(names) if a.gpu == "auto" else a.gpu
    driver = nvidia_driver_cuda() if gpu == "nvidia" else None
    build, note = torch_build(gpu, a.dry_run, driver)
    print(f"  using: {gpu}" + (f" (driver supports CUDA {driver[0]}.{driver[1]})" if driver else "")
          + f" -> PyTorch build '{build}'")
    if note:
        print(f"  note:  {note}")

    print("\n[2/5] PyTorch, NumPy and the app's window toolkit (a large download for GPU builds; a few minutes)")
    py = sys.executable
    run([py, "-m", "pip", "install", "--upgrade", "pip"], a.dry_run, check=False)
    run([py, "-m", "pip", "install", "--upgrade", "torch", "--index-url", TORCH + build], a.dry_run)
    run([py, "-m", "pip", "install", "--upgrade", "numpy"], a.dry_run)
    # The app only; the command line and the launchers work without it.
    gui = run([py, "-m", "pip", "install", "--upgrade", "PySide6-Essentials"], a.dry_run, check=False) == 0

    print("\n[3/5] ffmpeg")
    ff = ensure_ffmpeg(a.dry_run)
    if not a.dry_run:
        write_settings(ff)

    print("\n[4/5] The Frame Repair app")
    if not gui:
        print("  PySide6 didn't install, so the app is unavailable; the drag-and-drop launchers still work.")
    elif WINDOWS:
        if not make_shortcuts(a.dry_run):
            print("  Making the shortcuts failed; start the app with: app\\.venv\\Scripts\\pythonw -m frame_repair.gui")
    else:
        print("  Start it with ./frame-repair (on some systems Qt also needs: sudo apt install libxcb-cursor0)")

    print("\n[5/5] Checking that RIFE runs")
    env = dict(os.environ, PYTHONPATH=str(APP))
    code = 0 if a.dry_run else subprocess.call([py, "-c", CHECK % WEIGHTS], env=env)
    if code != 0:
        raise SystemExit("\nRIFE failed to run. If you have an Nvidia card, update its driver "
                         "and run the installer again; or rerun with --gpu cpu.")
    if ff is None:
        raise SystemExit("\nAlmost done: install ffmpeg (see above), then run the installer again.")
    print("\nReady.")


if __name__ == "__main__":
    main()
