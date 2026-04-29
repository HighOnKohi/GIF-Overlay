#!/usr/bin/env python3
"""
GIF Overlay
====================
A lightweight, floating transparent GIF / video overlay for your desktop.

Features
--------
  • Load GIF or MP4/AVI/MOV files and display as a transparent overlay
  • Batch **chroma-key** background removal (fast, NumPy-accelerated)
  • Batch **AI background removal** via ``rembg`` (handles complex scenes)
  • Drag to move / drag edges & corners to resize (when unlocked)
  • Lock the overlay to freeze position and enable click-through (Windows)
  • Smooth speed control from 0.1× to 5.0×

Dependencies
------------
    pip install PyQt6 numpy Pillow
    pip install opencv-python   # optional: for MP4 / video support
    pip install rembg            # optional: for AI background removal

Usage
-----
    python gif_overlay.py
"""

from __future__ import annotations

import os
import sys
from enum import IntFlag
from typing import List, Optional

import numpy as np
from PIL import Image
from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# Windows-only: click-through overlay support
if sys.platform == "win32":
    import ctypes


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def pil_to_qpixmap(pil_img: Image.Image) -> QPixmap:
    """Convert a PIL RGBA image to a QPixmap (copies pixel data)."""
    if pil_img.mode != "RGBA":
        pil_img = pil_img.convert("RGBA")
    w, h = pil_img.size
    data = pil_img.tobytes("raw", "RGBA")
    qimg = QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg)  # deep-copies; safe after data goes away


def _check_rembg() -> bool:
    """Return True if rembg is importable."""
    try:
        import rembg  # noqa: F401
        return True
    except ImportError:
        return False


def _check_cv2() -> bool:
    """Return True if OpenCV is importable."""
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


# ═══════════════════════════════════════════════════════════════
#  RESIZE EDGE FLAGS
# ═══════════════════════════════════════════════════════════════

class Edge(IntFlag):
    NONE   = 0
    LEFT   = 1
    RIGHT  = 2
    TOP    = 4
    BOTTOM = 8


# ═══════════════════════════════════════════════════════════════
#  OVERLAY WINDOW
# ═══════════════════════════════════════════════════════════════

