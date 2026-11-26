# Frame Repair for Windows and Linux

The same engine as the Mac app, as a command-line tool (`rife-repair`) written in Python
and PyTorch. It runs on:

| Hardware | How |
|---|---|
| Nvidia GPUs | CUDA, fp16. Hardware decode (NVDEC) and encode (NVENC). |
| AMD GPUs | ROCm on Linux, fp16. Encode with AMF (Windows) or VAAPI (Linux). |
| Intel GPUs (Arc, Core Ultra) | PyTorch XPU, fp16. Encode with Quick Sync. |
| Intel and AMD CPUs | Any x86-64 CPU, fp32. Works everywhere, much slower than a GPU. |
| Apple Silicon | Works (MPS), but the Mac app is faster there. |

Same flags, same output files and the same results as the Mac engine. Frame detection
and the retime plan are line-for-line ports, and the RIFE model matches the official
reference frame at 51.7 dB.

## Download (easiest)

From **Releases**:

- **Windows**: `FrameRepair-<version>-windows.zip`. Unzip, double-click **Install** (it gets
  Python, ffmpeg and the right PyTorch for your graphics card, and puts **Frame Repair** in
  the Start menu, on the desktop and in the folder). Open the app, or drag clips onto
  **Analyze**, **Film look (24 fps)**, **Fix dropped frames** or **Smooth 60 fps**.
- **Linux**: `FrameRepair-<version>-linux.tar.gz`. Unpack, run `./install.sh`, then
  `./frame-repair` for the app or `./rife-repair clip.mov --preset film`.

## The app

The same window as the Mac app ([frame_repair/gui.py](frame_repair/gui.py), Qt via PySide6):

1. Drop clips (or a folder) onto the window. Nothing starts yet.
2. Select clips, click **Analyze**: the game's real frame rate and how many frames repeat.
3. Pick a look (**Film look**, **Fix dropped frames only**, **Smooth 60 fps**, or **Manual**
   for every control), then **Process**.

Results are saved next to the original (or in a chosen folder) with the Mac app's names.
Two clips run at once by default: 14% faster than one after another on an RTX 5070 Ti.
Right-click a clip to open or show the original or the result, see the log, or process it
again. **Stop** ends the engine and its ffmpeg processes and deletes unfinished files.

Each clip runs the command-line engine as a hidden child process (`--machine-progress`);
the app's settings logic is [frame_repair/appkit.py](frame_repair/appkit.py), a port of the
Mac app's FrameRepairKit with the same tests. From source: `pip install -e ".[app]"`, then
`frame-repair` (or `python -m frame_repair.gui`). End-to-end check without clicking:
`FRAME_REPAIR_SELFTEST="a.mkv|b.mkv" FRAME_REPAIR_SELFTEST_OUT=out python -m frame_repair.gui`.

Nothing is compiled, so the same download works on any x86-64 or ARM machine. The
installer picks CUDA, ROCm, XPU or CPU PyTorch from the graphics card it finds (force
one with `--gpu nvidia|amd|intel|cpu`).

## Install from source

1. **Python 3.9 or newer**: python.org, or `winget install Python.Python.3.12` on Windows.
2. **ffmpeg** on your PATH:
   Windows `winget install ffmpeg`; Ubuntu/Debian `sudo apt install ffmpeg`; Fedora `sudo dnf install ffmpeg`.
3. **A virtual environment** in this folder:

   ```bash
   python -m venv .venv
   ```

   Activate it: Windows `.venv\Scripts\activate`; Linux `source .venv/bin/activate`.

