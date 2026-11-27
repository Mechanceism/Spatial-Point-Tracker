"""Camera intrinsic calibration from ChArUco photos."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .boards import BoardSpec
from .detect import BoardDetector, Detection

FORMAT = 1
MIN_CORNERS = 8  # per view; fewer makes the view's pose poorly constrained


@dataclass
class CameraCalibration:
    image_size: tuple[int, int]  # (width, height) in sensor orientation
    camera_matrix: np.ndarray  # 3x3
    dist_coeffs: np.ndarray  # (k1, k2, p1, p2, k3[, k4, k5, k6])
    rms_px: float = 0.0
    std_intrinsics: dict[str, float] = field(default_factory=dict)
    views: list[dict] = field(default_factory=list)  # file, corners, error_px
    board: dict = field(default_factory=dict)
    coverage: float = 0.0

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "image_size": list(self.image_size),
            "camera_matrix": self.camera_matrix.tolist(),
            "dist_coeffs": self.dist_coeffs.ravel().tolist(),
            "rms_px": self.rms_px,
            "std_intrinsics": self.std_intrinsics,
            "coverage": self.coverage,
            "board": self.board,
            "views": self.views,
        }

    @classmethod
    def from_json(cls, data: dict) -> "CameraCalibration":
        if data.get("format") != FORMAT:
            raise ValueError(f"unsupported calibration format: {data.get('format')}")
        return cls(
            image_size=tuple(data["image_size"]),
            camera_matrix=np.asarray(data["camera_matrix"], dtype=np.float64),
            dist_coeffs=np.asarray(data["dist_coeffs"], dtype=np.float64),
            rms_px=data.get("rms_px", 0.0),
            std_intrinsics=data.get("std_intrinsics", {}),
            views=data.get("views", []),
            board=data.get("board", {}),
            coverage=data.get("coverage", 0.0),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "CameraCalibration":
        return cls.from_json(json.loads(Path(path).read_text()))


def coverage_fraction(points: list[np.ndarray], image_size: tuple[int, int], grid=(8, 6)) -> float:
    """Fraction of image grid cells containing at least one detected corner.

    Low coverage (corners only near the centre) leaves the lens distortion near the
    edges poorly determined.
    """
    w, h = image_size
    hit = np.zeros(grid[::-1], dtype=bool)
    for pts in points:
        gx = np.clip((pts[:, 0] / w * grid[0]).astype(int), 0, grid[0] - 1)
        gy = np.clip((pts[:, 1] / h * grid[1]).astype(int), 0, grid[1] - 1)
        hit[gy, gx] = True
    return float(hit.mean())


def coverage_image(points: list[np.ndarray], image_size: tuple[int, int], grid=(8, 6), width: int = 1000) -> np.ndarray:
    """Map of where detected corners fall in the frame: covered grid cells green,
    empty cells red. Calibration needs (nearly) all cells covered."""
    w, h = image_size
    s = width / w
    out = np.full((round(h * s), width, 3), 255, dtype=np.uint8)
    hit = np.zeros(grid[::-1], dtype=bool)
    for pts in points:
        gx = np.clip((pts[:, 0] / w * grid[0]).astype(int), 0, grid[0] - 1)
        gy = np.clip((pts[:, 1] / h * grid[1]).astype(int), 0, grid[1] - 1)
        hit[gy, gx] = True
    cw, ch = out.shape[1] / grid[0], out.shape[0] / grid[1]
    for gy in range(grid[1]):
        for gx in range(grid[0]):
            color = (200, 240, 200) if hit[gy, gx] else (200, 200, 255)
            cv2.rectangle(out, (round(gx * cw), round(gy * ch)), (round((gx + 1) * cw), round((gy + 1) * ch)), color, -1)
            cv2.rectangle(out, (round(gx * cw), round(gy * ch)), (round((gx + 1) * cw), round((gy + 1) * ch)), (150, 150, 150), 1)
    for pts in points:
        for x, y in pts:
            cv2.circle(out, (round(x * s), round(y * s)), 2, (60, 60, 60), -1)
    return out


def calibrate(
    views: list[tuple[str, Detection]],
    spec: BoardSpec,
    image_size: tuple[int, int],
    rational: bool = False,
    min_corners: int = MIN_CORNERS,
) -> CameraCalibration:
    detector = BoardDetector(spec)
    obj_pts, img_pts, names = [], [], []
    for name, det in views:
        if det is None or len(det) < min_corners:
            continue
        obj, img = detector.match(det)
        obj_pts.append(obj)
        img_pts.append(img)
        names.append(name)
    if len(obj_pts) < 4:
        raise ValueError(
            f"only {len(obj_pts)} usable views (need at least 4, 15-30 recommended)"
        )

    flags = cv2.CALIB_RATIONAL_MODEL if rational else 0
    rms, K, dist, _rvecs, _tvecs, std_int, _std_ext, per_view = cv2.calibrateCameraExtended(
        obj_pts, img_pts, image_size, None, None, flags=flags
    )
    std_int = np.asarray(std_int).ravel()
    per_view = np.asarray(per_view).ravel()
    return CameraCalibration(
        image_size=tuple(image_size),
        camera_matrix=np.asarray(K, dtype=np.float64),
        dist_coeffs=np.asarray(dist, dtype=np.float64).ravel(),
        rms_px=float(rms),
        std_intrinsics={k: float(v) for k, v in zip(("fx", "fy", "cx", "cy"), std_int[:4])},
        views=[
            {"file": n, "corners": int(len(o)), "error_px": float(e)}
            for n, o, e in zip(names, obj_pts, per_view)
        ],
        board={"name": spec.name, "square_mm": spec.square_mm, "marker_mm": spec.marker_mm},
        coverage=coverage_fraction(img_pts, image_size),
    )
