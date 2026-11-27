"""Live measurement test: phase 0's question asked with a live camera and averaging.

Every frame gives one measurement of the target corner in the reference frame.
When the boards are held still, frames are averaged into a *capture*. Comparing
three levels against the exact truth shows where the error comes from:

- single frames            (all error sources)
- one capture (N frames)   (averages out frame-to-frame noise only)
- mean over captures       (also averages over different viewpoints)

Both boards are on one rigid screen, so hand tremor does not change the measured
relative position; holding still only needs to avoid motion blur.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .measure import PointMeasurer, ViewResult


@dataclass
class Capture:
    number: int
    results: list[ViewResult]

    @property
    def points(self) -> np.ndarray:
        return np.array([r.point for r in self.results])

    @property
    def mean(self) -> np.ndarray:
        return self.points.mean(axis=0)

    @property
    def frame_spread(self) -> float:
        """RMS distance of this capture's frames from their mean (frame noise)."""
        p = self.points
        return float(np.sqrt(np.mean(np.sum((p - p.mean(axis=0)) ** 2, axis=1))))


@dataclass
class LiveStep:
    state: str  # "none", "moving", "collecting", "captured", "move"
    result: ViewResult | None = None
    reason: str = ""
    progress: float = 0.0
    capture: Capture | None = None


@dataclass
class LiveMeasureSession:
    measurer: PointMeasurer
    truth: np.ndarray | None = None
    frames_per_capture: int = 15
    still_mm: float = 2.0  # reference-board motion per frame that still counts as still
    rearm_mm: float = 20.0  # move at least this far (or turn 5 deg) before the next capture
    rearm_deg: float = 5.0
    frames: list[tuple[int, ViewResult]] = field(default_factory=list)  # (capture number or 0, result)
    captures: list[Capture] = field(default_factory=list)

    def __post_init__(self):
        self._collecting: list[ViewResult] = []
        self._prev = None
        self._anchor = None  # reference pose of the last capture
        self._armed = True

    def update(self, name: str, gray: np.ndarray) -> LiveStep:
        result, why = self.measurer.measure(name, gray)
        if result is None:
            self._collecting, self._prev = [], None
            return LiveStep("none", reason=why)
        moving = self._prev is None or np.linalg.norm(result.ref.t - self._prev.ref.t) > self.still_mm
        self._prev = result
        if not self._armed and self._moved_from_anchor(result):
            self._armed = True
        if moving:
            self._collecting = []
            self.frames.append((0, result))
            return LiveStep("moving", result)
        if not self._armed:
            self.frames.append((0, result))
            return LiveStep("move", result, reason="captured - move or turn the screen for the next one")
        self._collecting.append(result)
        if len(self._collecting) < self.frames_per_capture:
            self.frames.append((0, result))
            return LiveStep("collecting", result, progress=len(self._collecting) / self.frames_per_capture)
        capture = Capture(len(self.captures) + 1, self._collecting)
        self.captures.append(capture)
        # Re-tag this capture's frames.
        n = len(self._collecting) - 1
        self.frames[len(self.frames) - n:] = [(capture.number, r) for _, r in self.frames[len(self.frames) - n:]]
        self.frames.append((capture.number, result))
        self._collecting = []
        self._anchor = result.ref
        self._armed = False
        return LiveStep("captured", result, progress=1.0, capture=capture)

    def _moved_from_anchor(self, result: ViewResult) -> bool:
        a = self._anchor
        if a is None:
            return True
        dt = np.linalg.norm(result.ref.t - a.t)
        cos = (np.trace(a.R.T @ result.ref.R) - 1) / 2
        angle = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))
        return dt > self.rearm_mm or angle > self.rearm_deg

    # ----- statistics ----------------------------------------------------
    def stats(self) -> dict:
        out: dict = {"frames": len(self.frames), "captures": len(self.captures)}
        if self.truth is None:
            return out
        if self.frames:
            err = np.array([r.point - self.truth for _, r in self.frames])
            out["frame_rms"] = float(np.sqrt(np.mean(np.sum(err**2, axis=1))))
            out["frame_rms_xyz"] = np.sqrt(np.mean(err**2, axis=0))
        if self.captures:
            cerr = np.array([c.mean - self.truth for c in self.captures])
            out["capture_rms"] = float(np.sqrt(np.mean(np.sum(cerr**2, axis=1))))
            out["capture_rms_xyz"] = np.sqrt(np.mean(cerr**2, axis=0))
            out["capture_worst"] = float(np.max(np.linalg.norm(cerr, axis=1)))
            out["frame_noise"] = float(np.sqrt(np.mean([c.frame_spread**2 for c in self.captures])))
            out["all_mean_error"] = cerr.mean(axis=0)
        return out