class OverlayWindow(QWidget):
    """
    Borderless, transparent, always-on-top widget that renders frames.

    Supports drag-to-move, edge/corner resize (when unlocked), and
    Win32 click-through (when locked).
    """

    GRIP = 12

    _CURSOR = {
        Edge.NONE:                 Qt.CursorShape.OpenHandCursor,
        Edge.LEFT:                 Qt.CursorShape.SizeHorCursor,
        Edge.RIGHT:                Qt.CursorShape.SizeHorCursor,
        Edge.TOP:                  Qt.CursorShape.SizeVerCursor,
        Edge.BOTTOM:               Qt.CursorShape.SizeVerCursor,
        Edge.TOP | Edge.LEFT:      Qt.CursorShape.SizeFDiagCursor,
        Edge.BOTTOM | Edge.RIGHT:  Qt.CursorShape.SizeFDiagCursor,
        Edge.TOP | Edge.RIGHT:     Qt.CursorShape.SizeBDiagCursor,
        Edge.BOTTOM | Edge.LEFT:   Qt.CursorShape.SizeBDiagCursor,
    }

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(64, 64)
        self.setWindowTitle("GIF Overlay")

        self._locked = False
        self._dragging = False
        self._resizing = False
        self._drag_start = QPoint()
        self._resize_edge = Edge.NONE
        self._resize_origin = QPoint()
        self._resize_geom = QRect()

        self.gif_label = QLabel(self)
        self.gif_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gif_label.setScaledContents(True)
        self.gif_label.setStyleSheet("background: transparent;")

        self.resize(320, 320)
        self._sync()

    def _sync(self) -> None:
        self.gif_label.setGeometry(0, 0, self.width(), self.height())

    # ── lock ──────────────────────────────────────────────

    @property
    def locked(self) -> bool:
        return self._locked

    def set_locked(self, state: bool) -> None:
        self._locked = state
        if sys.platform == "win32":
            self._click_through(state)

    def _click_through(self, enable: bool) -> None:
        hwnd = int(self.winId())
        GWL = -20
        EX_T, EX_L = 0x00000020, 0x00080000
        u32 = ctypes.windll.user32
        s = u32.GetWindowLongW(hwnd, GWL)
        if enable:
            u32.SetWindowLongW(hwnd, GWL, s | EX_T | EX_L)
        else:
            u32.SetWindowLongW(hwnd, GWL, s & ~EX_T)

    # ── edge detection ────────────────────────────────────

    def _hit(self, pos: QPoint) -> Edge:
        if self._locked:
            return Edge.NONE
        e, g = Edge.NONE, self.GRIP
        w, h = self.width(), self.height()
        if pos.x() <= g:        e |= Edge.LEFT
        elif pos.x() >= w - g:  e |= Edge.RIGHT
        if pos.y() <= g:        e |= Edge.TOP
        elif pos.y() >= h - g:  e |= Edge.BOTTOM
        return e

    # ── events ────────────────────────────────────────────

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._sync()

    def showEvent(self, ev):
        super().showEvent(ev)
        if sys.platform == "win32" and self._locked:
            QTimer.singleShot(50, lambda: self._click_through(True))

    def mousePressEvent(self, ev):
        if self._locked or ev.button() != Qt.MouseButton.LeftButton:
            return
        gp = ev.globalPosition().toPoint()
        edge = self._hit(ev.pos())
        if edge:
            self._resizing, self._resize_edge = True, edge
            self._resize_origin, self._resize_geom = gp, QRect(self.geometry())
        else:
            self._dragging, self._drag_start = True, gp - self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, ev):
        if self._locked:
            return
        gp = ev.globalPosition().toPoint()
        if self._dragging:
            self.move(gp - self._drag_start)
        elif self._resizing:
            d, r, mn, e = gp - self._resize_origin, QRect(self._resize_geom), 64, self._resize_edge
            if e & Edge.LEFT:   r.setLeft(min(r.left() + d.x(), r.right() - mn))
            if e & Edge.RIGHT:  r.setRight(max(r.right() + d.x(), r.left() + mn))
            if e & Edge.TOP:    r.setTop(min(r.top() + d.y(), r.bottom() - mn))
            if e & Edge.BOTTOM: r.setBottom(max(r.bottom() + d.y(), r.top() + mn))
            self.setGeometry(r)
        else:
            self.setCursor(self._CURSOR.get(self._hit(ev.pos()), Qt.CursorShape.ArrowCursor))

    def mouseReleaseEvent(self, ev):
        if self._dragging:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._dragging = self._resizing = False
        self._resize_edge = Edge.NONE

    def paintEvent(self, ev):
        super().paintEvent(ev)
        pix = self.gif_label.pixmap()
        if not self._locked and (pix is None or pix.isNull()):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(180, 130, 255, 120), 2, Qt.PenStyle.DashLine))
            p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 10, 10)
            p.setPen(QColor(180, 130, 255, 150))
            p.setFont(QFont("Segoe UI", 11))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No file loaded")
            p.end()


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND-REMOVAL WORKER  (runs in QThread)
# ═══════════════════════════════════════════════════════════════

class ProcessingWorker(QThread):
    """
    Processes a list of PIL RGBA frames in a background thread.

    Supports two methods:
      • ``"chroma"`` – NumPy chroma-key (fast; ~2 ms / frame)
      • ``"rembg"``  – AI background removal via rembg (~1-3 s / frame)
    """

    progress = pyqtSignal(int, int)        # (current, total)
    finished = pyqtSignal(list)            # list[PIL.Image.Image]
    error    = pyqtSignal(str)

    def __init__(
        self,
        frames: List[Image.Image],
        method: str,
        *,
        chroma_r: int = 0,
        chroma_g: int = 255,
        chroma_b: int = 0,
        chroma_tol: int = 30,
    ) -> None:
        super().__init__()
        self._frames = frames
        self._method = method
        self._kr, self._kg, self._kb = chroma_r, chroma_g, chroma_b
        self._tol = chroma_tol
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            results = (
                self._chroma() if self._method == "chroma" else self._rembg()
            )
            if not self._cancelled:
                self.finished.emit(results)
        except Exception as exc:
            if not self._cancelled:
                self.error.emit(str(exc))

    # ── chroma key (NumPy) ────────────────────────────────

    def _chroma(self) -> List[Image.Image]:
        tol_sq = self._tol * self._tol
        results: List[Image.Image] = []
        total = len(self._frames)
        for i, frame in enumerate(self._frames):
            if self._cancelled:
                return []
            arr = np.array(frame.convert("RGBA"))          # R G B A
            dr = arr[:, :, 0].astype(np.int32) - self._kr
            dg = arr[:, :, 1].astype(np.int32) - self._kg
            db = arr[:, :, 2].astype(np.int32) - self._kb
            dist_sq = dr * dr + dg * dg + db * db
            arr[:, :, 3] = np.where(dist_sq <= tol_sq, 0, arr[:, :, 3])
            results.append(Image.fromarray(arr, "RGBA"))
            self.progress.emit(i + 1, total)
        return results

    # ── rembg (AI) ────────────────────────────────────────

    def _rembg(self) -> List[Image.Image]:
        from rembg import remove, new_session

        # Using DirectML for GPU acceleration on Windows (works for NVIDIA/AMD/Intel)
        session = new_session(providers=["DmlExecutionProvider", "CPUExecutionProvider"])

        results: List[Image.Image] = []
        total = len(self._frames)
        for i, frame in enumerate(self._frames):
            if self._cancelled:
                return []
            out = remove(frame.convert("RGBA"), session=session)
            results.append(out.convert("RGBA"))
            self.progress.emit(i + 1, total)
        return results


