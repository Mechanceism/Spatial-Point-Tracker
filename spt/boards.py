"""ChArUco board definitions and their exact geometry.

Board coordinates are millimetres with the origin at the top-left outer corner of
the pattern, x to the right, y down (as printed) and z into the board. A board
pose (R, t) maps board coordinates to camera coordinates: X_cam = R @ X + t.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import cv2
import numpy as np

DICTIONARY = cv2.aruco.DICT_5X5_100
DICTIONARY_NAME = "DICT_5X5_100"


@dataclass(frozen=True)
class BoardSpec:
    name: str
    squares_x: int
    squares_y: int
    square_mm: float
    marker_mm: float
    first_id: int
    dictionary: int = DICTIONARY

    @property
    def marker_count(self) -> int:
        return (self.squares_x * self.squares_y) // 2

    @property
    def ids(self) -> np.ndarray:
        return np.arange(self.first_id, self.first_id + self.marker_count, dtype=np.int32)

    @property
    def size_mm(self) -> tuple[float, float]:
        return self.squares_x * self.square_mm, self.squares_y * self.square_mm

    @property
    def corner_count(self) -> int:
        return (self.squares_x - 1) * (self.squares_y - 1)

    def scaled(self, square_mm: float | None) -> "BoardSpec":
        """The same board with its true (measured) printed square size.

        Printers rarely print at exactly 100 %, and a 0.5 % scale error is 2 mm over
        400 mm, so measurements should always use the measured square size.
        """
        if square_mm is None or square_mm == self.square_mm:
            return self
        k = square_mm / self.square_mm
        return replace(self, square_mm=square_mm, marker_mm=self.marker_mm * k)

    def cv_board(self) -> cv2.aruco.CharucoBoard:
        dictionary = cv2.aruco.getPredefinedDictionary(self.dictionary)
        return cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y), self.square_mm, self.marker_mm, dictionary, self.ids
        )

    def corner_xyz(self, corner_id: int) -> np.ndarray:
        """Board-frame position (mm) of a ChArUco (inner chessboard) corner."""
        corners = self.cv_board().getChessboardCorners()
        return np.asarray(corners[corner_id], dtype=np.float64).reshape(3)


# The fixed reference (defines the measurement frame) and the moving target
# (stands in for the probe in phase 0). Different marker IDs, so both can be
# detected in one photo.
REFERENCE = BoardSpec("reference", 7, 5, 30.0, 22.0, first_id=0)
TARGET = BoardSpec("target", 5, 4, 25.0, 18.0, first_id=20)
BOARDS = {b.name: b for b in (REFERENCE, TARGET)}


def layout_rects(spec: BoardSpec) -> list[tuple[float, float, float, float]]:
    """Every black area of the printed board as (x, y, w, h) rectangles in mm.

    Used both for the vector PDF and for raster previews/synthetic test images, so
    what is printed and what is tested are the same geometry.
    """
    board = spec.cv_board()
    dictionary = board.getDictionary()
    cells = dictionary.markerSize + 2  # data bits plus a one-cell black border
    rects: list[tuple[float, float, float, float]] = []
    marker_squares: set[tuple[int, int]] = set()

    for marker_id, corners in zip(board.getIds().ravel(), board.getObjPoints()):
        c = np.asarray(corners, dtype=np.float64).reshape(4, 3)[:, :2]
        x0, y0 = float(c[:, 0].min()), float(c[:, 1].min())
        size = float(c[:, 0].max()) - x0
        marker_squares.add(
            (int((x0 + size / 2) // spec.square_mm), int((y0 + size / 2) // spec.square_mm))
        )
        img = cv2.aruco.generateImageMarker(dictionary, int(marker_id), cells, borderBits=1)
        cell = size / cells
        for row in range(cells):
            col = 0
            while col < cells:
                if img[row, col] == 0:
                    end = col
                    while end < cells and img[row, end] == 0:
                        end += 1
                    rects.append((x0 + col * cell, y0 + row * cell, (end - col) * cell, cell))
                    col = end
                else:
                    col += 1

    white_parity = next(iter(marker_squares))
    white_parity = (white_parity[0] + white_parity[1]) % 2
    s = spec.square_mm
    for row in range(spec.squares_y):
        for col in range(spec.squares_x):
            if (col + row) % 2 != white_parity:
                rects.append((col * s, row * s, s, s))
    return rects


def render_raster(spec: BoardSpec, px_per_mm: float, margin_mm: float = 0.0) -> np.ndarray:
    """Rasterise the board (white background, uint8) at the given resolution."""
    w_mm, h_mm = spec.size_mm
    width = round((w_mm + 2 * margin_mm) * px_per_mm)
    height = round((h_mm + 2 * margin_mm) * px_per_mm)
    img = np.full((height, width), 255, dtype=np.uint8)
    for x, y, w, h in layout_rects(spec):
        x0 = round((x + margin_mm) * px_per_mm)
        y0 = round((y + margin_mm) * px_per_mm)
        x1 = round((x + w + margin_mm) * px_per_mm)
        y1 = round((y + h + margin_mm) * px_per_mm)
        img[y0:y1, x0:x1] = 0
    return img
