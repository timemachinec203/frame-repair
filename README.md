# Frame Repair

Fixes gameplay recordings where the game dropped frames, and can turn them into a
24 fps film look with real motion blur. Runs entirely on your own computer; nothing
is uploaded anywhere.

When a game renders slower than the recorder (for example a game at 35-55 fps captured
by OBS at 60 fps), the recorder repeats the previous frame. That shows up as judder.
Frame Repair finds those repeated frames and rebuilds them with
[RIFE](https://github.com/hzwer/Practical-RIFE) frame interpolation.

It comes as a Mac app (**Frame Repair**) with a command-line engine (`rife-repair`), and
as a command-line edition for **Windows and Linux** (Nvidia, AMD and Intel GPUs, or any
Intel or AMD CPU): see [portable/README.md](portable/README.md).

## What it can do

| | |
|---|---|
| **Fix dropped frames** | Replace only the repeated frames; everything else stays as recorded. |
| **Even out timing** | Estimate when each game frame was really drawn and rebuild every frame on a steady grid. For games running below the recording rate. |
| **Change frame rate** | Any rate, e.g. 30 to 60 fps, or 59.94. Length and audio are unchanged. |
| **Film look** | 24 fps with a camera shutter (default 180°): each frame averages several RIFE-generated moments across the exposure, so motion smears like a film camera. |

Also: long runs of repeats (pauses, loading screens) are left alone, the source colour
standard (BT.601 / BT.709) is kept, audio is copied untouched, and originals are never
modified.

## Install (no Xcode needed)

1. Download `FrameRepair-<version>-macOS-universal.zip` from **Releases**.
2. Unzip it and drag **Frame Repair** into **Applications**.
3. Open it. macOS says it can't verify the developer, because the app is not notarized
   by Apple. Click **Done**, then open **System Settings > Privacy & Security**, scroll
   to "Frame Repair was blocked", click **Open Anyway** and confirm. Only needed once.

   Or, in Terminal: `xattr -dr com.apple.quarantine "/Applications/Frame Repair.app"`

Windows and Linux: download `FrameRepair-<version>-windows.zip` or `-linux.tar.gz` and
run its installer; see [portable/README.md](portable/README.md).

Runs on macOS 14 or later, on Apple Silicon and Intel Macs (one Universal app).
Everything it needs is inside: the engine, the RIFE model and the GPU shaders.
The Intel build has been tested under Rosetta, not yet on a real Intel Mac.

## The app

1. Drop clips (or a folder) onto the window. Nothing starts yet.
2. Select clips, click **Analyze**: shows the game's real frame rate and how many frames were repeated.
3. Pick a **Preset** (Film look, Fix dropped frames only, Smooth 60 fps) or **Manual**, then click **Process**.

Results are saved next to the original as `clip [24fps 180deg].mov` (or in a chosen
folder). Up to 3 clips can run at once; 2 was about 17% faster than 1 on the test Mac.
Right-click a clip to show or open the original or the output.

## Command line

```bash
rife-repair clip.mov --dry-run                       # analyse only
rife-repair clip.mov                                 # fix repeated frames
rife-repair clip.mov --retime                        # even out timing
rife-repair clip.mov --fps 60                        # change frame rate
rife-repair clip.mov --fps 24 --shutter 180 --codec prores   # film look
```

Other options: `--tier hq|balanced|fast`, `--codec auto|hevc|h264|prores`,
`--threshold`, `--max-gap`, `--samples`, `-o`, `-y`. See `rife-repair --help`.

## Build

Requirements: Mac (Apple Silicon or Intel), macOS 14+, Xcode 15.3+ with the Metal toolchain.
Xcode 26 and later ship the Metal toolchain separately:

```bash
xcodebuild -downloadComponent MetalToolchain
```

Then:

```bash
swift build -c release            # engine + app binaries
swift test                        # app logic tests
tools/make-app.sh                 # builds "Frame Repair.app", signs it for this Mac, installs to /Applications
tools/make-app.sh --no-install --universal   # Apple Silicon + Intel build for sharing (build/Frame Repair.app)
```

`make-app.sh` signs ad hoc, so the app runs on the Mac that built it. Another Mac needs
its own build (or a Developer ID signature).

End-to-end check without clicking (adds the clips, analyses, processes with the film
look, prints the result):

```bash
FRAME_REPAIR_SELFTEST="/path/a.mov|/path/b.mov" FRAME_REPAIR_SELFTEST_OUT=/tmp/out \
  "/Applications/Frame Repair.app/Contents/MacOS/FrameRepair"
```

## Layout

```
Sources/rife-repair/      engine: repeat detection, retime plan, RIFE, motion blur, encode
Sources/FrameRepairKit/   app logic without UI: settings -> arguments, presets, output parsing
Sources/FrameRepair/      SwiftUI app; runs the bundled engine as a helper process
Tests/                    tests for FrameRepairKit
portable/                 Windows / Linux edition: same engine in Python + PyTorch + ffmpeg
tools/                    app packaging and icon
```

## Speed (Apple Silicon test Mac)

Film look on a 3.8 s 3440x1440 clip (91 output frames):

| Setting | Time |
|---|---|
| Balanced, 5 moments per frame (default) | 30.8 s |
| Fast, 10 moments per frame (smoother streaks) | 20.5 s |
| Fast, 5 moments per frame | 11.1 s |

- RIFE is ~98% of the time; decode, repeat detection, blur blending and encode add about 1 s.
- RIFE runs as a stream: each real frame is encoded once and every moment between a pair
  renders in one GPU submission (12% faster than separate calls, identical output).
- Two clips at once is still ~12% faster than one after another (the GPU has gaps
  between submissions), so the app runs two by default.
- `RIFE_REPAIR_TIMING=1 rife-repair ...` prints the per-stage breakdown.

## Credits

- [RifeMetal](https://github.com/cinemore/rife-metal) (Apache-2.0): Metal port of RIFE,
  pulled in as a Swift package pinned to a tested commit.
- [Practical-RIFE](https://github.com/hzwer/Practical-RIFE) v4.25 weights (MIT), bundled by RifeMetal.
