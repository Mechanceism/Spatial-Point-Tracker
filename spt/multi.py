"""Several cameras (e.g. the phone's lenses) measuring the same point together.

Each camera measures the target point in the reference board's frame with its own
calibration, so no camera-to-camera calibration is needed: the results are already
in a common frame. The fused point is a weighted mean. :class:`FusedMeasurer` stands
in for :class:`spt.measure.PointMeasurer`, so the live capture logic is unchanged;
every fused result keeps each camera's own point for comparison.

Weights: single-view error grows with the pixel noise divided by the focal length,
so by default each camera gets fx**2 (inverse variance for equal pixel noise).
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
                 anchor: list[str] | None = None, min_cameras: int = 1):
        self.measurers = measurers
        self.weights = weights or {k: float(m.calib.camera_matrix[0, 0]) ** 2 for k, m in measurers.items()}
        self.anchor = anchor or list(measurers)  # whose poses drive the stillness check, in order
        self.min_cameras = min_cameras
        self.last: dict[str, ViewResult] = {}

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
        if len(results) < self.min_cameras:
            return None, "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "nothing seen"
        w = np.array([self.weights[k] for k in results])
        pts = np.array([r.point for r in results.values()])
        point = (w[:, None] * pts).sum(axis=0) / w.sum()
        lead = next(results[k] for k in self.anchor + list(results) if k in results)
        any_m = self.measurers[next(iter(results))]
        return FusedResult(
            name=name, point=point, distance_mm=float(np.linalg.norm(point - any_m.ref_xyz)),
            ref=lead.ref, tgt=lead.tgt, camera_distance_mm=lead.camera_distance_mm,
            points={k: r.point for k, r in results.items()},
            rms_px={k: max(r.ref.rms_px, r.tgt.rms_px) for k, r in results.items()},
            reasons=reasons,
        ), ""


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
