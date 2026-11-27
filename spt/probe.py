"""The touch probe: its marker board, its tip, and the files that describe them.

Probe frame = the probe board's frame (mm): origin at the board's top-left outer
corner, x right, y down (towards the bottom of the phone), z into the board (behind
the screen). The tip is a point in this frame. Pivot calibration measures it.

A probe file (JSON) holds the board (with its true square size), how it sits on a
screen if it is shown on one, and the tip.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .boards import PROBE, BoardSpec, render_raster

FORMAT = 1


@dataclass
class Tip:
    kind: str = "sharp"  # "sharp" or "ball"
    radius_mm: float = 0.0  # ball radius; the tip point is the ball centre
    offset: list[float] | None = None  # tip in the probe frame (mm); None until calibrated
    guess: list[float] | None = None  # rough hand-measured position, for sanity checks
    rms_mm: float | None = None  # pivot calibration residual
    samples: int = 0


@dataclass
class ProbeSpec:
    name: str
    board: BoardSpec
    tip: Tip = field(default_factory=Tip)
    screen: dict = field(default_factory=dict)  # width_px, height_px, square_px, origin_px, pitch_mm

    def to_json(self) -> dict:
        b = self.board
        return {
            "format": FORMAT,
            "name": self.name,
            "board": {"squares_x": b.squares_x, "squares_y": b.squares_y, "square_mm": b.square_mm,
                      "marker_mm": b.marker_mm, "first_id": b.first_id},
            "screen": self.screen,
            "tip": self.tip.__dict__,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ProbeSpec":
        if data.get("format") != FORMAT:
            raise ValueError(f"unsupported probe format: {data.get('format')}")
        b = data["board"]
        board = BoardSpec("probe", b["squares_x"], b["squares_y"], b["square_mm"], b["marker_mm"], b["first_id"])
        return cls(data["name"], board, Tip(**data.get("tip", {})), data.get("screen", {}))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "ProbeSpec":
        return cls.from_json(json.loads(Path(path).read_text()))

    def with_square_mm(self, square_mm: float) -> "ProbeSpec":
        """The probe after measuring the real (displayed) square size."""
        spec = ProbeSpec(self.name, self.board.scaled(square_mm), self.tip, dict(self.screen))
        if spec.screen:
            spec.screen["pitch_mm"] = square_mm / spec.screen["square_px"]
        return spec


def screen_probe(name: str, width_px: int, height_px: int, pitch_mm: float,
                 square_px: int | None = None, margin_px: int = 40) -> ProbeSpec:
    """A probe whose board is shown on a phone screen (portrait), as large as fits."""
    sx, sy = PROBE.squares_x, PROBE.squares_y
    if square_px is None:
        square_px = min((width_px - 2 * margin_px) // sx, (height_px - 2 * margin_px) // sy)
    origin = ((width_px - square_px * sx) // 2, (height_px - square_px * sy) // 2)
    board = PROBE.scaled(square_px * pitch_mm)
    return ProbeSpec(name, board, screen={
        "width_px": width_px, "height_px": height_px, "square_px": square_px,
        "origin_px": list(origin), "pitch_mm": pitch_mm,
    })


def screen_image(probe: ProbeSpec) -> np.ndarray:
    """The full-screen image to show on the phone: board centred on white, plus a
    small label below it saying what to measure."""
    s = probe.screen
    img = np.full((s["height_px"], s["width_px"]), 255, dtype=np.uint8)
    raster = render_raster(probe.board, px_per_mm=s["square_px"] / probe.board.square_mm)
    x, y = s["origin_px"]
    img[y:y + raster.shape[0], x:x + raster.shape[1]] = raster
    sq = s["square_px"]
    below = y + raster.shape[0]
    lines = [
        f"SPT probe board, IDs {probe.board.ids[0]}-{probe.board.ids[-1]}",
        f"measure inner corners: {probe.board.squares_y - 2} squares down,",
        f"{probe.board.squares_x - 2} squares across ({sq} px each)",
    ]
    for i, text in enumerate(lines):
        cv2.putText(img, text, (x, below + 45 + 36 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 120, 2, cv2.LINE_AA)
    return img
