"""Pivot calibration: find the probe tip by rotating the probe around it.

With the tip held in a small dent, every probe pose (R_i, t_i) satisfies
    R_i @ tip + t_i = P
where `tip` is the tip in the probe frame and P is the fixed dent position in the
camera frame. Stacking [R_i  -I] [tip; P] = -t_i over all poses gives a linear least
squares problem. The residual (how far each pose puts the tip from P) shows how good
the calibration is; it needs rotations in several directions to be well determined.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .pose import Pose


@dataclass
class PivotResult:
    tip: np.ndarray  # probe frame, mm
    pivot: np.ndarray  # camera frame (or whatever frame the poses are in), mm
    residuals: np.ndarray  # per pose, mm
    rms_mm: float
    spread_deg: float  # how widely the probe was rotated (largest angle between poses)
    condition: float  # condition number of the system (large = poorly determined)

    @property
    def samples(self) -> int:
        return len(self.residuals)


def solve_pivot(poses: list[Pose]) -> PivotResult:
    if len(poses) < 3:
        raise ValueError("need at least 3 poses")
    A = np.zeros((3 * len(poses), 6))
    b = np.zeros(3 * len(poses))
    for i, p in enumerate(poses):
        A[3 * i:3 * i + 3, :3] = p.R
        A[3 * i:3 * i + 3, 3:] = -np.eye(3)
        b[3 * i:3 * i + 3] = -p.t
    x, *_ = np.linalg.lstsq(A, b, rcond=None)
    tip, pivot = x[:3], x[3:]
    residuals = np.array([np.linalg.norm(p.R @ tip + p.t - pivot) for p in poses])
    return PivotResult(
        tip=tip,
        pivot=pivot,
        residuals=residuals,
        rms_mm=float(np.sqrt(np.mean(residuals**2))),
        spread_deg=rotation_spread(poses),
        condition=float(np.linalg.cond(A)),
    )


def solve_pivot_robust(poses: list[Pose], reject_factor: float = 3.0, min_keep: float = 0.7) -> tuple[PivotResult, list[int]]:
    """Solve, drop poses whose residual is far above the rest (tip slipped, bad
    detection), and solve again. Returns the result and the indices kept."""
    keep = list(range(len(poses)))
    result = solve_pivot(poses)
    for _ in range(3):
        median = float(np.median(result.residuals))
        limit = max(reject_factor * median, 0.3)
        new_keep = [keep[i] for i, r in enumerate(result.residuals) if r <= limit]
        if len(new_keep) == len(keep) or len(new_keep) < max(6, min_keep * len(poses)):
            break
        keep = new_keep
        result = solve_pivot([poses[i] for i in keep])
    return result, keep


def rotation_spread(poses: list[Pose]) -> float:
    """Largest rotation angle (degrees) between any pose and the mean orientation."""
    if not poses:
        return 0.0
    # Average orientation via the SVD of the summed rotation matrices.
    U, _, Vt = np.linalg.svd(sum(p.R for p in poses))
    mean = U @ Vt
    angles = [np.degrees(np.arccos(np.clip((np.trace(mean.T @ p.R) - 1) / 2, -1.0, 1.0))) for p in poses]
    return float(max(angles))
