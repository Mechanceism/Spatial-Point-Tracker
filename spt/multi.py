"""Several cameras (e.g. the phone's lenses) measuring the same point together.

Each camera measures the target point in the reference board's frame with its own
calibration, so no camera-to-camera calibration is needed: the results are already
in a common frame. The fused point is a weighted mean. :class:`FusedMeasurer` stands
in for :class:`spt.measure.PointMeasurer`, so the live capture logic is unchanged;
every fused result keeps each camera's own point for comparison.

Weights (``weighting``):

- ``"cov"`` (default): each camera's 3x3 covariance of the point, propagated from its
  two board fits (corner count, viewing geometry, residuals). A camera is weak along
  its own line of sight, so cameras looking from different directions complement
  each other: the fusion takes each one's strong directions.
- ``"fx2"``: one number per camera, fx**2 (inverse variance for equal pixel noise).
- ``"equal"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .measure import PointMeasurer, ViewResult


@dataclass
class FusedResult(ViewResult):
    points: dict[str, np.ndarray] = field(default_factory=dict)  # camera -> its own point
    rms_px: dict[str, float] = field(default_factory=dict)  # camera -> worse of its two board fits
    reasons: dict[str, str] = field(default_factory=dict)  # camera -> why it gave nothing


class FusedMeasurer:
    def __init__(self, measurers: dict[str, PointMeasurer], weights: dict[str, float] | None = None,
                 anchor: list[str] | None = None, min_cameras: int = 1, weighting: str = "cov",
                 floor_mm: float = 0.05):
        self.measurers = measurers
        if weighting == "equal":
            weights = {k: 1.0 for k in measurers}
        self.weights = weights or {k: float(m.calib.camera_matrix[0, 0]) ** 2 for k, m in measurers.items()}
        self.weighting = weighting
        self.floor_mm = floor_mm  # added to every covariance: no camera is ever "perfect"
        self.anchor = anchor or list(measurers)  # whose poses drive the stillness check, in order
        self.min_cameras = min_cameras
        self.last: dict[str, ViewResult] = {}
        self.last_reasons: dict[str, str] = {}  # camera -> why it gave nothing, last frame

    @property
    def last_detections(self):
        """Detections of the first anchor camera (for drawing)."""
        return self.measurers[self.anchor[0]].last_detections

    def measure(self, name: str, observed: dict) -> tuple[FusedResult | None, str]:
        results, reasons = {}, {}
        for cam, m in self.measurers.items():
            if cam not in observed:
                reasons[cam] = "no frame"
                continue
            r, why = m.measure(name, observed[cam])
            if r is None:
                reasons[cam] = why
            else:
                results[cam] = r
        self.last = results
        self.last_reasons = reasons
        if len(results) < self.min_cameras:
            return None, "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "nothing seen"
        point = fuse_points(results, self.weights, self.weighting, self.floor_mm)
        lead = next(results[k] for k in self.anchor + list(results) if k in results)
        any_m = self.measurers[next(iter(results))]
        return FusedResult(
            name=name, point=point, distance_mm=float(np.linalg.norm(point - any_m.ref_xyz)),
            ref=lead.ref, tgt=lead.tgt, camera_distance_mm=lead.camera_distance_mm,
            points={k: r.point for k, r in results.items()},
            rms_px={k: max(r.ref.rms_px, r.tgt.rms_px) for k, r in results.items()},
            reasons=reasons,
        ), ""


def fuse_points(results: dict, weights: dict[str, float], weighting: str = "cov", floor_mm: float = 0.05) -> np.ndarray:
    """Weighted mean of the cameras' points: by inverse 3x3 covariance when every
    camera has one (``weighting="cov"``), else by the scalar weights."""
    pts = {k: np.asarray(r.point, float) for k, r in results.items()}
    covs = {k: getattr(r, "cov", None) for k, r in results.items()}
    if weighting == "cov" and all(c is not None for c in covs.values()):
        floor = floor_mm ** 2 * np.eye(3)
        W = {k: np.linalg.inv(covs[k] + floor) for k in pts}
        return np.linalg.solve(sum(W.values()), sum(W[k] @ pts[k] for k in pts))
    w = np.array([weights[k] for k in pts])
    return (w[:, None] * np.array(list(pts.values()))).sum(axis=0) / w.sum()


def compare(captures, truth: np.ndarray, cameras: list[str], weights: dict[str, float]) -> dict:
    """Per capture: each camera's own mean over the capture's frames, the fused mean and
    the equal-weight mean, all against truth; plus how alike the cameras' errors are
    (correlation per axis across captures): shared error cannot be averaged away."""
    truth = np.asarray(truth, dtype=float)
    rows = []
    for c in captures:
        row = {"capture": c.number, "fused": c.mean - truth}
        per = {}
        for cam in cameras:
            pts = [r.points[cam] for r in c.results if cam in r.points]
            if pts:
                per[cam] = np.mean(pts, axis=0) - truth
                row[cam] = per[cam]
        if len(per) > 1:
            row["equal"] = np.mean(list(per.values()), axis=0)
        rows.append(row)
    out: dict = {"rows": rows, "rms": {}, "rms_xyz": {}, "n": {}}
    for key in ["fused", "equal", *cameras]:
        errs = np.array([r[key] for r in rows if key in r])
        if len(errs):
            out["rms"][key] = float(np.sqrt(np.mean(np.sum(errs ** 2, axis=1))))
            out["rms_xyz"][key] = np.sqrt(np.mean(errs ** 2, axis=0))
            out["n"][key] = len(errs)
    corr = {}
    full = [r for r in rows if all(k in r for k in cameras)]
    if len(full) >= 3:
        for i, a in enumerate(cameras):
            for b in cameras[i + 1:]:
                ea = np.array([r[a] for r in full])
                eb = np.array([r[b] for r in full])
                corr[f"{a}-{b}"] = [
                    float(np.corrcoef(ea[:, k], eb[:, k])[0, 1]) if ea[:, k].std() > 0 and eb[:, k].std() > 0
                    else float("nan")
                    for k in range(3)
                ]
    out["corr"] = corr
    return out
