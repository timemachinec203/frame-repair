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

## Install

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

## Tested so far

- Apple GPU (MPS) and the CPU path, on macOS: output matches the Mac engine, and the CPU
  and GPU paths give identical frames. Fill, retime, frame-rate change and film look were
  checked against ground truth on a simulated 45-in-60 fps recording.
- Not yet run on Nvidia, AMD or Intel GPUs, or on Windows or Linux. Those code paths
  are standard PyTorch and ffmpeg, but treat the first runs there as a test.

## Tests

```bash
python -m unittest discover -s tests
```