# ═══════════════════════════════════════════════════════════════
#  STYLESHEET
# ═══════════════════════════════════════════════════════════════

DARK_STYLE = """
QMainWindow { background: #0c0c1d; }
QWidget#central { background: #0c0c1d; }

QFrame#card {
    background: rgba(18,18,42,0.95);
    border: 1px solid rgba(138,92,246,0.18);
    border-radius: 14px;
    padding: 14px;
}
QLabel { color: #e2e0ff; font-family: "Segoe UI","Inter",sans-serif; }
QLabel#title   { font-size:22px; font-weight:700; color:#c4b5fd; }
QLabel#sub     { font-size:11px; color:rgba(196,181,253,0.50); }
QLabel#sec     { font-size:13px; font-weight:600; color:#a78bfa; }
QLabel#val     { font-size:15px; font-weight:700; color:#c4b5fd;
                 font-family:"Cascadia Code","Consolas",monospace; }
QLabel#preview { background:rgba(10,10,26,0.70);
                 border:1px solid rgba(138,92,246,0.12);
                 border-radius:10px; color:rgba(196,181,253,0.35);
                 font-size:12px; }
QLabel#fname   { font-size:11px; color:rgba(196,181,253,0.50); font-style:italic; }
QLabel#hint    { font-size:10px; color:rgba(196,181,253,0.35); }

/* ── Buttons ──────────────────────────────────────────────── */
QPushButton#primary {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #7c3aed,stop:1 #a855f7);
    color:#fff; border:none; border-radius:10px;
    padding:10px 20px; font-size:13px; font-weight:600;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #8b5cf6,stop:1 #c084fc);
}
QPushButton#primary:pressed {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #6d28d9,stop:1 #9333ea);
}

QPushButton#toggle {
    background:rgba(30,30,60,0.80); color:#c4b5fd;
    border:1px solid rgba(138,92,246,0.25); border-radius:10px;
    padding:9px 16px; font-size:12px; font-weight:600;
}
QPushButton#toggle:hover { background:rgba(40,40,80,0.90); border-color:rgba(138,92,246,0.45); }
QPushButton#toggle:checked {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #dc2626,stop:1 #ef4444);
    color:#fff; border:1px solid rgba(239,68,68,0.4);
}
QPushButton#toggle:checked:hover {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ef4444,stop:1 #f87171);
}

QPushButton#visBtn {
    background:rgba(30,30,60,0.80); color:#c4b5fd;
    border:1px solid rgba(138,92,246,0.25); border-radius:10px;
    padding:9px 16px; font-size:12px; font-weight:600;
}
QPushButton#visBtn:hover { background:rgba(40,40,80,0.90); border-color:rgba(138,92,246,0.45); }
QPushButton#visBtn:checked {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #0891b2,stop:1 #06b6d4);
    color:#fff; border:1px solid rgba(6,182,212,0.4);
}

QPushButton#action {
    background:rgba(30,30,60,0.80); color:#c4b5fd;
    border:1px solid rgba(138,92,246,0.25); border-radius:8px;
    padding:8px 12px; font-size:11px; font-weight:600;
}
QPushButton#action:hover { background:rgba(40,40,80,0.90); border-color:rgba(138,92,246,0.45); }
QPushButton#action:disabled { color:rgba(196,181,253,0.20); border-color:rgba(138,92,246,0.08); }

QPushButton#actionGreen {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #059669,stop:1 #10b981);
    color:#fff; border:none; border-radius:8px;
    padding:8px 12px; font-size:11px; font-weight:600;
}
QPushButton#actionGreen:hover {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #10b981,stop:1 #34d399);
}
QPushButton#actionGreen:disabled { background:rgba(30,30,60,0.60); color:rgba(196,181,253,0.20); }

QPushButton#actionCyan {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #0891b2,stop:1 #06b6d4);
    color:#fff; border:none; border-radius:8px;
    padding:8px 12px; font-size:11px; font-weight:600;
}
QPushButton#actionCyan:hover {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #06b6d4,stop:1 #22d3ee);
}
QPushButton#actionCyan:disabled { background:rgba(30,30,60,0.60); color:rgba(196,181,253,0.20); }

QPushButton#swatch {
    border:2px solid rgba(138,92,246,0.35); border-radius:8px;
}
QPushButton#swatch:hover { border-color:rgba(138,92,246,0.75); }

QPushButton#small {
    background:rgba(30,30,60,0.80); color:#e2e0ff;
    border:1px solid rgba(138,92,246,0.25); border-radius:8px;
    font-size:15px; padding:0; min-width:34px; min-height:34px;
}
QPushButton#small:hover { background:rgba(40,40,80,0.90); border-color:rgba(138,92,246,0.45); }

/* ── Slider ───────────────────────────────────────────────── */
QSlider::groove:horizontal { height:6px; background:rgba(255,255,255,0.08); border-radius:3px; }
QSlider::sub-page:horizontal {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #7c3aed,stop:1 #a855f7);
    border-radius:3px;
}
QSlider::handle:horizontal {
    width:18px; height:18px; margin:-6px 0; border-radius:9px;
    background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #c084fc,stop:1 #a855f7);
    border:2px solid rgba(124,58,237,0.50);
}
QSlider::handle:horizontal:hover {
    background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #d8b4fe,stop:1 #c084fc);
    border-color:rgba(139,92,246,0.80);
}

/* ── Progress bar ─────────────────────────────────────────── */
QProgressBar {
    background:rgba(255,255,255,0.06); border:1px solid rgba(138,92,246,0.12);
    border-radius:5px; height:10px; text-align:center; color:transparent;
}
QProgressBar::chunk {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #7c3aed,stop:1 #a855f7);
    border-radius:4px;
}

/* ── Status bar ───────────────────────────────────────────── */
QStatusBar {
    background:rgba(12,12,29,0.95); color:rgba(196,181,253,0.45);
    font-size:11px; border-top:1px solid rgba(138,92,246,0.10); padding:2px 8px;
}
"""


