"""Phase 0 measurement test: locate a target-board corner in the reference frame.

The reference board stands in for markers fixed to the car, the target board for
the probe. Every photo gives an independent estimate of the same physical point,
so the spread across photos is the repeatability, and comparing the distance to a
hand-measured one gives the accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .boards import BoardSpec
from .calibration import CameraCalibration
from .detect import BoardDetector
from .pose import Pose, estimate_pose, relative_pose


@dataclass
class ViewResult:
    name: str
    point: np.ndarray  # target corner in the reference frame (mm)
    distance_mm: float  # from the reference corner to that point
    ref: Pose
    tgt: Pose
    camera_distance_mm: float  # camera to reference-board origin


@dataclass
class Summary:
    views: int
    mean_point: np.ndarray
    std_xyz: np.ndarray
    std_3d: float  # RMS distance of the per-view points from their mean
    max_dev_3d: float
    mean_distance: float
    std_distance: float
    min_distance: float
    max_distance: float
    known_mm: float | None
    error_mm: float | None
    # Only when the true point is known exactly (boards shown on one screen).
    truth: np.ndarray | None = None
    mean_error: np.ndarray | None = None  # mean point - truth
    rms_error_3d: float | None = None  # per-view distance from truth, RMS
    max_error_3d: float | None = None


class PointMeasurer:
    def __init__(
        self,
        calib: CameraCalibration,
        ref_spec: BoardSpec,
        tgt_spec: BoardSpec,
        ref_corner: int = 0,
        tgt_corner: int = 0,
        max_rms_px: float = 1.0,
    ):
        self.calib = calib
        self.max_rms_px = max_rms_px
        self.ref = BoardDetector(ref_spec)
        self.tgt = BoardDetector(tgt_spec)
        self.ref_xyz = ref_spec.corner_xyz(ref_corner)
        self.tgt_xyz = tgt_spec.corner_xyz(tgt_corner)
        self.last_detections = (None, None)  # (reference, target) from the last measure()

    def measure(self, name: str, gray: np.ndarray) -> tuple[ViewResult | None, str]:
        """Result for one photo, or None and the reason it was skipped."""
        self.last_detections = (self.ref.detect(gray), self.tgt.detect(gray))
        ref = estimate_pose(self.ref, self.last_detections[0], self.calib)
        if ref is None:
            return None, "reference board not found"
        tgt = estimate_pose(self.tgt, self.last_detections[1], self.calib)
        if tgt is None:
            return None, "target board not found"
        # A board that does not fit its own pose well (bad calibration near the frame
        # edge, a bent board, blur) gives a confidently wrong point: reject it.
        for label, pose in (("reference", ref), ("target", tgt)):
            if pose.rms_px > self.max_rms_px:
                return None, f"{label} reprojection {pose.rms_px:.2f} px > {self.max_rms_px:g} px"
        point = relative_pose(ref, tgt).apply(self.tgt_xyz)
        return (
            ViewResult(
                name=name,
                point=point,
                distance_mm=float(np.linalg.norm(point - self.ref_xyz)),
                ref=ref,
                tgt=tgt,
                camera_distance_mm=float(np.linalg.norm(ref.t)),
            ),
            "",
        )


def summarize(
    results: list[ViewResult], known_mm: float | None = None, truth: np.ndarray | None = None
) -> Summary:
    if not results:
        raise ValueError("no usable views")
    pts = np.array([r.point for r in results])
    dists = np.array([r.distance_mm for r in results])
    mean = pts.mean(axis=0)
    dev = np.linalg.norm(pts - mean, axis=1)
    ddof = 1 if len(results) > 1 else 0
    mean_distance = float(dists.mean())
    truth_fields = {}
    if truth is not None:
        truth = np.asarray(truth, dtype=np.float64)
        err = np.linalg.norm(pts - truth, axis=1)
        truth_fields = dict(
            truth=truth,
            mean_error=mean - truth,
            rms_error_3d=float(np.sqrt(np.mean(err**2))),
            max_error_3d=float(err.max()),
        )
    return Summary(
        **truth_fields,
        views=len(results),
        mean_point=mean,
        std_xyz=pts.std(axis=0, ddof=ddof),
        std_3d=float(np.sqrt(np.mean(dev**2))),
        max_dev_3d=float(dev.max()),
        mean_distance=mean_distance,
        std_distance=float(dists.std(ddof=ddof)),
        min_distance=float(dists.min()),
        max_distance=float(dists.max()),
        known_mm=known_mm,
        error_mm=None if known_mm is None else mean_distance - known_mm,
    )
