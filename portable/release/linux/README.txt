Frame Repair for Linux
======================

Fixes gameplay recordings where the game dropped frames (the recorder repeats the
last frame, which looks like judder), and can turn them into a 24 fps film look
with real motion blur. Everything runs on your own machine.

Setup (once)
  Needs Python 3.9+ with venv, and ffmpeg:
    Ubuntu/Debian:  sudo apt install python3 python3-venv ffmpeg
    Fedora:         sudo dnf install python3 ffmpeg     (ffmpeg from RPM Fusion)
    Arch:           sudo pacman -S python ffmpeg
  Then, in this folder:
    ./install.sh
  It finds your graphics card and installs the matching PyTorch build (CUDA for
  Nvidia, ROCm for AMD, XPU for Intel Arc, or CPU). Force one with --gpu nvidia|amd|intel|cpu.

The app
  ./frame-repair                            opens the window: drop clips, Analyze, pick a look, Process
  (Qt may need: sudo apt install libxcb-cursor0. Not yet tested on Linux.)

Command line
  ./rife-repair clip.mov --dry-run          analyse: real game frame rate, repeated frames
  ./rife-repair clip.mov --preset film      24 fps, 180-degree shutter, ProRes -> "clip [24fps 180deg].mov"
  ./rife-repair clip.mov --preset fix       replace repeated frames only     -> "clip [fixed].mov"
  ./rife-repair clip.mov --preset smooth60  even 60 fps timing, ProRes        -> "clip [60fps].mov"
  ./rife-repair --help                      all options
  Results are saved next to the original. Originals are never changed.
  To run it from anywhere: ln -s "$PWD/rife-repair" ~/.local/bin/rife-repair

Graphics cards
  Nvidia: CUDA (keep the driver current). AMD: ROCm, if your card is supported.
  Intel Arc: XPU. Video encoding uses NVENC or VAAPI when available.

Credits: RIFE (Practical-RIFE v4.25, MIT) and RifeMetal's converted weights
(Apache-2.0); see THIRD-PARTY-NOTICES.md.