# ═══════════════════════════════════════════════════════════════
#  CONTROL PANEL
# ═══════════════════════════════════════════════════════════════

class ControlPanel(QMainWindow):
    """
    Main settings window.

    Manages a list of PIL RGBA frames (original + optionally processed),
    drives a QTimer-based playback loop, and pushes each frame to the
    overlay and preview as a QPixmap.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("GIF Overlay")
        self.setFixedSize(400, 830)
        self.setStyleSheet(DARK_STYLE)

        # ── frame store ───────────────────────────────────
        self._original: List[Image.Image] = []
        self._processed: Optional[List[Image.Image]] = None
        self._durations: List[int] = []       # ms per frame
        self._idx: int = 0
        self._speed: float = 1.0              # multiplier
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

        # ── chroma-key settings ───────────────────────────
        self._chroma_color = QColor(0, 255, 0)
        self._chroma_tol: int = 30
        self._eyedropper: bool = False

        # ── worker (background processing) ────────────────
        self._worker: Optional[ProcessingWorker] = None

        # ── overlay ───────────────────────────────────────
        self.overlay = OverlayWindow()
        scr = QApplication.primaryScreen()
        if scr:
            c = scr.geometry().center()
            self.overlay.move(c.x() - 160, c.y() - 160)
        self.overlay.show()

        self._build_ui()
        self.statusBar().showMessage("Ready — select a GIF or video to get started")

    # ──────────────────────────────────────────────────────
    #  ACTIVE FRAMES PROPERTY
    # ──────────────────────────────────────────────────────

    @property
    def _frames(self) -> List[Image.Image]:
        return self._processed if self._processed else self._original

    # ──────────────────────────────────────────────────────
    #  UI CONSTRUCTION
    # ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        cw = QWidget()
        cw.setObjectName("central")
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setContentsMargins(20, 18, 20, 10)
        root.setSpacing(10)

        # Header
        t = QLabel("🎬  OVERLAY"); t.setObjectName("title"); root.addWidget(t)
        s = QLabel("Floating GIF & Video Overlay"); s.setObjectName("sub"); root.addWidget(s)
        root.addSpacing(2)

        # ── preview card ──────────────────────────────────
        card1 = self._card()
        v1 = QVBoxLayout(card1); v1.setSpacing(6)
        v1.addWidget(self._sec("Preview"))

        self.preview = QLabel("No file selected")
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(140)
        self.preview.setScaledContents(False)
        self.preview.installEventFilter(self)
        v1.addWidget(self.preview)

        self.fname = QLabel(""); self.fname.setObjectName("fname")
        self.fname.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v1.addWidget(self.fname)

        self.sel_btn = QPushButton("📂  Select File")
        self.sel_btn.setObjectName("primary")
        self.sel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sel_btn.clicked.connect(self._pick_file)
        v1.addWidget(self.sel_btn)
        root.addWidget(card1)

        # ── speed card ────────────────────────────────────
        card2 = self._card()
        v2 = QVBoxLayout(card2); v2.setSpacing(4)
        hr = QHBoxLayout()
        hr.addWidget(self._sec("Playback Speed")); hr.addStretch()
        self.spd_lbl = QLabel("1.0×"); self.spd_lbl.setObjectName("val")
        hr.addWidget(self.spd_lbl); v2.addLayout(hr)

        self.spd_slider = QSlider(Qt.Orientation.Horizontal)
        self.spd_slider.setRange(10, 500); self.spd_slider.setValue(100)
        self.spd_slider.setSingleStep(5); self.spd_slider.setPageStep(25)
        self.spd_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.spd_slider.valueChanged.connect(self._on_speed)
        v2.addWidget(self.spd_slider)

        ticks = QHBoxLayout()
        for tx in ("0.1×","1×","2×","3×","4×","5×"):
            l = QLabel(tx); l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setStyleSheet("font-size:10px;color:rgba(196,181,253,0.28);")
            ticks.addWidget(l)
        v2.addLayout(ticks)
        root.addWidget(card2)

        # ── BG removal card ───────────────────────────────
        card3 = self._card()
        v3 = QVBoxLayout(card3); v3.setSpacing(8)
        v3.addWidget(self._sec("Background Removal"))

        # Chroma key settings row
        cr = QHBoxLayout(); cr.setSpacing(6)
        self.swatch = QPushButton(); self.swatch.setObjectName("swatch")
        self.swatch.setFixedSize(34, 34)
        self.swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch.setToolTip("Pick key colour")
        self.swatch.clicked.connect(self._pick_color)
        self._refresh_swatch()
        cr.addWidget(self.swatch)

        self.eye_btn = QPushButton("🎯"); self.eye_btn.setObjectName("small")
        self.eye_btn.setFixedSize(34, 34)
        self.eye_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eye_btn.setToolTip("Sample colour from preview")
        self.eye_btn.clicked.connect(self._toggle_eye)
        cr.addWidget(self.eye_btn)

        cr.addSpacing(4)
        tl = QLabel("Tol"); tl.setStyleSheet("font-size:11px;color:rgba(196,181,253,0.50);")
        cr.addWidget(tl)
        self.tol_slider = QSlider(Qt.Orientation.Horizontal)
        self.tol_slider.setRange(0, 200); self.tol_slider.setValue(30)
        self.tol_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tol_slider.valueChanged.connect(self._on_tol)
        cr.addWidget(self.tol_slider, stretch=1)
        self.tol_lbl = QLabel("30"); self.tol_lbl.setObjectName("val")
        self.tol_lbl.setFixedWidth(32)
        cr.addWidget(self.tol_lbl)
        v3.addLayout(cr)

        # Action buttons
        ar = QHBoxLayout(); ar.setSpacing(6)
        self.chroma_btn = QPushButton("🎨 Apply Chroma Key")
        self.chroma_btn.setObjectName("actionGreen")
        self.chroma_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chroma_btn.clicked.connect(self._apply_chroma)
        ar.addWidget(self.chroma_btn)

        self.ai_btn = QPushButton("✨ AI Remove BG")
        self.ai_btn.setObjectName("actionCyan")
        self.ai_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ai_btn.clicked.connect(self._apply_rembg)
        if not _check_rembg():
            self.ai_btn.setToolTip("pip install rembg")
            self.ai_btn.setEnabled(False)
        ar.addWidget(self.ai_btn)

        self.reset_btn = QPushButton("↩ Reset")
        self.reset_btn.setObjectName("action")
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_btn.clicked.connect(self._reset_frames)
        ar.addWidget(self.reset_btn)

        self.save_btn = QPushButton("💾 Save")
        self.save_btn.setObjectName("action")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(self._save_processed)
        ar.addWidget(self.save_btn)
        
        v3.addLayout(ar)

        # Progress bar (hidden by default)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        v3.addWidget(self.progress)

        ht = QLabel("Tip: use 🎯 to eyedrop a colour from the preview, then apply chroma key")
        ht.setObjectName("hint"); ht.setWordWrap(True)
        v3.addWidget(ht)
        root.addWidget(card3)

        # ── overlay controls ──────────────────────────────
        card4 = self._card()
        v4 = QVBoxLayout(card4); v4.setSpacing(8)
        v4.addWidget(self._sec("Overlay Controls"))

        shr = QHBoxLayout()
        shr.addWidget(self._sec("Overlay Size")); shr.addStretch()
        self.scale_lbl = QLabel("100%"); self.scale_lbl.setObjectName("val")
        shr.addWidget(self.scale_lbl); v4.addLayout(shr)

        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(20, 500); self.scale_slider.setValue(100)
        self.scale_slider.setSingleStep(10); self.scale_slider.setPageStep(50)
        self.scale_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scale_slider.valueChanged.connect(self._on_scale)
        v4.addWidget(self.scale_slider)

        v4.addSpacing(4)
        v4.addWidget(self._sec("Pin & Visibility"))
        br = QHBoxLayout(); br.setSpacing(8)

        self.lock_btn = QPushButton("🔓 Unlocked")
        self.lock_btn.setObjectName("toggle"); self.lock_btn.setCheckable(True)
        self.lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lock_btn.toggled.connect(self._on_lock)
        br.addWidget(self.lock_btn)

        self.vis_btn = QPushButton("👁 Visible")
        self.vis_btn.setObjectName("visBtn"); self.vis_btn.setCheckable(True)
        self.vis_btn.setChecked(True)
        self.vis_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.vis_btn.toggled.connect(self._on_vis)
        br.addWidget(self.vis_btn)
        v4.addLayout(br)
        root.addWidget(card4)

        root.addStretch()

    # ── tiny helpers ──────────────────────────────────────

    @staticmethod
    def _card():
        f = QFrame(); f.setObjectName("card"); return f

    @staticmethod
    def _sec(t):
        l = QLabel(t); l.setObjectName("sec"); return l

    # ──────────────────────────────────────────────────────
    #  FILE LOADING
    # ──────────────────────────────────────────────────────

    _VID_EXTS = {".mp4", ".avi", ".mov", ".webm", ".mkv", ".wmv"}

    def _pick_file(self) -> None:
        filters = "Supported Files (*.gif *.mp4 *.avi *.mov *.webm *.mkv)"
        filters += ";;GIF Files (*.gif)"
        filters += ";;Video Files (*.mp4 *.avi *.mov *.webm *.mkv)"
        filters += ";;All Files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Select File", "", filters)
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext == ".gif":
            self._load_gif(path)
        elif ext in self._VID_EXTS:
            self._load_video(path)
        else:
            self.statusBar().showMessage(f"⚠ Unsupported format: {ext}")

    def _load_gif(self, path: str) -> None:
        """Extract all frames from a GIF using Pillow."""
        self._stop()
        self._original.clear()
        self._processed = None
        self._durations.clear()

        try:
            gif = Image.open(path)
            for i in range(getattr(gif, "n_frames", 1)):
                gif.seek(i)
                self._original.append(gif.convert("RGBA").copy())
                self._durations.append(gif.info.get("duration", 100) or 100)
        except Exception as exc:
            self.statusBar().showMessage(f"⚠ Error: {exc}")
            return

        name = os.path.basename(path)
        self.fname.setText(name)
        self.statusBar().showMessage(
            f"✓ Loaded {name} — {len(self._original)} frames"
        )
        self._play()

    def _load_video(self, path: str) -> None:
        """Extract frames from a video file using OpenCV."""
        if not _check_cv2():
            self.statusBar().showMessage(
                "⚠ Install opencv-python:  pip install opencv-python"
            )
            return

        import cv2

        self._stop()
        self._original.clear()
        self._processed = None
        self._durations.clear()

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            self.statusBar().showMessage(f"⚠ Could not open {os.path.basename(path)}")
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        ms = max(1, int(1000 / fps))

        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
            self._original.append(Image.fromarray(rgba, "RGBA"))
            self._durations.append(ms)
        cap.release()

        if not self._original:
            self.statusBar().showMessage("⚠ No frames extracted")
            return

        name = os.path.basename(path)
        self.fname.setText(name)
        n = len(self._original)
        msg = f"✓ Loaded {name} — {n} frames @ {fps:.0f} fps"
        if n > 500:
            msg += "  ⚠ high frame count — may use significant memory"
        self.statusBar().showMessage(msg)
        self._play()

    # ──────────────────────────────────────────────────────
    #  PLAYBACK  (QTimer-driven)
    # ──────────────────────────────────────────────────────

    def _play(self) -> None:
        if not self._frames:
            return
        self._idx = 0
        self._show(0)
        d = self._durations[0] if self._durations else 100
        self._timer.start(max(1, int(d / self._speed)))

    def _stop(self) -> None:
        self._timer.stop()

    def _advance(self) -> None:
        frames = self._frames
        if not frames:
            return
        self._idx = (self._idx + 1) % len(frames)
        d = self._durations[self._idx] if self._idx < len(self._durations) else 100
        self._timer.setInterval(max(1, int(d / self._speed)))
        self._show(self._idx)

    def _show(self, idx: int) -> None:
        """Push frame *idx* to both the overlay and the preview."""
        frames = self._frames
        if not frames or idx >= len(frames):
            return
        px = pil_to_qpixmap(frames[idx])
        self.overlay.gif_label.setPixmap(px)
        scaled = px.scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(scaled)

    # ──────────────────────────────────────────────────────
    #  SPEED & SCALE
    # ──────────────────────────────────────────────────────

    def _on_speed(self, val: int) -> None:
        self._speed = val / 100.0
        self.spd_lbl.setText(f"{self._speed:.1f}×")

    def _on_scale(self, val: int) -> None:
        self.scale_lbl.setText(f"{val}%")
        if not self._frames:
            return
        
        frame = self._frames[0]
        base_w, base_h = frame.size
        
        new_w = max(32, int(base_w * (val / 100.0)))
        new_h = max(32, int(base_h * (val / 100.0)))
        
        self.overlay.resize(new_w, new_h)

    # ──────────────────────────────────────────────────────
    #  CHROMA-KEY SETTINGS
    # ──────────────────────────────────────────────────────

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(self._chroma_color, self, "Key Colour")
        if c.isValid():
            self._chroma_color = c
            self._refresh_swatch()
            self.statusBar().showMessage(
                f"Key colour → RGB({c.red()}, {c.green()}, {c.blue()})"
            )

    def _toggle_eye(self) -> None:
        self._eyedropper = not self._eyedropper
        if self._eyedropper:
            self.preview.setCursor(Qt.CursorShape.CrossCursor)
            self.statusBar().showMessage("🎯 Click on the preview to sample a colour…")
        else:
            self.preview.setCursor(Qt.CursorShape.ArrowCursor)

    def _on_tol(self, v: int) -> None:
        self._chroma_tol = v
        self.tol_lbl.setText(str(v))

    def _refresh_swatch(self) -> None:
        c = self._chroma_color
        self.swatch.setStyleSheet(
            f"QPushButton#swatch{{background:rgb({c.red()},{c.green()},{c.blue()});"
            f"border:2px solid rgba(138,92,246,0.35);border-radius:8px;}}"
            f"QPushButton#swatch:hover{{border-color:rgba(138,92,246,0.75);}}"
        )

    # ── eyedropper event filter ───────────────────────────

    def eventFilter(self, obj, ev):
        if obj is self.preview and self._eyedropper and ev.type() == QEvent.Type.MouseButtonPress:
            px = self.preview.pixmap()
            if px and not px.isNull():
                lw, lh = self.preview.width(), self.preview.height()
                pw, ph = px.width(), px.height()
                ix = ev.pos().x() - (lw - pw) // 2
                iy = ev.pos().y() - (lh - ph) // 2
                img = px.toImage()
                if 0 <= ix < img.width() and 0 <= iy < img.height():
                    c = QColor(img.pixel(ix, iy))
                    self._chroma_color = c
                    self._refresh_swatch()
                    self.statusBar().showMessage(
                        f"Sampled RGB({c.red()}, {c.green()}, {c.blue()})"
                    )
            self._eyedropper = False
            self.preview.setCursor(Qt.CursorShape.ArrowCursor)
            return True
        return super().eventFilter(obj, ev)

    # ──────────────────────────────────────────────────────
    #  BATCH PROCESSING
    # ──────────────────────────────────────────────────────

    def _apply_chroma(self) -> None:
        if not self._original:
            self.statusBar().showMessage("⚠ Load a file first")
            return
        self._start_worker("chroma")

    def _apply_rembg(self) -> None:
        if not self._original:
            self.statusBar().showMessage("⚠ Load a file first")
            return
        if not _check_rembg():
            self.statusBar().showMessage("⚠ pip install rembg")
            return
        self._start_worker("rembg")

    def _reset_frames(self) -> None:
        self._processed = None
        self._play()
        self.statusBar().showMessage("↩ Reset to original frames")

    def _start_worker(self, method: str) -> None:
        self._stop()
        self._set_ui_busy(True)
        self.progress.setValue(0)
        self.progress.setVisible(True)

        c = self._chroma_color
        w = ProcessingWorker(
            [f.copy() for f in self._original],
            method,
            chroma_r=c.red(), chroma_g=c.green(), chroma_b=c.blue(),
            chroma_tol=self._chroma_tol,
        )
        w.progress.connect(self._on_prog)
        w.finished.connect(self._on_done)
        w.error.connect(self._on_err)
        self._worker = w
        w.start()

    def _on_prog(self, cur: int, tot: int) -> None:
        self.progress.setValue(int(cur / tot * 100))
        self.statusBar().showMessage(f"Processing frame {cur}/{tot} …")

    def _on_done(self, results: list) -> None:
        self._processed = results
        self.progress.setVisible(False)
        self._set_ui_busy(False)
        self._worker = None
        self._play()
        self.statusBar().showMessage(
            f"✓ Background removed from {len(results)} frames"
        )

    def _on_err(self, msg: str) -> None:
        self.progress.setVisible(False)
        self._set_ui_busy(False)
        self._worker = None
        self._play()
        self.statusBar().showMessage(f"⚠ Error: {msg}")

    def _save_processed(self) -> None:
        if not self._processed:
            self.statusBar().showMessage("⚠ No processed frames to save. Process a file first.")
            return

        out_dir = os.path.join(os.getcwd(), "processed")
        os.makedirs(out_dir, exist_ok=True)
        
        base_name = os.path.splitext(self.fname.text())[0] if self.fname.text() else "output"
        out_path = os.path.join(out_dir, f"{base_name}_processed.gif")
        
        self._set_ui_busy(True)
        self.statusBar().showMessage(f"Saving to {out_path} ...")
        QApplication.processEvents()  # Force UI update
        
        try:
            durations = self._durations[:len(self._processed)]
            self._processed[0].save(
                out_path,
                save_all=True,
                append_images=self._processed[1:],
                duration=durations,
                loop=0,
                disposal=2,  # Important for transparency replacement
                optimize=False
            )
            self.statusBar().showMessage(f"✓ Saved to {os.path.basename(out_dir)}/{os.path.basename(out_path)}")
        except Exception as e:
            self.statusBar().showMessage(f"⚠ Error saving: {str(e)}")
        finally:
            self._set_ui_busy(False)

    def _set_ui_busy(self, busy: bool) -> None:
        enabled = not busy
        self.sel_btn.setEnabled(enabled)
        self.chroma_btn.setEnabled(enabled)
        self.ai_btn.setEnabled(enabled and _check_rembg())
        self.reset_btn.setEnabled(enabled)
        self.save_btn.setEnabled(enabled)

    # ──────────────────────────────────────────────────────
    #  LOCK & VISIBILITY
    # ──────────────────────────────────────────────────────

    def _on_lock(self, checked: bool) -> None:
        self.lock_btn.setText("🔒 Locked" if checked else "🔓 Unlocked")
        self.overlay.set_locked(checked)
        self.statusBar().showMessage(
            "🔒 Overlay locked — click-through" if checked
            else "🔓 Overlay unlocked — drag to reposition"
        )

    def _on_vis(self, checked: bool) -> None:
        if checked:
            self.vis_btn.setText("👁 Visible"); self.overlay.show()
        else:
            self.vis_btn.setText("🚫 Hidden"); self.overlay.hide()

    # ──────────────────────────────────────────────────────
    #  CLEANUP
    # ──────────────────────────────────────────────────────

    def closeEvent(self, ev) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        self._stop()
        self._original.clear()
        self._processed = None
        self.overlay.close()
        super().closeEvent(ev)


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    panel = ControlPanel()
    panel.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
