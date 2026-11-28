"""Guided live calibration: frames are captured automatically while the user moves
the board in front of a fixed camera.

A frame is kept only when the board is held still (corners stop moving), enough of
it is visible, and the view adds something: a part of the frame not yet covered, or
a tilt not yet seen in that part of the frame. After a few views the camera is
recalibrated on every capture; a view that fits much worse than the rest (blur,
glare) is dropped again. Calibration is "done" when enough views, frame coverage,
tilt variety and a low error are all reached.

The logic here is independent of cameras and windows so it can be tested with
synthetic frames; spt.cli runs it on a live camera.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import cv2
import numpy as np

from .boards import BoardSpec
from .calibration import CameraCalibration, calibrate
from .detect import BoardDetector, Detection


@dataclass
class GuideConfig:
    grid: tuple[int, int] = (8, 6)
    min_corners: int = 12  # of the board's inner corners
    still_px: float = 1.0  # median corner motion per frame counted as "still"
    still_frames: int = 6  # consecutive still frames before a capture
    rearm_px: float = 4.0  # motion that counts as "moved on" after a capture
    min_interval: float = 0.6  # seconds between captures
    novel_tilt_deg: float = 10.0  # tilt difference that makes a view new in a region
    calibrate_after: int = 6  # views before the first calibration
    reject_factor: float = 3.0  # drop a view whose error > factor * median (and > 1 px)
    min_views: int = 15
    target_coverage: float = 0.8
    max_rms: float = 0.6
    rational: bool = False  # 8-term distortion model, for wide lenses

    def for_stream(self, width: int, fps: float) -> "GuideConfig":
        """The same steadiness for another image width and frame rate.

        The pixel thresholds above are per frame at 1920 px wide and 30 fps (the
        webcams). Motion per frame grows with resolution and with the time between
        frames, and the hold should last the same time (6 frames = 0.2 s at 30 fps).
        """
        k = (width / 1920.0) * (30.0 / fps)
        return replace(self, still_px=self.still_px * k, rearm_px=self.rearm_px * k,
                       still_frames=max(2, round(self.still_frames * fps / 30.0)))
    min_tilt_deg: float = 20.0  # need views at least this tilted, both left/right and up/down


@dataclass
class View:
    detection: Detection
    tilt: tuple[float, float]  # (about vertical axis, about horizontal axis) degrees
    region: tuple[int, int]  # 3x3 region of the frame holding the board centre
    cells: set[tuple[int, int]] = field(default_factory=set)


@dataclass
class Step:
    """What happened with one frame, for the UI."""
    state: str  # "none", "few", "moving", "holding", "duplicate", "captured", "rejected"
    still: float = 0.0  # 0..1 progress of the hold-still count
    message: str = ""


class GuidedCalibrator:
    def __init__(self, spec: BoardSpec, image_size: tuple[int, int], config: GuideConfig | None = None):
        self.spec = spec
        self.detector = BoardDetector(spec)
        self.image_size = image_size
        self.cfg = config or GuideConfig()
        self.views: list[View] = []
        self.calibration: CameraCalibration | None = None
        self._prev: Detection | None = None
        self._still_count = 0
        self._armed = True
        self._last_capture = -1e9
        w, h = image_size
        # Rough pinhole guess for tilt estimates until a calibration exists.
        self._guess = np.array([[0.8 * w, 0, w / 2], [0, 0.8 * w, h / 2], [0, 0, 1.0]])

    # ----- per-frame -----------------------------------------------------
    def update(self, det: Detection | None, t: float) -> Step:
        cfg = self.cfg
        if det is None or len(det) < cfg.min_corners:
            self._prev, self._still_count = None, 0
            return Step("none" if det is None else "few",
                        message="show the board to the camera" if det is None else "more of the board in view")
        motion = self._motion(det)
        self._prev = det
        if motion is None or motion > cfg.still_px:
            self._still_count = 0
            if motion is not None and motion > cfg.rearm_px:
                self._armed = True
            return Step("moving")
        self._still_count += 1
        progress = min(1.0, self._still_count / cfg.still_frames)
        if self._still_count < cfg.still_frames:
            return Step("holding", progress, "hold still")
        if not self._armed or t - self._last_capture < cfg.min_interval:
            return Step("duplicate", 1.0, "captured - move to a new spot or angle")
        view = self._make_view(det)
        if not self._is_new(view):
            self._armed = False
            return Step("duplicate", 1.0, "already have this view - move or tilt the board")
        self._armed = False
        self._last_capture = t
        self.views.append(view)
        if len(self.views) >= cfg.calibrate_after:
            rejected = self._recalibrate()
            if rejected:
                return Step("rejected", 1.0, "that view was blurry or glared - dropped")
        return Step("captured", 1.0, "captured")

    def _motion(self, det: Detection) -> float | None:
        if self._prev is None:
            return None
        common, a, b = np.intersect1d(self._prev.ids, det.ids, return_indices=True)
        if len(common) < 4:
            return None
        return float(np.median(np.linalg.norm(self._prev.corners[a] - det.corners[b], axis=1)))

    # ----- views ---------------------------------------------------------
    def _cells(self, pts: np.ndarray) -> set[tuple[int, int]]:
        gx, gy = self.cfg.grid
        w, h = self.image_size
        xs = np.clip((pts[:, 0] / w * gx).astype(int), 0, gx - 1)
        ys = np.clip((pts[:, 1] / h * gy).astype(int), 0, gy - 1)
        return set(zip(xs.tolist(), ys.tolist()))

    def _make_view(self, det: Detection) -> View:
        obj, img = self.detector.match(det)
        K = self.calibration.camera_matrix if self.calibration else self._guess
        dist = self.calibration.dist_coeffs if self.calibration else np.zeros(5)
        ok, rvec, _ = cv2.solvePnP(obj, img, K, dist, flags=cv2.SOLVEPNP_IPPE)
        R, _ = cv2.Rodrigues(rvec)
        n = R[:, 2]  # board normal in camera coordinates
        tilt = (float(np.degrees(np.arctan2(n[0], n[2]))), float(np.degrees(np.arctan2(n[1], n[2]))))
        w, h = self.image_size
        cx, cy = det.corners.mean(axis=0)
        region = (min(2, int(cx / w * 3)), min(2, int(cy / h * 3)))
        return View(det, tilt, region, self._cells(det.corners))

    def _is_new(self, view: View) -> bool:
        if view.cells - self.covered_cells:
            return True
        for other in self.views:
            if other.region == view.region and max(
                abs(other.tilt[0] - view.tilt[0]), abs(other.tilt[1] - view.tilt[1])
            ) < self.cfg.novel_tilt_deg:
                return False
        return True

    def _recalibrate(self) -> bool:
        """Calibrate on all views; drop the newest if it fits much worse. True if dropped."""
        calib = self._run_calibration()
        errors = [v["error_px"] for v in calib.views]
        median = float(np.median(errors))
        if errors[-1] > max(1.0, self.cfg.reject_factor * median) and len(self.views) > self.cfg.calibrate_after:
            self.views.pop()
            self.calibration = self._run_calibration()
            return True
        self.calibration = calib
        return False

    def _run_calibration(self) -> CameraCalibration:
        named = [(f"view{i:02d}", v.detection) for i, v in enumerate(self.views)]
        return calibrate(named, self.spec, self.image_size, rational=self.cfg.rational, min_corners=self.cfg.min_corners)

    # ----- progress ------------------------------------------------------
    @property
    def covered_cells(self) -> set[tuple[int, int]]:
        cells: set[tuple[int, int]] = set()
        for v in self.views:
            cells |= v.cells
        return cells

    @property
    def coverage(self) -> float:
        gx, gy = self.cfg.grid
        return len(self.covered_cells) / (gx * gy)

    @property
    def tilt_range(self) -> tuple[float, float]:
        """Largest absolute tilt seen about each axis."""
        if not self.views:
            return 0.0, 0.0
        return (max(abs(v.tilt[0]) for v in self.views), max(abs(v.tilt[1]) for v in self.views))

    @property
    def rms(self) -> float | None:
        return self.calibration.rms_px if self.calibration else None

    def done(self) -> bool:
        cfg = self.cfg
        tx, ty = self.tilt_range
        return (
            len(self.views) >= cfg.min_views
            and self.coverage >= cfg.target_coverage
            and tx >= cfg.min_tilt_deg and ty >= cfg.min_tilt_deg
            and self.rms is not None and self.rms <= cfg.max_rms
        )

    def hint(self) -> str:
        cfg = self.cfg
        if self.coverage < cfg.target_coverage:
            return "move the board over the RED cells"
        tx, ty = self.tilt_range
        if tx < cfg.min_tilt_deg:
            return "turn the board more to the left / right"
        if ty < cfg.min_tilt_deg:
            return "tilt the board more forward / back"
        if self.rms is not None and self.rms > cfg.max_rms:
            return "error is high - hold steadier, avoid glare"
        if len(self.views) < cfg.min_views:
            return "keep going: new spots and angles"
        return "DONE - press q to save (or keep adding views)"
