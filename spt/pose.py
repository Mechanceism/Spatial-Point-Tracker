"""Board pose estimation and rigid transforms."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .calibration import CameraCalibration
from .detect import BoardDetector, Detection

MIN_POSE_CORNERS = 6


@dataclass
class Pose:
    """Board-to-camera transform: X_cam = R @ X_board + t (mm)."""

    R: np.ndarray
    t: np.ndarray
    rms_px: float = 0.0
    corners: int = 0

    def inverse(self) -> "Pose":
        return Pose(self.R.T, -self.R.T @ self.t, self.rms_px, self.corners)

    def compose(self, other: "Pose") -> "Pose":
        """Apply `other` first, then `self`."""
        return Pose(self.R @ other.R, self.R @ other.t + self.t)

    def apply(self, xyz: np.ndarray) -> np.ndarray:
        return self.R @ np.asarray(xyz, dtype=np.float64) + self.t


def relative_pose(frame: Pose, other: Pose) -> Pose:
    """Pose of `other` expressed in `frame`'s coordinates (both seen by one camera)."""
    return frame.inverse().compose(other)


def estimate_pose(
    detector: BoardDetector,
    det: Detection | None,
    calib: CameraCalibration,
    min_corners: int = MIN_POSE_CORNERS,
) -> Pose | None:
    if det is None or len(det) < min_corners:
        return None
    obj, img = detector.match(det)
    K, dist = calib.camera_matrix, calib.dist_coeffs
    # IPPE is the robust solver for planar targets; LM then polishes the result.
    ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    rvec, tvec = cv2.solvePnPRefineLM(obj, img, K, dist, rvec, tvec)
    projected, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
    rms = float(np.sqrt(np.mean(np.sum((projected.reshape(-1, 2) - img) ** 2, axis=1))))
    R, _ = cv2.Rodrigues(rvec)
    return Pose(R, tvec.reshape(3).astype(np.float64), rms, len(obj))
