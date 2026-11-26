"""The Frame Repair app for Windows (and Linux): drop clips, Analyze, pick a look,
Process. Mirrors the Mac app. Each clip runs the same engine as the command line
(`python -m frame_repair ... --machine-progress`) as a hidden child process, and the
window follows its progress lines.

    pythonw -m frame_repair.gui [clips...]        (the installer makes a shortcut for this)
    python -m frame_repair.gui --write-icon frame-repair.ico

Self-test without clicking (like the Mac app's): FRAME_REPAIR_SELFTEST="a.mkv|b.mkv"
FRAME_REPAIR_SELFTEST_OUT=<folder> [FRAME_REPAIR_SELFTEST_SHOTS=<folder for screenshots>].
"""

from __future__ import annotations

import codecs
import os
import re
import shlex
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QObject, QPointF, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QImage, QKeySequence,
                           QLinearGradient, QPainter, QPainterPath, QPalette, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from . import __version__
from .appkit import (FORMAT_LABELS, FRAME_RATES, MOVIE_EXTENSIONS, PRESET_BY_KEY, PRESETS, QUALITY_LABELS,
                     TIMING_LABELS, ClipAnalysis, LineBuffer, RepairSettings, matching_preset, overall_fraction,
                     parse_event, rate_label)

WINDOWS = sys.platform == "win32"
APP_DIR = Path(__file__).resolve().parent.parent          # the folder holding frame_repair/
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GREEN, RED = QColor(46, 157, 79), QColor(214, 69, 69)


# =========================================================================== engine

def python_exe() -> str:
    """The engine runs under python.exe (not pythonw), hidden, so its ffmpeg children
    share its invisible console instead of each opening a window."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def engine_env() -> dict:
    """What the launchers' env.cmd sets up: the app folder on the path, the bundled
    weights, and the ffmpeg the installer found (settings.cmd / settings.sh)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    weights = APP_DIR / "weights" / "rife-v4.25.rmw"
    if weights.exists() and not env.get("FRAME_REPAIR_WEIGHTS"):
        env["FRAME_REPAIR_WEIGHTS"] = str(weights)
    for name in ("settings.cmd", "settings.sh"):
        f = APP_DIR / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = (re.match(r'\s*set\s+"(FFMPEG|FFPROBE)=(.*)"\s*$', line, re.I)
                 or re.match(r"\s*export\s+(FFMPEG|FFPROBE)='(.*)'\s*$", line))
            if m and not env.get(m[1].upper()):
                env[m[1].upper()] = m[2]
    return env


class Engine(QObject):
    """One run of the engine. Emits each output line (stderr lines flagged) and then
    the exit code, always on the GUI thread."""
    line = Signal(str, bool)
    finished = Signal(int)
    # Emitted from the reader threads. Connected to this object's own methods, so Qt
    # queues them onto the GUI thread (a plain function slot would run on the reader
    # thread), and they re-emit the public signals there.
    _raw_line = Signal(str, bool)
    _raw_done = Signal(int)

    def __init__(self, args: list[str]) -> None:
        super().__init__()
        self.args = args
        self.proc: subprocess.Popen | None = None
        self._raw_line.connect(self._relay_line)
        self._raw_done.connect(self._relay_done)

    def _relay_line(self, text: str, is_err: bool) -> None:
        self.line.emit(text, is_err)

    def _relay_done(self, code: int) -> None:
        self.finished.emit(code)

    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(
                [python_exe(), "-m", "frame_repair", *self.args], cwd=str(APP_DIR), env=engine_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=NO_WINDOW, start_new_session=not WINDOWS)
        except OSError as e:
            self.line.emit(f"Error: could not start the engine: {e}", True)
            QTimer.singleShot(0, lambda: self.finished.emit(-1))
            return
        readers = [threading.Thread(target=self._read, args=(self.proc.stdout, False), daemon=True),
                   threading.Thread(target=self._read, args=(self.proc.stderr, True), daemon=True)]
        for t in readers:
            t.start()

        def wait() -> None:
            for t in readers:
                t.join()
            self._raw_done.emit(self.proc.wait())
        threading.Thread(target=wait, daemon=True).start()

    def _read(self, stream, is_err: bool) -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = LineBuffer()
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            for ln in buf.feed(dec.decode(chunk)):
                self._raw_line.emit(ln, is_err)
        for ln in buf.feed(dec.decode(b"", final=True)) + buf.flush():
            self._raw_line.emit(ln, is_err)

    def cancel(self) -> None:
        """Stops the engine and its ffmpeg children (a plain kill would leave ffmpeg
        running, still holding the unfinished output file open)."""
        p = self.proc
        if p is None or p.poll() is not None:
            return
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                p.kill()


# =========================================================================== model

ADDED, ANALYSIS_QUEUED, ANALYZING, ANALYZED, ANALYSIS_FAILED, QUEUED, RUNNING, DONE, FAILED, CANCELLED = range(10)


class Job:
    _next = 0

    def __init__(self, path: Path) -> None:
        Job._next += 1
        self.id = Job._next
        self.path = path
        self.status = ADDED
        self.progress = 0.0
        self.analysis: ClipAnalysis | None = None
        self.output: Path | None = None
        self.log: list[str] = []
        self.elapsed: float | None = None
        self.error = ""
        self.settings: RepairSettings | None = None     # captured when Process was pressed
        self.engine: Engine | None = None
        self.pending_output: Path | None = None          # file being written right now
        self.cancel_requested = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def can_analyze(self) -> bool:
        return self.status in (ADDED, ANALYSIS_FAILED)

    @property
    def can_process(self) -> bool:
        return self.status in (ANALYZED, FAILED, CANCELLED)

    @property
    def is_busy(self) -> bool:
        return self.status in (ANALYSIS_QUEUED, ANALYZING, QUEUED, RUNNING)


