Frame Repair for Windows
========================

Fixes gameplay recordings where the game dropped frames (the recorder repeats the
last frame, which looks like judder), and can turn them into a 24 fps film look
with real motion blur. Everything runs on your own PC.

Setup (once)
  1. Unzip this folder somewhere permanent, e.g. Documents\FrameRepair.
  2. Double-click "Install". It finds your graphics card and downloads the matching
     PyTorch (a few GB for Nvidia), plus Python and ffmpeg if they're missing.
     If Windows SmartScreen warns about it, click "More info" > "Run anyway".

The app
  Open "Frame Repair" from the Start menu, the desktop or this folder (Install
  makes the shortcuts). Drop clips or a folder onto the window, select them, click
  Analyze (shows the game's real frame rate and the repeated frames), pick a look
  (Film look, Fix dropped frames only, Smooth 60 fps, or Manual), then Process.
  Two clips are processed at once by default. Right-click a clip to open or show
  the result; Stop deletes unfinished files.

Without the app
  Drag one or more clips onto one of these:
    Analyze               shows the game's real frame rate and how many frames repeat
    Film look (24 fps)    24 fps, 180-degree shutter motion blur, ProRes
                          -> "clip [24fps 180deg].mov"
    Fix dropped frames    replaces only the repeated frames -> "clip [fixed].mov"
    Smooth 60 fps         even timing on a steady 60 fps grid, ProRes -> "clip [60fps].mov"
  Results are saved next to the original. Originals are never changed.

Command line (more options): open a terminal in this folder and run
  rife-repair clip.mov --help

Graphics cards
  Nvidia      CUDA (fastest). Keep the driver up to date.
  Intel Arc   XPU.
  AMD         RIFE runs on the CPU on Windows; video encoding still uses the GPU.
  None        CPU only: works, but slowly.
  To force the CPU build: run "Install.cmd --gpu cpu" from a terminal.

Credits: RIFE (Practical-RIFE v4.25, MIT) and RifeMetal's converted weights
(Apache-2.0); see THIRD-PARTY-NOTICES.md.
