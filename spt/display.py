"""Showing boards on a monitor instead of printing them.

A flat screen is flatter than paper on a backing, and its pixel grid is exact. With
both boards on one screen the target's true position in the reference frame is known
from pixel positions alone ("truth"). An error in the assumed pixel pitch scales the
boards and the truth together, so it cancels out of the accuracy test.
"""

from __future__ import annotations

import ctypes
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .boards import BOARDS, REFERENCE, TARGET, BoardSpec, render_raster

LAYOUTS = ("measure", "calib", "reference", "target", "touch")
FORMAT = 1


@dataclass
class Monitor:
    index: int  # n of \\.\DISPLAYn
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool


def set_dpi_aware() -> None:
    """Work in physical pixels, so Windows display scaling can't resize the boards."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def list_monitors() -> list[Monitor]:
    if sys.platform != "win32":
        raise RuntimeError("monitor listing is only implemented on Windows")
    from ctypes import wintypes

    set_dpi_aware()

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    monitors: list[Monitor] = []
    proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    def callback(hmon, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        digits = re.findall(r"\d+", info.szDevice)
        monitors.append(Monitor(
            int(digits[-1]) if digits else len(monitors) + 1, info.szDevice,
            r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1),
        ))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    return sorted(monitors, key=lambda m: m.index)


def pitch_from_diagonal(diagonal_in: float, width_px: int, height_px: int) -> float:
    return diagonal_in * 25.4 / math.hypot(width_px, height_px)


@dataclass
class PlacedBoard:
    spec: BoardSpec  # square_mm = square_px * pitch
    square_px: int
    origin_px: tuple[int, int]  # top-left outer corner of the pattern on screen


@dataclass
class Scene:
    width_px: int
    height_px: int
    pitch_mm: float
    layout: str
    boards: list[PlacedBoard]
    monitor: str = ""
    # Touch targets: name -> (x, y) screen position in pixel-edge coordinates (the
    # point between pixels), so the crosshair centre is exact.
    targets: dict[str, tuple[int, int]] = field(default_factory=dict)

    def board(self, name: str) -> PlacedBoard | None:
        return next((b for b in self.boards if b.spec.name == name), None)

    def image(self) -> np.ndarray:
        img = np.full((self.height_px, self.width_px), 255, dtype=np.uint8)
        for b in self.boards:
            raster = render_raster(b.spec, px_per_mm=b.square_px / b.spec.square_mm)
            x, y = b.origin_px
            img[y:y + raster.shape[0], x:x + raster.shape[1]] = raster
        for name, (x, y) in self.targets.items():
            arm, gap = 45, 6
            # 2 px lines centred on the pixel edge at (x, y), with a small gap so the
            # exact centre stays visible.
            img[y - 1:y + 1, x - arm:x - gap] = 0
            img[y - 1:y + 1, x + gap:x + arm] = 0
            img[y - arm:y - gap, x - 1:x + 1] = 0
            img[y + gap:y + arm, x - 1:x + 1] = 0
            cv2.circle(img, (x, y), 28, 0, 2, cv2.LINE_AA)
            cv2.putText(img, name, (x + 32, y - 32), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2, cv2.LINE_AA)
        return img

    def target_points(self) -> dict[str, np.ndarray]:
        """Touch targets in the reference-board frame (mm). They lie on the pixel
        plane (z = 0); a tip touching the cover glass sits slightly in front of it
        (negative z)."""
        ref = self.board("reference")
        if ref is None:
            return {}
        return {
            name: np.array([(x - ref.origin_px[0]) * self.pitch_mm, (y - ref.origin_px[1]) * self.pitch_mm, 0.0])
            for name, (x, y) in self.targets.items()
        }

    def truth(self, ref_corner: int = 0, tgt_corner: int = 0) -> np.ndarray | None:
        """Target corner in the reference-board frame (mm), if both are on this screen.

        Screen x/y run the same way as board x/y, so the reference frame is the screen
        shifted to the reference board's origin, and z is 0 for everything on it.
        """
        ref, tgt = self.board("reference"), self.board("target")
        if ref is None or tgt is None:
            return None
        offset = (np.subtract(tgt.origin_px, ref.origin_px)) * self.pitch_mm
        return np.array([offset[0], offset[1], 0.0]) + tgt.spec.corner_xyz(tgt_corner)

    def known_mm(self, ref_corner: int = 0, tgt_corner: int = 0) -> float | None:
        t = self.truth(ref_corner, tgt_corner)
        if t is None:
            return None
        ref = self.board("reference")
        return float(np.linalg.norm(t - ref.spec.corner_xyz(ref_corner)))

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "layout": self.layout,
            "monitor": self.monitor,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "pitch_mm": self.pitch_mm,
            "boards": [
                {"name": b.spec.name, "square_px": b.square_px, "square_mm": b.spec.square_mm,
                 "origin_px": list(b.origin_px)}
                for b in self.boards
            ],
            "truth_mm": None if self.truth() is None else self.truth().tolist(),
            "known_mm": self.known_mm(),
            "targets": {k: list(v) for k, v in self.targets.items()},
            "target_points_mm": {k: v.tolist() for k, v in self.target_points().items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> "Scene":
        if data.get("format") != FORMAT:
            raise ValueError(f"unsupported scene format: {data.get('format')}")
        pitch = data["pitch_mm"]
        boards = [
            PlacedBoard(BOARDS[b["name"]].scaled(b["square_px"] * pitch), b["square_px"], tuple(b["origin_px"]))
            for b in data["boards"]
        ]
        targets = {k: tuple(v) for k, v in data.get("targets", {}).items()}
        return cls(data["width_px"], data["height_px"], pitch, data["layout"], boards, data.get("monitor", ""), targets)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Scene":
        return cls.from_json(json.loads(Path(path).read_text()))


def make_scene(width_px: int, height_px: int, pitch_mm: float, layout: str, monitor: str = "") -> Scene:
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}")
    m = max(16, round(0.04 * height_px))  # white margin (quiet zone) around boards

    def placed(spec: BoardSpec, sq: int, x: int, y: int) -> PlacedBoard:
        return PlacedBoard(spec.scaled(sq * pitch_mm), sq, (x, y))

    def centred(spec: BoardSpec) -> list[PlacedBoard]:
        sq = min((width_px - 2 * m) // spec.squares_x, (height_px - 2 * m) // spec.squares_y)
        x = (width_px - sq * spec.squares_x) // 2
        y = (height_px - sq * spec.squares_y) // 2
        return [placed(spec, sq, x, y)]

    targets: dict[str, tuple[int, int]] = {}
    if layout == "touch":
        # Reference board in the left ~45 % of the screen; touch targets to its right
        # and above / below it, spread out to test different distances.
        rsq = int(min((0.45 * width_px - 2 * m) / REFERENCE.squares_x, (0.6 * height_px) / REFERENCE.squares_y))
        bw, bh = rsq * REFERENCE.squares_x, rsq * REFERENCE.squares_y
        rx, ry = m, (height_px - bh) // 2
        boards = [placed(REFERENCE, rsq, rx, ry)]
        cx = rx + bw // 2
        above, below = ry // 2, ry + bh + (height_px - ry - bh) // 2
        right = [round(width_px * f) for f in (0.6, 0.75, 0.9)]
        rows = [round(height_px * 0.25), round(height_px * 0.75)]
        names = iter(f"T{i}" for i in range(1, 20))
        for y in rows:
            for x in right:
                targets[next(names)] = (x, y)
        targets[next(names)] = (cx, above)
        targets[next(names)] = (cx, below)
    elif layout == "calib":
        boards = centred(REFERENCE)
    elif layout in ("reference", "target"):
        boards = centred(BOARDS[layout])
    else:
        # Reference at the left edge, target at the right edge, keeping the printed
        # size ratio between them (target square = 25/30 of the reference square).
        ratio = TARGET.square_mm / REFERENCE.square_mm
        by_width = (width_px - 3 * m) / (REFERENCE.squares_x + TARGET.squares_x * ratio)
        by_height = (height_px - 2 * m) / REFERENCE.squares_y
        rsq = int(min(by_width, by_height))
        tsq = round(rsq * ratio)
        ry = (height_px - rsq * REFERENCE.squares_y) // 2
        ty = (height_px - tsq * TARGET.squares_y) // 2
        boards = [
            placed(REFERENCE, rsq, m, ry),
            placed(TARGET, tsq, width_px - m - tsq * TARGET.squares_x, ty),
        ]
    return Scene(width_px, height_px, pitch_mm, layout, boards, monitor, targets)


def show(scene: Scene, monitor: Monitor, seconds: float | None = None) -> tuple[int, int]:
    """Full-screen the scene on the monitor until Esc/q (or `seconds`).

    Returns the window's drawable size, which must equal the scene size for the
    boards to be shown 1:1.
    """
    set_dpi_aware()
    name = "Spatial Point Tracker - Esc to close"
    img = scene.image()
    cv2.namedWindow(name, cv2.WINDOW_NORMAL)
    cv2.moveWindow(name, monitor.x + 50, monitor.y + 50)
    cv2.setWindowProperty(name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cv2.imshow(name, img)
    for _ in range(5):
        cv2.waitKey(30)
    _, _, w, h = cv2.getWindowImageRect(name)
    limit = None if seconds is None else int(seconds * 1000)
    waited = 0
    while True:
        key = cv2.waitKey(100)
        waited += 100
        if key in (27, ord("q")):
            break
        if cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE) < 1:
            break
        if limit is not None and waited >= limit:
            break
    cv2.destroyWindow(name)
    return w, h