4. **PyTorch for your hardware.** The exact command for your system is on
   [pytorch.org/get-started](https://pytorch.org/get-started/locally/). Typical ones:

   | Hardware | Command |
   |---|---|
   | Nvidia (RTX 50 series needs CUDA 12.8 or newer) | `pip install torch --index-url https://download.pytorch.org/whl/cu128` |
   | AMD on Linux | the ROCm command from the PyTorch site |
   | Intel GPU | `pip install torch --index-url https://download.pytorch.org/whl/xpu` |
   | CPU only | `pip install torch --index-url https://download.pytorch.org/whl/cpu` |

   AMD on Windows: use AMD's ROCm build of PyTorch if your card supports it, otherwise
   the CPU build. Video encoding still uses the GPU through AMF either way.

5. **Frame Repair itself**:

   ```bash
   pip install -e .
   ```

The RIFE weights (11 MB) download on first use, checksum-verified, to
`~/.cache/frame-repair` (Windows: `C:\Users\<you>\.cache\frame-repair`). To use a copy
you already have, pass `-m file.rmw` or set `FRAME_REPAIR_WEIGHTS`.

## Use

```bash
rife-repair clip.mov --dry-run                       # analyse only
rife-repair clip.mov                                 # fix repeated frames
rife-repair clip.mov --retime                        # even out timing
rife-repair clip.mov --fps 60                        # change frame rate
rife-repair clip.mov --fps 24 --shutter 180 --codec prores   # film look
```

Presets match the Mac app, including its file names (saved next to the input, never
overwriting):

| Preset | Does | Saved as |
|---|---|---|
| `--preset film` | 24 fps, 180° shutter motion blur, ProRes | `clip [24fps 180deg].mov` |
| `--preset fix` | replaces only the repeated frames | `clip [fixed].mov` |
| `--preset smooth60` | even timing on a 60 fps grid, ProRes | `clip [60fps].mov` |

The startup lines say which GPU and video encoder it picked:

```
Using:  Nvidia CUDA: <your GPU> fp16, encoder hevc_nvenc
```

Hardware options:

| Option | |
|---|---|
| `--device` | `auto` (default), `cuda`, `cuda:1`, `xpu`, `mps` or `cpu`. AMD GPUs show up as `cuda`. |
| `--precision` | `auto` (fp16 on GPUs, fp32 on CPU and Apple), `fp16`, `fp32`. |
| `--batch N` | In-between frames per GPU call (default 8). Lower it if the GPU runs out of memory. |
| `--encoder` | Force an ffmpeg encoder, e.g. `hevc_nvenc`, `hevc_amf`, `hevc_qsv`, `hevc_vaapi`, `libx265`. |
| `--no-hwaccel` | Decode on the CPU. |

All other options are the same as on the Mac (`rife-repair --help`). `RIFE_REPAIR_TIMING=1`
prints how long each stage took.

## Notes

- Without `--codec`, a ProRes source gives ProRes and anything else gives HEVC (hardware
  encoder if one works, else libx265). ProRes output needs a `.mov` file name.
- Audio is copied untouched. The one exception is uncompressed audio going into an
  `.mp4`, which is converted to AAC 320 kb/s because .mp4 can't hold it.
- The source colour standard (BT.601 or BT.709) is kept. HDR and 10-bit sources are
  processed in 8-bit SDR, as on the Mac.
- Output is constant frame rate. A variable-rate source gets its average rate.
- For a VAAPI device other than `/dev/dri/renderD128`, set `FRAME_REPAIR_VAAPI_DEVICE`.
- ProRes is encoded on the GPU with `prores_ks_vulkan` (ffmpeg 8 or newer) when it works,
  else on the CPU with `prores_ks`; force one with `--encoder`. Both write ProRes 422 HQ.
  To pick the Vulkan GPU, set `FRAME_REPAIR_VULKAN_DEVICE` to its index or part of its name.
- cuDNN autotuning (a few seconds up front, ~7% faster RIFE after) is only turned on for
  jobs with 2000 or more RIFE frames.

## Tested so far

- Apple GPU (MPS) and the CPU path, on macOS: output matches the Mac engine, and the CPU
  and GPU paths give identical frames. Fill, retime, frame-rate change and film look were
  checked against ground truth on a simulated 45-in-60 fps recording.
- Windows 11 with an Nvidia RTX 5070 Ti (Ryzen 7 9800X3D, driver 621, PyTorch 2.14 cu130,
  ffmpeg 9.1): the Windows download's installer and all four drag-and-drop launchers,
  on 1440p60 OBS recordings. Times for an 8.8 s clip (525 frames):

  | | Analyze | Fix dropped frames | Film look | Smooth 60 fps |
  |---|---|---|---|---|
  | Before tuning | 5.5 s | 9.5 s | 5.0 + 17.7 s | 5.0 + 20.4 s |
  | Now | 3.5 s | 6.3 s | 3.2 + 12.9 s | 3.2 + 7.4 s |

  (Retime presets: pass 1 scan + rendering.) The RIFE changes give bit-identical frames
  to the earlier code; differences between runs (~72 dB) come from cuDNN, as before.
- Not yet run on AMD or Intel GPUs, or on Linux. Those code paths are standard PyTorch
  and ffmpeg, but treat the first runs there as a test.

## Tests and release builds

```bash
python -m unittest discover -s tests
python release/build.py OUT_DIR       # Windows .zip + Linux .tar.gz (reproducible)
```
