"""RIFE v4.25 (Practical-RIFE IFNet_HDv3) in PyTorch, loading RifeMetal's .rmw weights.

Runs on Nvidia (CUDA), AMD (ROCm builds of PyTorch expose the GPU as "cuda"),
Intel GPUs (XPU), Apple GPUs (MPS) and any CPU.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import urllib.request
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

# Practical-RIFE v4.25 weights (MIT), as converted by RifeMetal (Apache-2.0),
# pinned to the same commit the Mac app is built from.
WEIGHTS_URL = ("https://raw.githubusercontent.com/cinemore/rife-metal/"
               "f18852ddb72f5706ddbeb3333b80643a63634222/Sources/RifeMetal/Resources/rife-v4.25.rmw")
WEIGHTS_SHA256 = "ef29a643a76a9202600458a23dddce16919337ef01743d0fbe817684ec990f74"

TIER_SCALE = {"hq": 1.0, "balanced": 0.5, "fast": 0.25}


# --------------------------------------------------------------------------- network

def _conv(cin: int, cout: int, k: int = 3, s: int = 1, p: int = 1) -> nn.Sequential:
    return nn.Sequential(nn.Conv2d(cin, cout, k, s, p, bias=True), nn.LeakyReLU(0.2, True))


class Head(nn.Module):
    """Per-image encoder: RGB -> 4 feature channels."""

    def __init__(self) -> None:
        super().__init__()
        self.cnn0 = nn.Conv2d(3, 16, 3, 2, 1)
        self.cnn1 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn2 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn3 = nn.ConvTranspose2d(16, 4, 4, 2, 1)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.relu(self.cnn0(x))
        x = self.relu(self.cnn1(x))
        x = self.relu(self.cnn2(x))
        return self.cnn3(x)


class ResConv(nn.Module):
    def __init__(self, c: int) -> None:
        super().__init__()
        self.conv = nn.Conv2d(c, c, 3, 1, 1)
        self.beta = nn.Parameter(torch.ones((1, c, 1, 1)))
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.relu(self.conv(x) * self.beta + x)


class IFBlock(nn.Module):
    def __init__(self, cin: int, c: int) -> None:
        super().__init__()
        self.conv0 = nn.Sequential(_conv(cin, c // 2, 3, 2, 1), _conv(c // 2, c, 3, 2, 1))
        self.convblock = nn.Sequential(*[ResConv(c) for _ in range(8)])
        self.lastconv = nn.Sequential(nn.ConvTranspose2d(c, 4 * 13, 4, 2, 1), nn.PixelShuffle(2))

    @staticmethod
    def shrink(x: torch.Tensor, scale: float) -> torch.Tensor:
        return F.interpolate(x, scale_factor=1.0 / scale, mode="bilinear", align_corners=False)

    def forward(self, x, flow=None, scale: float = 1.0):
        """x: the block's inputs, already shrunk by `scale` (see IFNet.forward)."""
        if flow is not None:
            flow = F.interpolate(flow, scale_factor=1.0 / scale, mode="bilinear", align_corners=False) / scale
            x = torch.cat((x, flow), 1)
        feat = self.convblock(self.conv0(x))
        tmp = F.interpolate(self.lastconv(feat), scale_factor=scale, mode="bilinear", align_corners=False)
        return tmp[:, :4] * scale, tmp[:, 4:5], tmp[:, 5:]


_grid_cache: dict = {}


def warp(img: torch.Tensor, flow: torch.Tensor) -> torch.Tensor:
    n, _, h, w = flow.shape
    key = (flow.device, flow.dtype, n, h, w)
    grid = _grid_cache.get(key)
    if grid is None:
        gx = torch.linspace(-1.0, 1.0, w, device=flow.device, dtype=flow.dtype).view(1, 1, 1, w).expand(n, -1, h, -1)
        gy = torch.linspace(-1.0, 1.0, h, device=flow.device, dtype=flow.dtype).view(1, 1, h, 1).expand(n, -1, -1, w)
        grid = torch.cat([gx, gy], 1)
        _grid_cache[key] = grid
    f = torch.cat([flow[:, 0:1] / ((img.shape[3] - 1.0) / 2.0), flow[:, 1:2] / ((img.shape[2] - 1.0) / 2.0)], 1)
    g = (grid + f).permute(0, 2, 3, 1)
    return F.grid_sample(img, g, mode="bilinear", padding_mode="border", align_corners=True)


