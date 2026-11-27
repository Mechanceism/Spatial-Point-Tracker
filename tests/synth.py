"""Synthetic photos of boards seen by a known pinhole camera (no distortion)."""

from __future__ import annotations

import cv2
import numpy as np

from spt.boards import BoardSpec, render_raster

SIZE = (1600, 1200)
K = np.array([[1400.0, 0.0, 803.0], [0.0, 1400.0, 597.0], [0.0, 0.0, 1.0]])


def rot(rx_deg: float = 0.0, ry_deg: float = 0.0, rz_deg: float = 0.0) -> np.ndarray:
    R, _ = cv2.Rodrigues(np.array([np.radians(rx_deg), 0.0, 0.0]))
    Ry, _ = cv2.Rodrigues(np.array([0.0, np.radians(ry_deg), 0.0]))
    Rz, _ = cv2.Rodrigues(np.array([0.0, 0.0, np.radians(rz_deg)]))
    return R @ Ry @ Rz


def look_at(target: np.ndarray, rx_deg: float, ry_deg: float, distance: float) -> tuple[np.ndarray, np.ndarray]:
    """Camera pose (world->camera R, t) viewing `target` (world frame = board frame,
    z into the board) from `distance` mm, tilted by rx/ry degrees."""
    R = rot(rx_deg, ry_deg)
    t = np.array([0.0, 0.0, distance]) - R @ target
    return R, t


def render(boards: list[tuple[BoardSpec, np.ndarray, np.ndarray]], K: np.ndarray = K,
           size: tuple[int, int] = SIZE, px_per_mm: float = 8.0, supersample: int = 2) -> np.ndarray:
    """Render each (spec, R, t) board (board->camera pose) into one grayscale photo."""
    w, h = size
    ss = supersample
    # Pixel centre u in the supersampled image is (u + 0.5) / ss - 0.5 in the final one.
    S_img = np.array([[ss, 0, (ss - 1) / 2], [0, ss, (ss - 1) / 2], [0, 0, 1.0]])
    canvas = np.full((h * ss, w * ss), 255, dtype=np.uint8)
    for spec, R, t in boards:
        src = render_raster(spec, px_per_mm)
        # Source pixel centre (u, v) is board point ((u + 0.5) / ppm, (v + 0.5) / ppm).
        S_src = np.array([[1 / px_per_mm, 0, 0.5 / px_per_mm], [0, 1 / px_per_mm, 0.5 / px_per_mm], [0, 0, 1.0]])
        H = S_img @ K @ np.column_stack([R[:, 0], R[:, 1], t]) @ S_src
        warped = cv2.warpPerspective(src, H, (w * ss, h * ss), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=255)
        mask = cv2.warpPerspective(np.full_like(src, 255), H, (w * ss, h * ss), flags=cv2.INTER_NEAREST,
                                   borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0
        canvas[mask] = warped[mask]
    return cv2.resize(canvas, (w, h), interpolation=cv2.INTER_AREA)
