"""Loading photos and detecting ChArUco corners."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .boards import BoardSpec

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def list_images(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"not a folder: {folder}")
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def load_gray(path: str | Path) -> np.ndarray:
    """Grayscale image in *sensor* orientation.

    EXIF rotation is deliberately ignored: calibration describes the sensor, and a
    portrait photo rotated by EXIF would otherwise look like a different camera.
    """
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        raise ValueError(f"could not read image: {path}")
    return img


def image_size(gray: np.ndarray) -> tuple[int, int]:
    return gray.shape[1], gray.shape[0]


def most_common_size(sizes: list[tuple[int, int]]) -> tuple[int, int]:
    return Counter(sizes).most_common(1)[0][0]


@dataclass
class Detection:
    corners: np.ndarray  # (N, 2) float32 pixel positions
    ids: np.ndarray  # (N,) int32 ChArUco corner ids

    def __len__(self) -> int:
        return len(self.ids)


class BoardDetector:
    def __init__(self, spec: BoardSpec):
        self.spec = spec
        self.board = spec.cv_board()
        self._detector = cv2.aruco.CharucoDetector(self.board)

    def detect(self, gray: np.ndarray) -> Detection | None:
        corners, ids, _marker_corners, _marker_ids = self._detector.detectBoard(gray)
        if ids is None or len(ids) == 0:
            return None
        return Detection(
            np.asarray(corners, dtype=np.float32).reshape(-1, 2),
            np.asarray(ids, dtype=np.int32).reshape(-1),
        )

    def match(self, det: Detection) -> tuple[np.ndarray, np.ndarray]:
        """Board-frame 3D points and their pixel positions for a detection."""
        obj, img = self.board.matchImagePoints(det.corners.reshape(-1, 1, 2), det.ids.reshape(-1, 1))
        return (
            np.asarray(obj, dtype=np.float32).reshape(-1, 3),
            np.asarray(img, dtype=np.float32).reshape(-1, 2),
        )


def draw_detection(image: np.ndarray, *dets: Detection | None, max_side: int = 1600) -> np.ndarray:
    """Overlay of detected corners (red, then green, ...) downscaled for viewing.
    Accepts a grayscale or colour image; returns a new colour image."""
    out = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image.copy()
    colors = [(0, 0, 255), (0, 200, 0), (255, 0, 0)]
    for det, color in zip(dets, colors):
        if det is not None:
            cv2.aruco.drawDetectedCornersCharuco(
                out, det.corners.reshape(-1, 1, 2), det.ids.reshape(-1, 1), color
            )
    scale = max_side / max(out.shape[:2])
    if scale < 1:
        out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return out