class IFNet(nn.Module):
    """IFNet v4.25: 5 refinement blocks at scales [16, 8, 4, 2, 1] / tier scale."""

    def __init__(self) -> None:
        super().__init__()
        self.block0 = IFBlock(7 + 8, 192)
        self.block1 = IFBlock(8 + 4 + 8 + 8, 128)
        self.block2 = IFBlock(8 + 4 + 8 + 8, 96)
        self.block3 = IFBlock(8 + 4 + 8 + 8, 64)
        self.block4 = IFBlock(8 + 4 + 8 + 8, 32)
        self.encode = Head()

    def forward(self, img0, img1, f0, f1, timestep, scale_list):
        """img*: [N,3,H,W] in 0..1, f*: encoder features [N,4,H,W], timestep: [N,1,H,W].
        img* and f* may be one frame expanded over the batch (all timesteps of one pair).

        Each block shrinks its inputs by up to 32x first. Bilinear resizing works per
        channel, so shrinking every input separately and joining the small results gives
        the same numbers as joining at full size and then shrinking, without building
        a full-size 24-channel tensor per block (that join was ~30% of the GPU time)."""
        blocks = [self.block0, self.block1, self.block2, self.block3, self.block4]
        n = timestep.shape[0]
        flow = mask = feat = None
        w0, w1 = img0, img1
        for i, block in enumerate(blocks):
            s = scale_list[i]
            shrink = block.shrink
            if flow is None:
                # Same for every timestep of the pair: shrink one copy, then expand.
                same = [shrink(t[:1], s).expand(n, -1, -1, -1) for t in (img0, img1, f0, f1)]
                flow, mask, feat = block(torch.cat(same + [shrink(timestep, s)], 1), None, scale=s)
            else:
                wf0 = warp(f0, flow[:, :2])
                wf1 = warp(f1, flow[:, 2:4])
                x = torch.cat([shrink(t, s) for t in (w0, w1, wf0, wf1, timestep, mask, feat)], 1)
                fd, mask, feat = block(x, flow, scale=s)
                flow = flow + fd
            w0 = warp(img0, flow[:, :2])
            w1 = warp(img1, flow[:, 2:4])
        m = torch.sigmoid(mask)
        return w0 * m + w1 * (1 - m)


# --------------------------------------------------------------------------- weights

