"""Digitizing: the probe tip in the reference frame, captured when held still.

Every frame where both the reference board and the probe board are found gives one
tip position in the reference frame:  tip_ref = inverse(ref) * probe * tip.
When the last N tip positions stay within a small spread, they are averaged into a
captured point. The tip must then move away before the next capture. With known
targets (e.g. crosshairs on a screen), each point is matched to the nearest target
and its error is reported.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .boards import BoardSpec
from .calibration import CameraCalibration
from .detect import BoardDetector
from .pose import Pose, estimate_pose, relative_pose
from .probe import ProbeSpec


@dataclass
class TipSample:
    frame: str
    tip: np.ndarray  # reference frame, mm
    ref: Pose
    probe: Pose


@dataclass
class CapturedPoint:
    number: int
    samples: list[TipSample]
    target: str | None = None
    target_xyz: np.ndarray | None = None

    @property
    def name(self) -> str:
        return self.target or f"P{self.number}"

    @property
    def xyz(self) -> np.ndarray:
        return np.mean([s.tip for s in self.samples], axis=0)

    @property
    def spread(self) -> float:
        p = np.array([s.tip for s in self.samples])
        return float(np.sqrt(np.mean(np.sum((p - p.mean(axis=0)) ** 2, axis=1))))

    @property
    def error(self) -> np.ndarray | None:
        return None if self.target_xyz is None else self.xyz - self.target_xyz


@dataclass
class DigitizeStep:
    state: str  # "none", "moving", "holding", "captured", "move"
    sample: TipSample | None = None
    reason: str = ""
    progress: float = 0.0
    point: CapturedPoint | None = None
    nearest: tuple[str, float] | None = None  # nearest target and distance (mm)


@dataclass
class DigitizeSession:
    calib: CameraCalibration
    reference: BoardSpec
    probe: ProbeSpec
    targets: dict[str, np.ndarray] = field(default_factory=dict)
    frames_per_point: int = 15
    still_mm: float = 1.5  # RMS spread of the window that counts as held still
    jump_mm: float = 4.0  # a sample this far from the window mean restarts the window
    rearm_mm: float = 10.0  # move this far from the last point before the next capture
    snap_mm: float = 15.0  # a point within this of a target is matched to it
    max_rms_px: float = 1.0
    samples: list[tuple[int, TipSample]] = field(default_factory=list)  # (point number or 0, sample)
    points: list[CapturedPoint] = field(default_factory=list)

    def __post_init__(self):
        if self.probe.tip.offset is None:
            raise ValueError("the probe tip is not calibrated (run pivot first)")
        self.tip_offset = np.asarray(self.probe.tip.offset, dtype=np.float64)
        self.ref_detector = BoardDetector(self.reference)
        self.probe_detector = BoardDetector(self.probe.board)
        self.last_detections = (None, None)
        self._window: deque[TipSample] = deque(maxlen=self.frames_per_point)
        self._armed = True

    def locate(self, name: str, gray: np.ndarray) -> tuple[TipSample | None, str]:
        ref_det, probe_det = self.ref_detector.detect(gray), self.probe_detector.detect(gray)
        self.last_detections = (ref_det, probe_det)
        ref = estimate_pose(self.ref_detector, ref_det, self.calib)
        if ref is None:
            return None, "reference board not found"
        probe = estimate_pose(self.probe_detector, probe_det, self.calib)
        if probe is None:
            return None, "probe not found"
        for label, pose in (("reference", ref), ("probe", probe)):
            if pose.rms_px > self.max_rms_px:
                return None, f"{label} fit {pose.rms_px:.2f} px - glare or blur?"
        tip = relative_pose(ref, probe).apply(self.tip_offset)
        return TipSample(name, tip, ref, probe), ""

    def nearest_target(self, xyz: np.ndarray) -> tuple[str, float] | None:
        if not self.targets:
            return None
        # Distance in the screen plane (x, y): the tip touches the glass, a little in
        # front of the pixels, so z would only add a constant.
        name, d = min(((n, float(np.linalg.norm((xyz - t)[:2]))) for n, t in self.targets.items()), key=lambda v: v[1])
        return name, d

    def update(self, name: str, gray: np.ndarray) -> DigitizeStep:
        sample, why = self.locate(name, gray)
        if sample is None:
            self._window.clear()
            return DigitizeStep("none", reason=why)
        nearest = self.nearest_target(sample.tip)
        if not self._armed and np.linalg.norm(sample.tip - self.points[-1].xyz) > self.rearm_mm:
            self._armed = True
        if self._window and np.linalg.norm(sample.tip - np.mean([s.tip for s in self._window], axis=0)) > self.jump_mm:
            self._window.clear()
        self._window.append(sample)
        if not self._armed:
            self.samples.append((0, sample))
            return DigitizeStep("move", sample, reason="captured - move to the next point", nearest=nearest)
        if len(self._window) < self.frames_per_point:
            self.samples.append((0, sample))
            state = "holding" if len(self._window) > 1 else "moving"
            return DigitizeStep(state, sample, progress=len(self._window) / self.frames_per_point, nearest=nearest)
        window = list(self._window)
        p = np.array([s.tip for s in window])
        if np.sqrt(np.mean(np.sum((p - p.mean(axis=0)) ** 2, axis=1))) > self.still_mm:
            self._window.popleft()
            self.samples.append((0, sample))
            return DigitizeStep("holding", sample, progress=0.9, reason="hold steadier", nearest=nearest)
        point = CapturedPoint(len(self.points) + 1, window)
        near = self.nearest_target(point.xyz)
        if near is not None and near[1] <= self.snap_mm:
            point.target, point.target_xyz = near[0], self.targets[near[0]]
        self.points.append(point)
        # Tag the window's samples (the last len(window) - 1 logged ones plus this one).
        n = len(window) - 1
        if n:
            self.samples[-n:] = [(point.number, s) for _, s in self.samples[-n:]]
        self.samples.append((point.number, sample))
        self._window.clear()
        self._armed = False
        return DigitizeStep("captured", sample, progress=1.0, point=point, nearest=nearest)

    def summary(self) -> dict:
        matched = [p for p in self.points if p.error is not None]
        out: dict = {"points": len(self.points), "matched": len(matched)}
        if matched:
            e = np.array([p.error for p in matched])
            out["xy_rms"] = float(np.sqrt(np.mean(np.sum(e[:, :2] ** 2, axis=1))))
            out["z_mean"] = float(e[:, 2].mean())
            out["z_std"] = float(e[:, 2].std(ddof=1)) if len(e) > 1 else 0.0
            out["rms_3d"] = float(np.sqrt(np.mean(np.sum(e**2, axis=1))))
            ez = e.copy()
            ez[:, 2] -= out["z_mean"]  # remove the common glass offset
            out["rms_3d_offset_removed"] = float(np.sqrt(np.mean(np.sum(ez**2, axis=1))))
            out["worst_xy"] = float(np.max(np.linalg.norm(e[:, :2], axis=1)))
        return out