def delete_soon(path: Path, tries: int = 20) -> None:
    """Deletes an unfinished output. Windows may hold the file a moment after its
    writer was killed, so retry for a few seconds."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        if tries > 0:
            QTimer.singleShot(250, lambda: delete_soon(path, tries - 1))


def reveal(path: Path) -> None:
    if WINDOWS:
        subprocess.Popen(f'explorer /select,"{path}"', creationflags=NO_WINDOW)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def open_file(path: Path) -> None:
    if WINDOWS:
        os.startfile(str(path))                    # noqa: the default player (Films & TV, VLC ...)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


# =========================================================================== icon

def draw_icon(px: int) -> QImage:
    """The Mac app's icon (three film frames, the middle one rebuilt, on a dark blue
    tile), drawn for Windows: a fuller tile with less margin."""
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(px)
    inset = s * 0.04
    rect = QRectF(inset, inset, s - 2 * inset, s - 2 * inset)
    tile = QPainterPath()
    tile.addRoundedRect(rect, rect.width() * 0.2, rect.width() * 0.2)
    g = QLinearGradient(QPointF(0, rect.bottom()), QPointF(0, rect.top()))
    g.setColorAt(0, QColor.fromRgbF(0.10, 0.13, 0.24))
    g.setColorAt(1, QColor.fromRgbF(0.20, 0.28, 0.55))
    p.fillPath(tile, g)

    fw, fh, gap = rect.width() * 0.24, rect.height() * 0.34, rect.width() * 0.045
    y = rect.center().y() - fh / 2
    x0 = rect.center().x() - (3 * fw + 2 * gap) / 2
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(3):
        p.setBrush(QColor.fromRgbF(0.45, 0.78, 1.0) if i == 1 else QColor.fromRgbF(0.92, 0.92, 0.92))
        p.drawRoundedRect(QRectF(x0 + i * (fw + gap), y, fw, fh), fw * 0.12, fw * 0.12)
    if px >= 32:                                   # sprocket holes: too fine for small sizes
        p.setBrush(QColor(255, 255, 255, 140))
        hole = rect.width() * 0.04
        for row in (y - hole * 2.2, y + fh + hole * 1.2):
            x = x0
            while x < x0 + 3 * fw + 2 * gap - hole:
                p.drawRoundedRect(QRectF(x, row, hole, hole), hole * 0.25, hole * 0.25)
                x += hole * 2
    p.end()
    return img


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def app_icon() -> QIcon:
    icon = QIcon()
    for px in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(draw_icon(px)))
    return icon


def write_ico(path: Path) -> None:
    """A multi-size .ico (PNG entries) for the Start menu and desktop shortcuts."""
    pngs = []
    for px in ICON_SIZES:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        draw_icon(px).save(buf, "PNG")
        pngs.append((px, bytes(buf.data())))
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for px, png in pngs:
        d = px if px < 256 else 0
        entries += struct.pack("<BBBBHHII", d, d, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.write_bytes(head + entries + data)


# =========================================================================== widgets

class ElidedLabel(QLabel):
    """One line, shortened with "..." to fit; the full text is in the tooltip."""

    def __init__(self, mode=Qt.TextElideMode.ElideRight) -> None:
        super().__init__()
        self.mode, self.full = mode, ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full(self, text: str) -> None:
        self.full = text
        self.setToolTip(text)
        self._fit()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()

    def _fit(self) -> None:
        self.setText(self.fontMetrics().elidedText(self.full, self.mode, max(20, self.width())))


# Colours go through style sheets: the Windows 11 style ignores palette text colours on labels.

def dimmed(w: QWidget, alpha: int = 150, small: bool = True) -> QWidget:
    c = QApplication.palette().color(QPalette.ColorRole.WindowText)
    w.setStyleSheet(f"color: rgba({c.red()}, {c.green()}, {c.blue()}, {alpha});")
    if small:
        f = w.font()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        w.setFont(f)
    return w


def coloured(w: QLabel, c: QColor | None) -> QLabel:
    w.setStyleSheet(f"color: {c.name()};" if c is not None else "")
    return w


def footer(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    dimmed(lab)
    return lab


class ClipRow(QWidget):
    def __init__(self, window: "MainWindow", job: Job) -> None:
        super().__init__()
        self.win, self.job = window, job
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(28, 28))
        icon.setFixedWidth(34)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.name = ElidedLabel(Qt.TextElideMode.ElideMiddle)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        self.summary = dimmed(ElidedLabel())
        self.advice = dimmed(ElidedLabel(), alpha=110)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setMaximumHeight(16)
        self.status = ElidedLabel(Qt.TextElideMode.ElideMiddle)
        f = self.status.font()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        self.status.setFont(f)
        self.tag = dimmed(QLabel())
        self.show_btn = QPushButton("Show")
        self.show_btn.setToolTip("Show the output in File Explorer (right-click the clip for more)")
        self.show_btn.clicked.connect(lambda: self.job.output and reveal(self.job.output))

        text = QVBoxLayout()
        text.setSpacing(2)
        for w in (self.name, self.summary, self.advice, self.bar, self.status):
            text.addWidget(w)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 8, 6)
        row.addWidget(icon)
        row.addLayout(text, 1)
        row.addWidget(self.tag, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.show_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.refresh()

    def refresh(self) -> None:
        j = self.job
        self.name.set_full(j.name)
        a = j.analysis
        self.summary.setVisible(a is not None)
        self.advice.setVisible(a is not None and j.status == ANALYZED)
        if a:
            self.summary.set_full(a.summary)
            self.advice.set_full(a.advice)
        busy = j.status in (ANALYZING, RUNNING)
        self.bar.setVisible(busy)
        if busy:
            self.bar.setValue(int(j.progress * 1000))
            self.bar.setFormat(f"{'Analyzing' if j.status == ANALYZING else 'Processing'} {int(j.progress * 100)}%")
        status, colour = "", None
        if j.status == DONE:
            status, colour = f"✓ Saved as {j.output.name if j.output else ''}{elapsed_text(j.elapsed)}", GREEN
        elif j.status in (FAILED, ANALYSIS_FAILED):
            status, colour = f"⚠ {j.error}", RED
        elif j.status == CANCELLED:
            status = "Stopped"
        self.status.setVisible(bool(status))
        if status:
            self.status.set_full(status)
            if colour is None:
                dimmed(self.status, small=False)
            else:
                coloured(self.status, colour)
        tags = {ADDED: "Not analyzed", ANALYSIS_QUEUED: "Waiting to analyze", ANALYZED: "Ready to process",
                QUEUED: "Queued"}
        self.tag.setText(tags.get(j.status, ""))
        self.tag.setVisible(j.status in tags)
        self.show_btn.setVisible(j.status == DONE and j.output is not None)


def elapsed_text(e: float | None) -> str:
    if e is None:
        return ""
    return f"  ({e:.0f} s)" if e < 60 else f"  ({int(e) // 60} min {int(e) % 60:02d} s)"


class LogDialog(QDialog):
    def __init__(self, parent: QWidget, job: Job) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Log: {job.name}")
        self.resize(760, 460)
        text = QPlainTextEdit("\n".join(job.log) if job.log else "Nothing logged yet.")
        text.setReadOnly(True)
        text.setFont(QFont("Consolas", 9))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(text)
        lay.addWidget(buttons)


# =========================================================================== settings panel

class SettingsPanel(QScrollArea):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__()
        self.win = win
        self._syncing = True                 # filling the combo boxes fires their signals
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # wrap, never scroll sideways
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        # ---- Look
        look = QGroupBox("Look")
        lf = QVBoxLayout(look)
        self.preset = QComboBox()
        for p in PRESETS:
            if p.key == "manual":
                self.preset.insertSeparator(self.preset.count())
            self.preset.addItem(p.label, p.key)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        lf.addWidget(self.preset)

        self.summary = QWidget()
        sf = QFormLayout(self.summary)
        sf.setContentsMargins(0, 4, 0, 0)
        self.sum_rate, self.sum_timing, self.sum_blur, self.sum_quality = QLabel(), QLabel(), QLabel(), QLabel()
        for label, w in (("Frame rate", self.sum_rate), ("Timing", self.sum_timing),
                         ("Motion blur", self.sum_blur), ("Quality", self.sum_quality)):
            sf.addRow(label, w)

        self.manual = QWidget()
        mf = QFormLayout(self.manual)
        mf.setContentsMargins(0, 4, 0, 0)
        self.rate = QComboBox()
        for r in FRAME_RATES:
            self.rate.addItem(rate_label(r), r)
        self.rate.currentIndexChanged.connect(lambda: self._set(frame_rate=self.rate.currentData()))
        self.timing = QComboBox()
        for k, v in TIMING_LABELS.items():
            self.timing.addItem(v, k)
        self.timing.currentIndexChanged.connect(lambda: self._set(timing=self.timing.currentData()))
        self.lock_note = footer()
        self.blur = QCheckBox("Motion blur")
        self.blur.toggled.connect(lambda on: self._set(motion_blur=on))
        self.shutter = QSlider(Qt.Orientation.Horizontal)
        self.shutter.setRange(3, 24)                       # x15 degrees: 45..360
        self.shutter.valueChanged.connect(lambda v: self._set(shutter=float(v * 15)))
        self.shutter_label = QLabel()
        self.shutter_label.setMinimumWidth(36)
        self.shutter_row = QWidget()
        sr = QHBoxLayout(self.shutter_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(self.shutter, 1)
        sr.addWidget(self.shutter_label)
        self.quality = QComboBox()
        for k, v in QUALITY_LABELS.items():
            self.quality.addItem(v, k)
        self.quality.currentIndexChanged.connect(lambda: self._set(quality=self.quality.currentData()))
        mf.addRow("Frame rate", self.rate)
        mf.addRow("Timing", self.timing)
        mf.addRow("", self.lock_note)
        mf.addRow("", self.blur)
        mf.addRow("Shutter", self.shutter_row)
        self.manual_form = mf
        mf.addRow("Quality", self.quality)
        lf.addWidget(self.summary)
        lf.addWidget(self.manual)
        self.look_footer = footer()
        lf.addWidget(self.look_footer)
        lay.addWidget(look)

        # ---- Output
        out = QGroupBox("Output")
        of = QFormLayout(out)
        self.format = QComboBox()
        for k in ("prores", "hevc", "h264", "auto"):
            self.format.addItem(FORMAT_LABELS[k], k)
        self.format.currentIndexChanged.connect(lambda: self._set(format=self.format.currentData()))
        self.at_once = QComboBox()
        self.at_once.addItems(["1", "2", "3"])
        self.at_once.currentIndexChanged.connect(lambda i: self._syncing or self.win.set_clips_at_once(i + 1))
        self.folder = ElidedLabel(Qt.TextElideMode.ElideMiddle)
        dimmed(self.folder, small=False)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_folder)
        self.clear_folder = QPushButton("Reset")
        self.clear_folder.setToolTip("Save next to the original again")
        self.clear_folder.clicked.connect(lambda: self._set(output_folder=None))
        fr = QWidget()
        frl = QHBoxLayout(fr)
        frl.setContentsMargins(0, 0, 0, 0)
        frl.addWidget(self.folder, 1)
        frl.addWidget(choose)
        frl.addWidget(self.clear_folder)
        of.addRow("Format", self.format)
        of.addRow("Clips at once", self.at_once)
        of.addRow("Save to", fr)
        self.out_footer = footer()
        of.addRow(self.out_footer)
        lay.addWidget(out)

        # ---- Repeat detection (Manual only)
        self.detect = QGroupBox("Repeat detection")
        df = QFormLayout(self.detect)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.5, 10)
        self.threshold.setSingleStep(0.5)
        self.threshold.setDecimals(1)
        self.threshold.valueChanged.connect(lambda v: self._set(threshold=float(v)))
        self.max_gap = QSpinBox()
        self.max_gap.setRange(1, 30)
        self.max_gap.setSuffix(" frames")
        self.max_gap.valueChanged.connect(lambda v: self._set(max_gap=int(v)))
        self.samples = QSpinBox()
        self.samples.setRange(0, 32)
        self.samples.setSpecialValueText("Auto")
        self.samples.valueChanged.connect(lambda v: self._set(samples=int(v) or None))
        df.addRow("Repeat threshold", self.threshold)
        df.addRow("Longest gap to fill", self.max_gap)
        df.addRow("Blur samples", self.samples)
        self.detect_form = df
        df.addRow(footer("Longer runs of repeated frames are treated as real pauses and left alone. Raise the "
                         "threshold only if the analysis finds too few repeats."))
        lay.addWidget(self.detect)
        lay.addStretch(1)
        self.setWidget(body)
        self.sync()

    # -- model <-> widgets

    def _set(self, **changes) -> None:
        if self._syncing:
            return
        s = self.win.settings.copy()
        for k, v in changes.items():
            setattr(s, k, v)
        self.win.set_settings(s)

    def _preset_changed(self) -> None:
        if not self._syncing:
            self.win.set_preset(self.preset.currentData())

    def _choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save processed clips to", self.win.settings.output_folder or "")
        if d:
            self._set(output_folder=d)

    def sync(self) -> None:
        self._syncing = True
        try:
            self._sync()
        finally:
            self._syncing = False

    def _sync(self) -> None:
        s, key = self.win.settings, self.win.preset_key
        self.preset.setCurrentIndex(self.preset.findData(key))
        manual = key == "manual"
        self.summary.setVisible(not manual)
        self.manual.setVisible(manual)
        self.detect.setVisible(manual)

        self.sum_rate.setText(rate_label(s.frame_rate))
        self.sum_timing.setText(TIMING_LABELS[s.effective_timing])
        self.sum_blur.setText(f"{s.shutter:.0f}° shutter" if s.motion_blur else "Off")
        self.sum_quality.setText(QUALITY_LABELS[s.quality] if s.quality != "auto"
                                 else ("Balanced (auto)" if s.motion_blur else "Best (auto)"))

        self.rate.setCurrentIndex(max(0, self.rate.findData(s.frame_rate)))
        locked = s.frame_rate is not None or s.motion_blur
        self.timing.setCurrentIndex(self.timing.findData(s.effective_timing))
        self.timing.setEnabled(not locked)
        if locked:
            why = (("changing the frame rate and adding motion blur" if s.motion_blur else "changing the frame rate")
                   if s.frame_rate is not None else "adding motion blur")
            self.lock_note.setText(f"Locked to Even out timing: {why} needs every frame rebuilt. Set Frame rate to "
                                   "Same as source with Motion blur off to choose Fill.")
        self.lock_note.setVisible(locked)
        self.manual_form.setRowVisible(self.lock_note, locked)
        self.blur.setChecked(s.motion_blur)
        self.shutter.setValue(int(round(s.shutter / 15)))
        self.shutter_label.setText(f"{s.shutter:.0f}°")
        self.manual_form.setRowVisible(self.shutter_row, s.motion_blur)
        self.quality.setCurrentIndex(self.quality.findData(s.quality))

        if not manual:
            self.look_footer.setText(PRESET_BY_KEY[key].description + " Choose Manual to change these.")
        else:
            parts = []
            if s.motion_blur:
                parts.append(f"Each frame blends moments across a {s.shutter:.0f}° shutter (180° is the classic "
                             "film look).")
            if not locked:
                parts.append("Only repeated frames are replaced; everything else is untouched." if s.timing == "fill"
                             else "Every frame is rebuilt on an even grid, for games running below the recording rate.")
            parts.append("Auto quality is Balanced with blur and Best without.")
            self.look_footer.setText(" ".join(parts))

        self.format.setCurrentIndex(self.format.findData(s.format))
        self.at_once.setCurrentIndex(self.win.clips_at_once - 1)
        self.folder.set_full(s.output_folder or "Next to original")
        self.clear_folder.setVisible(s.output_folder is not None)
        self.out_footer.setText(f"Saved as “clip name [{s.file_tag}].mov”. Originals are never changed. Settings "
                                "apply when you press Process; clips already queued keep theirs. Each clip at once "
                                "uses about 2–4 GB of graphics memory.")

        self.threshold.setValue(s.threshold)
        self.max_gap.setValue(s.max_gap)
        self.samples.setValue(s.samples or 0)
        self.detect_form.setRowVisible(self.samples, s.motion_blur)


# =========================================================================== window

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Frame Repair")
        self.setAcceptDrops(True)
        self.store = QSettings("FrameRepair", "Frame Repair")
        self.jobs: list[Job] = []
        self.rows: dict[int, tuple[QListWidgetItem, ClipRow]] = {}
        self.processing = 0
        self.analysis_running = False
        self.reserved: set[Path] = set()

        import json
        try:
            s = RepairSettings.from_dict(json.loads(self.store.value("settings", "") or "{}"))
        except ValueError:
            s = RepairSettings()
        key = self.store.value("preset", "") or ""
        if key not in PRESET_BY_KEY:
            p = matching_preset(s)
            key = p.key if p else "manual"
        self.preset_key = key
        self.settings = PRESET_BY_KEY[key].apply(s)
        n = int(self.store.value("clipsAtOnce", 0) or 0)
        self.clips_at_once = min(max(n, 1), 3) if n else 2

        self._build()
        self.resize(1180, 700)
        geo = self.store.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self.refresh()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 8, 10, 8)

        def button(text, slot, tip, shortcut=None, primary=False):
            b = QPushButton(text)
            b.setToolTip(tip + (f" ({QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.NativeText)})"
                                if shortcut else ""))
            b.clicked.connect(lambda *_: slot())      # not slot directly: clicked passes `checked`
            if primary:
                f = b.font()
                f.setBold(True)
                b.setFont(f)
                b.setMinimumWidth(110)
            if shortcut:
                act = QAction(self)
                act.setShortcut(shortcut)
                act.triggered.connect(lambda *_: b.isEnabled() and b.isVisible() and slot())
                self.addAction(act)
            return b

        self.add_btn = button("Add Clips…", self.choose_clips, "Add clips or a folder of clips", "Ctrl+O")
        self.remove_btn = button("Remove", lambda: self.remove(self.selected_ids()),
                                 "Remove the selected clips from the list", "Del")
        self.clear_btn = button("Clear Finished", self.clear_finished, "Remove finished clips from the list")
        self.stop_btn = button("Stop", self.stop_all,
                               "Stop everything. Unfinished files are deleted; queued clips go back to ready.")
        self.analyze_btn = button("Analyze", self.start_analysis,
                                  "Step 1: find repeated frames and the game's frame rate", "Ctrl+R", primary=True)
        self.process_btn = button("Process", self.start_processing,
                                  "Step 2: render the selected clips with the current settings", "Ctrl+Return",
                                  primary=True)
        for b in (self.add_btn, self.remove_btn, self.clear_btn):
            bl.addWidget(b)
        bl.addStretch(1)
        for b in (self.stop_btn, self.analyze_btn, self.process_btn):
            bl.addWidget(b)

        # clip list, or the empty-state message
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemSelectionChanged.connect(self.refresh_buttons)
        self.list.itemDoubleClicked.connect(self._double_clicked)

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch(1)
        big = QLabel()
        big.setPixmap(app_icon().pixmap(72, 72))
        big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Drop gameplay clips here")
        f = title.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc = footer("Then select clips, click Analyze, and when that's done, click Process. Everything runs on "
                      "this PC.")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        add = QPushButton("Add Clips…")
        add.clicked.connect(lambda *_: self.choose_clips())
        el.addWidget(big)
        el.addWidget(title)
        el.addWidget(desc)
        el.addWidget(add, 0, Qt.AlignmentFlag.AlignCenter)
        el.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(empty)
        self.stack.addWidget(self.list)
        self.drop_frame = QFrame()
        self.drop_frame.setObjectName("drop")
        dl = QVBoxLayout(self.drop_frame)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.stack)

        self.panel = SettingsPanel(self)
        split = QSplitter()
        split.addWidget(self.drop_frame)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 1)
        split.setSizes([780, 400])
        self.panel.setMinimumWidth(340)

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(bar)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        cl.addWidget(line)
        cl.addWidget(split, 1)
        self.setCentralWidget(central)

        self.counts = QLabel()
        self.hint = dimmed(QLabel(), small=False)
        sb = QStatusBar()
        sb.addWidget(self.counts)
        sb.addPermanentWidget(self.hint)
        self.setStatusBar(sb)
        sel_all = QAction(self)
        sel_all.setShortcut(QKeySequence.StandardKey.SelectAll)
        sel_all.triggered.connect(self.list.selectAll)
        self.addAction(sel_all)

    # ------------------------------------------------------------------ settings

    def set_settings(self, s: RepairSettings) -> None:
        import json
        self.settings = s
        self.store.setValue("settings", json.dumps(s.to_dict()))
        self.panel.sync()

    def set_preset(self, key: str) -> None:
        self.preset_key = key
        self.store.setValue("preset", key)
        self.set_settings(PRESET_BY_KEY[key].apply(self.settings))

    def set_clips_at_once(self, n: int) -> None:
        self.clips_at_once = n
        self.store.setValue("clipsAtOnce", n)
        self.pump()

    # ------------------------------------------------------------------ clips

    def choose_clips(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(MOVIE_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add clips", "", f"Videos ({exts});;All files (*)")
        self.add_paths(files)

    def add_paths(self, paths) -> None:
        expanded: list[Path] = []
        for p in map(Path, paths):
            if p.is_dir():
                expanded += sorted((c for c in p.iterdir() if c.is_file()), key=lambda c: c.name.lower())
            else:
                expanded.append(p)
        known = {j.path.resolve() for j in self.jobs}
        added = []
        for p in expanded:
            if p.suffix.lower() not in MOVIE_EXTENSIONS or p.resolve() in known or not p.is_file():
                continue
            job = Job(p)
            known.add(p.resolve())
            self.jobs.append(job)
            row = ClipRow(self, job)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self.rows[job.id] = (item, row)
            added.append(item)
        if added:
            self.list.clearSelection()
            for item in added:
                item.setSelected(True)
        self.refresh()

    def selected_ids(self) -> set[int]:
        return {i.data(Qt.ItemDataRole.UserRole) for i in self.list.selectedItems()}

    def selected_jobs(self) -> list[Job]:
        ids = self.selected_ids()
        return [j for j in self.jobs if j.id in ids]

    def remove(self, ids: set[int]) -> None:
        for j in [j for j in self.jobs if j.id in ids and not j.is_busy]:
            item, _ = self.rows.pop(j.id)
            self.list.takeItem(self.list.row(item))
            self.jobs.remove(j)
        self.refresh()

    def clear_finished(self) -> None:
        self.remove({j.id for j in self.jobs if j.status == DONE})

    def _double_clicked(self, item: QListWidgetItem) -> None:
        job = self._job(item.data(Qt.ItemDataRole.UserRole))
        if job and job.status == DONE and job.output:
            open_file(job.output)

    def _job(self, jid: int) -> Job | None:
        return next((j for j in self.jobs if j.id == jid), None)

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        job = self._job(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if job is None:
            return
        m = QMenu(self)
        if job.status == DONE and job.output:
            m.addAction("Show Output in File Explorer", lambda: reveal(job.output))
            m.addAction("Open Output", lambda: open_file(job.output))
            m.addSeparator()
        m.addAction("Show Original in File Explorer", lambda: reveal(job.path))
        m.addAction("Open Original", lambda: open_file(job.path))
        m.addSeparator()
        a = m.addAction("Analyze Again", lambda: self.start_analysis([job]))
        a.setEnabled(not job.is_busy and not job.can_analyze)
        a = m.addAction("Process Again", lambda: self.start_processing([job]))
        a.setEnabled(job.status == DONE)
        m.addAction("Show Log", lambda: LogDialog(self, job).exec())
        m.addSeparator()
        a = m.addAction("Remove from List", lambda: self.remove({job.id}))
        a.setEnabled(not job.is_busy)
        m.exec(self.list.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ drag and drop

    def _drop_paths(self, e) -> list[str]:
        return [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()] if e.mimeData().hasUrls() else []

    def _highlight(self, on: bool) -> None:
        self.drop_frame.setStyleSheet("#drop { border: 3px solid palette(highlight); border-radius: 8px; }"
                                      if on else "")

    def dragEnterEvent(self, e) -> None:
        if self._drop_paths(e):
            e.acceptProposedAction()
            self._highlight(True)

    def dragLeaveEvent(self, e) -> None:
        self._highlight(False)

    def dropEvent(self, e) -> None:
        self._highlight(False)
        self.add_paths(self._drop_paths(e))
        e.acceptProposedAction()

    # ------------------------------------------------------------------ step 1: analyse

    def start_analysis(self, targets: list[Job] | None = None) -> None:
        for job in targets if targets is not None else [j for j in self.selected_jobs() if j.can_analyze]:
            job.status, job.analysis = ANALYSIS_QUEUED, None
        self.refresh()
        self._next_analysis()

    def _next_analysis(self) -> None:
        if self.analysis_running:
            return
        job = next((j for j in self.jobs if j.status == ANALYSIS_QUEUED), None)
        if job is None:
            return
        self.analysis_running = True
        job.status, job.progress = ANALYZING, 0.0
        collected: list[str] = []
        engine = Engine(RepairSettings.analysis_arguments(job.path))
        job.engine = engine

        def on_line(line: str, is_err: bool) -> None:
            ev = parse_event(line)
            if ev[0] == "progress":
                job.progress = overall_fraction("analyse", ev[2], ev[3], False)
                self.refresh_job(job)
            else:
                collected.append(("! " if is_err else "") + line)

        def on_done(code: int) -> None:
            job.engine = None
            if job.cancel_requested:
                job.cancel_requested = False
                job.status = ADDED
            elif code == 0:
                job.analysis = ClipAnalysis.parse("\n".join(collected))
                job.status = ANALYZED
            else:
                job.log += collected
                reason = next((l for l in reversed(collected) if "Error" in l), "Could not read this clip.")
                job.error = reason.removeprefix("! ").replace("Error: ", "")
                job.status = ANALYSIS_FAILED
            job.progress = 0.0
            self.analysis_running = False
            self.refresh()
            self._next_analysis()

        engine.line.connect(on_line)
        engine.finished.connect(on_done)
        engine.start()
        self.refresh()

    # ------------------------------------------------------------------ step 2: process

    def start_processing(self, targets: list[Job] | None = None) -> None:
        snapshot = self.settings.copy()
        for job in targets if targets is not None else [j for j in self.selected_jobs() if j.can_process]:
            job.settings, job.status, job.progress = snapshot, QUEUED, 0.0
            job.output = job.elapsed = None
            job.error = ""
        self.refresh()
        self.pump()

    def pump(self) -> None:
        """Fill free processing slots from the queue, in list order."""
        while self.processing < self.clips_at_once:
            job = next((j for j in self.jobs if j.status == QUEUED), None)
            if job is None:
                break
            self.processing += 1
            job.status = RUNNING
            self._render(job)
        self.refresh()

    def _render(self, job: Job) -> None:
        s = job.settings or self.settings
        out = s.output_path(job.path, exists=lambda p: p.exists() or p in self.reserved)
        self.reserved.add(out)                         # two clips at once must not pick the same name
        if s.output_folder:
            Path(s.output_folder).mkdir(parents=True, exist_ok=True)
        args = s.arguments(job.path, out)
        job.log = ["$ rife-repair " + " ".join(shlex.quote(a) for a in args)]
        two_pass = s.effective_timing == "even"
        started = time.monotonic()
        engine = Engine(args)
        job.engine, job.pending_output = engine, out

        def on_line(line: str, is_err: bool) -> None:
            ev = parse_event(line)
            if ev[0] == "progress":
                job.progress = overall_fraction(ev[1], ev[2], ev[3], two_pass)
                self.refresh_job(job)
            elif ev[0] == "output":
                job.output = Path(ev[1])
            else:
                job.log.append(("! " if is_err else "") + line)

        def on_done(code: int) -> None:
            self.reserved.discard(out)
            job.engine, job.pending_output = None, None
            job.elapsed = time.monotonic() - started
            if job.cancel_requested:
                job.cancel_requested = False
                job.status = CANCELLED
                delete_soon(out)
            elif code == 0:
                job.status, job.progress = DONE, 1.0
                job.output = job.output or out
            else:
                reason = next((l[2:] for l in reversed(job.log) if l.startswith("! ") and "Error" in l),
                              next((l[2:] for l in reversed(job.log) if l.startswith("! ")),
                                   f"The engine stopped with code {code}."))
                job.error = reason.replace("Error: ", "")
                job.status = FAILED
                delete_soon(out)
            self.processing -= 1
            self.pump()
            if self.processing == 0 and not any(j.status == QUEUED for j in self.jobs) and not self.isActiveWindow():
                QApplication.alert(self)               # flash the taskbar button
        engine.line.connect(on_line)
        engine.finished.connect(on_done)
        engine.start()

    # ------------------------------------------------------------------ stop / quit

    def stop_all(self) -> None:
        for job in self.jobs:
            if job.status == QUEUED:
                job.status = ANALYZED
            elif job.status == ANALYSIS_QUEUED:
                job.status = ADDED
            elif job.status in (RUNNING, ANALYZING) and job.engine:
                job.cancel_requested = True
                job.engine.cancel()
        self.refresh()

    def is_busy(self) -> bool:
        return self.processing > 0 or self.analysis_running or any(j.is_busy for j in self.jobs)

    def closeEvent(self, e) -> None:
        if self.is_busy():
            r = QMessageBox.question(self, "Frame Repair", "Clips are still being processed. Stop them and quit?\n\n"
                                     "Unfinished files are deleted.")
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            pending = [j.pending_output for j in self.jobs if j.pending_output]
            self.stop_all()
            for p in pending:                          # the writers are gone; give Windows a moment
                for _ in range(20):
                    try:
                        p.unlink(missing_ok=True)
                        break
                    except OSError:
                        time.sleep(0.1)
        self.store.setValue("geometry", self.saveGeometry())
        e.accept()

    # ------------------------------------------------------------------ refresh

    def refresh_job(self, job: Job) -> None:
        item, row = self.rows.get(job.id, (None, None))
        if row is not None:
            row.refresh()
            item.setSizeHint(row.sizeHint())

    def refresh(self) -> None:
        for j in self.jobs:
            self.refresh_job(j)
        self.stack.setCurrentIndex(1 if self.jobs else 0)
        analyzed = sum(1 for j in self.jobs if j.analysis is not None)
        done = sum(1 for j in self.jobs if j.status == DONE)
        n = len(self.jobs)
        self.counts.setText(f"{n} clip{'' if n == 1 else 's'} · {analyzed} analyzed · {done} done" if n else "No clips")
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        sel = self.selected_jobs()
        a = sum(1 for j in sel if j.can_analyze)
        p = sum(1 for j in sel if j.can_process)
        self.analyze_btn.setText(f"Analyze {a} Clips" if a > 1 else "Analyze")
        self.analyze_btn.setEnabled(a > 0)
        self.process_btn.setText(f"Process {p} Clips" if p > 1 else "Process")
        self.process_btn.setEnabled(p > 0)
        self.remove_btn.setEnabled(any(not j.is_busy for j in sel))
        self.clear_btn.setEnabled(any(j.status == DONE for j in self.jobs))
        self.stop_btn.setVisible(self.is_busy())
        waiting = sum(1 for j in sel if j.status in (ADDED, ANALYSIS_FAILED, ANALYSIS_QUEUED, ANALYZING))
        if not self.jobs:
            hint = ""
        elif not sel:
            hint = "Select clips (Ctrl+A for all)"
        elif a:
            hint = "Step 1: Analyze"
        elif waiting:
            hint = "Analyzing…"
        elif p:
            hint = "Step 2: Process"
        else:
            hint = ""
        self.hint.setText(hint)


# =========================================================================== self-test

class SelfTest:
    """Drives the real two-step flow without clicking, prints what the window shows,
    saves screenshots, then quits with 0 if every clip finished."""

    def __init__(self, win: MainWindow, clips: list[str], out: str | None, shots: str | None) -> None:
        self.win, self.clips, self.out = win, clips, out
        self.shots = Path(shots) if shots else None
        self.seen: dict[int, list[float]] = {}
        self.max_parallel = 0
        self.started = 0.0
        self.shot_mid = False
        QTimer.singleShot(500, self.step_add)

    def say(self, s: str) -> None:
        log = os.environ.get("FRAME_REPAIR_SELFTEST_LOG")      # pythonw has no console to print to
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(s + "\n")
        elif sys.stdout:
            print(s, flush=True)

    def shot(self, name: str) -> None:
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
            self.win.grab().save(str(self.shots / f"{name}.png"))

    def step_add(self) -> None:
        w = self.win
        self.say(f"preset on launch: {PRESET_BY_KEY[w.preset_key].label}")
        if os.environ.get("FRAME_REPAIR_SELFTEST_AT_ONCE"):
            w.clips_at_once = int(os.environ["FRAME_REPAIR_SELFTEST_AT_ONCE"])      # not saved
        w.add_paths(self.clips)
        self.say(f"after drop: {[j.status for j in w.jobs]}, selected {len(w.selected_ids())}")
        self.say(f"analyze enabled: {w.analyze_btn.isEnabled()}, process enabled: {w.process_btn.isEnabled()}")
        self.shot("1-added")
        w.start_analysis()
        self.poll(self.step_analysed)

    def poll(self, then) -> None:
        def tick():
            if any(j.is_busy for j in self.win.jobs):
                for j in self.win.jobs:
                    if self.seen.get(j.id, [None])[-1] != j.progress and j.status == RUNNING:
                        self.seen.setdefault(j.id, []).append(j.progress)
                running = sum(1 for j in self.win.jobs if j.status == RUNNING)
                self.max_parallel = max(self.max_parallel, running)
                if running and not self.shot_mid and any(j.progress > 0.3 for j in self.win.jobs):
                    self.shot_mid = True
                    self.shot("3-processing")
                QTimer.singleShot(50, tick)
            else:
                then()
        tick()

    def step_analysed(self) -> None:
        w = self.win
        for j in w.jobs:
            self.say(f"analysis {j.name}: {j.analysis.summary if j.analysis else j.error}")
        self.say(f"process enabled: {w.process_btn.isEnabled()}")
        self.shot("2-analyzed")
        saved = (w.settings, w.preset_key)
        w.preset_key = "film"
        w.settings = PRESET_BY_KEY["film"].apply(w.settings)
        w.settings.output_folder = self.out
        self.started = time.monotonic()
        w.start_processing()
        w.settings, w.preset_key = saved
        self.poll(self.step_done)

    def step_done(self) -> None:
        w = self.win
        self.shot("4-done")
        for j in w.jobs:
            p = self.seen.get(j.id, [])
            mono = all(a <= b for a, b in zip(p, p[1:]))
            self.say(f"{j.name}: status {j.status} -> {j.output.name if j.output else j.error or 'none'}  "
                     f"[{len(p)} updates, monotonic={mono}, {j.elapsed or 0:.1f} s]")
            if os.environ.get("FRAME_REPAIR_DEBUG"):
                self.say("    | " + "\n    | ".join(j.log[-25:]))
        self.say(f"max clips at once: {self.max_parallel}, total {time.monotonic() - self.started:.1f} s")
        ok = all(j.status == DONE for j in w.jobs)
        QApplication.exit(0 if ok else 1)


# =========================================================================== main

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if WINDOWS:
        # Own taskbar identity, so the taskbar shows this app's icon, not Python's.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FrameRepair.App")
        except (OSError, AttributeError):
            pass
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Frame Repair")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())

    def on_error(kind, value, tb) -> None:
        # Under pythonw there is no console, so an error would otherwise vanish silently.
        import traceback
        text = "".join(traceback.format_exception(kind, value, tb))
        if sys.stderr:
            sys.stderr.write(text)
        box = QMessageBox(QMessageBox.Icon.Critical, "Frame Repair", f"Something went wrong: {value}")
        box.setDetailedText(text)
        box.exec()
    sys.excepthook = on_error
    if argv[:1] == ["--write-icon"] and len(argv) == 2:
        write_ico(Path(argv[1]))
        return 0
    win = MainWindow()
    win.show()
    files = [a for a in argv if not a.startswith("-")]
    if files:
        win.add_paths(files)
    if os.environ.get("FRAME_REPAIR_SELFTEST"):
        win._selftest = SelfTest(win, os.environ["FRAME_REPAIR_SELFTEST"].split("|"),
                                 os.environ.get("FRAME_REPAIR_SELFTEST_OUT"), os.environ.get("FRAME_REPAIR_SELFTEST_SHOTS"))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