def weights_path(explicit: str | None = None) -> Path:
    """Explicit path, $FRAME_REPAIR_WEIGHTS, or a cached download (checksum-verified)."""
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("FRAME_REPAIR_WEIGHTS"):
        return Path(os.environ["FRAME_REPAIR_WEIGHTS"]).expanduser()
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "frame-repair"
    path = cache / "rife-v4.25.rmw"
    if path.exists() and _sha256(path) == WEIGHTS_SHA256:
        return path
    cache.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    print(f"Downloading RIFE v4.25 weights (11 MB) to {path} ...", flush=True)
    req = urllib.request.Request(WEIGHTS_URL, headers={"User-Agent": "frame-repair"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        f.write(r.read())
    if _sha256(tmp) != WEIGHTS_SHA256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("downloaded weights failed the checksum; try again or pass --model")
    tmp.replace(path)
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rmw(path: Path) -> dict[str, torch.Tensor]:
    """Reads a .rmw file into a PyTorch state dict (undoing RifeMetal's HWIO layout)."""
    data = path.read_bytes()
    if data[:4] != b"RMW1":
        raise ValueError(f"{path} is not a .rmw weight file")
    (hlen,) = struct.unpack("<I", data[8:12])
    header = json.loads(data[12:12 + hlen])
    base = 12 + hlen
    base += (16 - base % 16) % 16
    state: dict[str, torch.Tensor] = {}
    for t in header["tensors"]:
        arr = np.frombuffer(data, dtype=np.float16, count=t["size"] // 2, offset=base + t["offset"])
        arr = arr.reshape(t["shape"]).astype(np.float32)
        name = t["name"]
        if arr.ndim == 4:
            if name.endswith(".beta"):
                arr = arr.reshape(1, -1, 1, 1)
            else:
                arr = arr.transpose(3, 2, 0, 1)      # HWIO -> OIHW (and back for ConvTranspose)
        state[name] = torch.from_numpy(np.ascontiguousarray(arr))
    return state


# --------------------------------------------------------------------------- runtime

def pick_device(name: str = "auto") -> torch.device:
    if name != "auto":
        return torch.device(name)
    if torch.cuda.is_available():                       # Nvidia CUDA or AMD ROCm
        return torch.device("cuda")
    if hasattr(torch, "xpu") and torch.xpu.is_available():   # Intel GPUs
        return torch.device("xpu")
    if torch.backends.mps.is_available():               # Apple GPUs
        return torch.device("mps")
    return torch.device("cpu")


def device_label(dev: torch.device) -> str:
    if dev.type == "cuda":
        kind = "AMD ROCm" if getattr(torch.version, "hip", None) else "Nvidia CUDA"
        return f"{kind}: {torch.cuda.get_device_name(dev)}"
    if dev.type == "xpu":
        return f"Intel XPU: {torch.xpu.get_device_name(dev)}"
    if dev.type == "mps":
        return "Apple GPU (MPS)"
    return f"CPU ({torch.get_num_threads()} threads)"


class Interpolator:
    """Generates in-between frames. Each real frame's encoder features are cached, so
    consecutive pairs reuse them, and all timesteps of a pair run as one batch."""

    def __init__(self, device: torch.device, tier: str = "hq", precision: str = "auto",
                 weights: str | None = None, max_batch: int = 8) -> None:
        self.device = device
        self.max_batch = max(1, max_batch)
        self.scale = TIER_SCALE[tier]
        self.scale_list = [16 / self.scale, 8 / self.scale, 4 / self.scale, 2 / self.scale, 1 / self.scale]
        self.pad = int(64 / self.scale)
        if precision == "auto":
            precision = "fp16" if device.type in ("cuda", "xpu") else "fp32"
        self.dtype = torch.float16 if precision == "fp16" else torch.float32
        net = IFNet()
        net.load_state_dict(load_rmw(weights_path(weights)), strict=True)
        self.net = net.to(device=device, dtype=self.dtype).eval()
        self.channels_last = device.type == "cuda"
        if self.channels_last:                      # faster convolutions on tensor cores
            self.net = self.net.to(memory_format=torch.channels_last)
        self.autotune(False)

    def autotune(self, on: bool) -> None:
        """cuDNN benchmark mode: times every convolution algorithm on the first call with
        each new batch size (~0.5 s each at 1440p on an RTX 5070 Ti), then runs ~7%
        faster. Worth it only for long jobs; the engine turns it on for those."""
        if self.device.type == "cuda":
            torch.backends.cudnn.benchmark = on
        self._feat: dict[int, tuple[torch.Tensor, torch.Tensor]] = {}   # key -> (padded img, features)

    @torch.inference_mode()
    def prepare(self, key: int, frame: np.ndarray):
        """Upload a uint8 HxWx3 RGB frame and compute its features (cached by key)."""
        hit = self._feat.get(key)
        if hit is not None:
            return hit
        h, w = frame.shape[:2]
        t = torch.from_numpy(frame).to(self.device, non_blocking=True).permute(2, 0, 1).unsqueeze(0)
        t = t.to(self.dtype) / 255.0
        ph, pw = (self.pad - h % self.pad) % self.pad, (self.pad - w % self.pad) % self.pad
        if ph or pw:
            t = F.pad(t, (0, pw, 0, ph), mode="replicate")
        if self.channels_last:
            t = t.contiguous(memory_format=torch.channels_last)
        entry = (t, self.net.encode(t))
        if len(self._feat) >= 4:                       # only neighbours are ever reused
            self._feat.pop(next(iter(self._feat)))
        self._feat[key] = entry
        return entry

    @torch.inference_mode()
    def interpolate(self, key0: int, frame0: np.ndarray, key1: int, frame1: np.ndarray,
                    timesteps: list[float]) -> torch.Tensor:
        """Returns uint8 frames [N,H,W,3] on the device, one per timestep in (0, 1)."""
        h, w = frame0.shape[:2]
        img0, f0 = self.prepare(key0, frame0)
        img1, f1 = self.prepare(key1, frame1)
        _, _, hp, wp = img0.shape
        outs = []
        for i in range(0, len(timesteps), self.max_batch):
            chunk = timesteps[i:i + self.max_batch]
            n = len(chunk)
            ts = torch.tensor(chunk, device=self.device, dtype=self.dtype).view(n, 1, 1, 1).expand(n, 1, hp, wp)
            out = self.net(img0.expand(n, -1, -1, -1), img1.expand(n, -1, -1, -1),
                           f0.expand(n, -1, -1, -1), f1.expand(n, -1, -1, -1), ts, self.scale_list)
            outs.append(out[:, :, :h, :w].clamp(0, 1).mul(255).round().to(torch.uint8).permute(0, 2, 3, 1))
        return outs[0] if len(outs) == 1 else torch.cat(outs)

    def to_host(self, t: torch.Tensor) -> np.ndarray:
        """A device tensor as a numpy array. On CUDA the copy lands in pinned (page-locked)
        memory, about twice as fast as .cpu(). Each call gets its own buffer, so arrays
        still waiting in the encoder's queue are never overwritten."""
        if self.device.type != "cuda":
            return t.cpu().numpy()
        out = torch.empty(t.shape, dtype=t.dtype, pin_memory=True)
        out.copy_(t)
        return out.numpy()

    def upload(self, frame: np.ndarray) -> torch.Tensor:
        """A uint8 HxWx3 frame as a tensor on the device (for motion-blur averaging)."""
        return torch.from_numpy(frame).to(self.device, non_blocking=True)

    def sync(self) -> None:
        """Waits for queued GPU work (only used for RIFE_REPAIR_TIMING)."""
        mod = getattr(torch, self.device.type, None)
        if mod is not None and hasattr(mod, "synchronize"):
            mod.synchronize()

    def forget(self) -> None:
        self._feat.clear()
