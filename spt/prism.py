"""Prism probes: a printed regular prism with ArUco markers glued on its side faces.

Design frame (mm): origin at the centre of the prism, **z** along the axis pointing
to the TOP (away from the tip), **x** through the middle of face 0. Faces are
numbered counter-clockwise seen from the top, so in the flat folding template face
k+1 is to the right of face k (printed side up, top up).

Each face carries `per_face` markers stacked along the axis, marker 0 nearest the
top. Marker corners follow OpenCV's order as seen from outside: top-left, top-right,
bottom-right, bottom-left, with "up" = +z.

The nominal geometry here is what the design *should* be. Printing and gluing are
never exact, so marker-map calibration measures the real layout from images and
starts from this one.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from .boards import DICTIONARY, DICTIONARY_NAME
from .pdf import PdfPage

LETTER = (215.9, 279.4)  # portrait, mm


@dataclass(frozen=True)
class PrismDesign:
    name: str
    face_w: float  # mm, width of each side face (edge to edge)
    length: float  # mm, along the axis
    first_id: int
    sides: int = 6
    per_face: int = 2
    marker_frac: float = 0.8  # marker size relative to its square cell on the face
    tip_mm: float = 80.0  # nominal tip distance below the prism centre, along -z

    @property
    def apothem(self) -> float:
        return self.face_w / (2 * math.tan(math.pi / self.sides))

    @property
    def across_flats(self) -> float:
        return 2 * self.apothem

    @property
    def segment(self) -> float:
        return self.length / self.per_face

    @property
    def marker_mm(self) -> float:
        return self.marker_frac * min(self.face_w, self.segment)

    @property
    def ids(self) -> list[int]:
        return list(range(self.first_id, self.first_id + self.sides * self.per_face))

    def marker_id(self, face: int, slot: int) -> int:
        return self.first_id + face * self.per_face + slot

    def nominal_markers(self) -> dict[int, np.ndarray]:
        """Marker id -> 4x3 corners (TL, TR, BR, BL) in the design frame."""
        out = {}
        h = self.marker_mm / 2
        up = np.array([0.0, 0.0, 1.0])
        for k in range(self.sides):
            a = 2 * math.pi * k / self.sides
            n = np.array([math.cos(a), math.sin(a), 0.0])
            right = np.cross(up, n)  # the viewer's right, seen from outside
            for j in range(self.per_face):
                zc = self.length / 2 - self.segment * (j + 0.5)
                c = self.apothem * n + zc * up
                out[self.marker_id(k, j)] = np.array([
                    c + h * (-right + up), c + h * (right + up), c + h * (right - up), c + h * (-right - up),
                ])
        return out

    @property
    def nominal_tip(self) -> np.ndarray:
        return np.array([0.0, 0.0, -self.tip_mm])

    def to_json(self) -> dict:
        return {
            "format": 1,
            "design": asdict(self),
            "dictionary": DICTIONARY_NAME,
            "across_flats_mm": round(self.across_flats, 3),
            "marker_mm": round(self.marker_mm, 3),
            "nominal_markers": {str(i): np.round(c, 4).tolist() for i, c in self.nominal_markers().items()},
            "nominal_tip": self.nominal_tip.tolist(),
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2))


# The two designs chosen with scripts/probe_size_sim.py.
SMALL = PrismDesign("hex30", face_w=30.0, length=60.0, first_id=60, tip_mm=80.0)
BODY = PrismDesign("hex60", face_w=60.0, length=120.0, first_id=72, tip_mm=200.0)
DESIGNS = {d.name: d for d in (SMALL, BODY)}


# ----- folding template ---------------------------------------------------

LINE = 0.2  # mm, cut / fold line width
TAB = 8.0  # mm, glue tab width


def _marker_rects(marker_id: int, x: float, y: float, size: float) -> list[tuple[float, float, float, float, float]]:
    """A marker whose top-left corner is at (x, y) on the page, as (x, y, w, h, gray)
    rectangles: one solid black square, then the white cells on top. The black border
    is then a single shape, so no hairline seams can break it up in print."""
    dictionary = cv2.aruco.getPredefinedDictionary(DICTIONARY)
    cells = dictionary.markerSize + 2
    img = cv2.aruco.generateImageMarker(dictionary, marker_id, cells, borderBits=1)
    cell = size / cells
    rects = [(x, y, size, size, 0.0)]
    for row in range(cells):
        col = 0
        while col < cells:
            if img[row, col] != 0:
                end = col
                while end < cells and img[row, end] != 0:
                    end += 1
                rects.append((x + col * cell, y + row * cell, (end - col) * cell, cell, 1.0))
                col = end
            else:
                col += 1
    return rects


def _hline(page: PdfPage, x0, x1, y, dashed=False):
    if not dashed:
        page.rect(x0, y - LINE / 2, x1 - x0, LINE)
        return
    x = x0
    while x < x1:
        page.rect(x, y - LINE / 2, min(2.0, x1 - x), LINE)
        x += 3.5


def _vline(page: PdfPage, x, y0, y1, dashed=False):
    if not dashed:
        page.rect(x - LINE / 2, y0, LINE, y1 - y0)
        return
    y = y0
    while y < y1:
        page.rect(x - LINE / 2, y, LINE, min(2.0, y1 - y))
        y += 3.5


def _strip(page: PdfPage, design: PrismDesign, faces: list[int], x0: float, y0: float) -> float:
    """Draw faces side by side with a glue tab on the right; returns the strip height."""
    w, L = design.face_w, design.length
    n = len(faces)
    right = x0 + n * w
    # cut outline (faces + tab), fold lines between faces and at the tab
    _hline(page, x0, right + TAB, y0)
    _hline(page, x0, right + TAB, y0 + L)
    _vline(page, x0, y0, y0 + L)
    _vline(page, right + TAB, y0, y0 + L)
    for i in range(1, n + 1):
        _vline(page, x0 + i * w, y0, y0 + L, dashed=True)
    page.text(right + 1.2, y0 + L / 2 + 1, "glue", size_pt=6)
    m = design.marker_mm
    seg = design.segment
    for i, k in enumerate(faces):
        fx = x0 + i * w
        for j in range(design.per_face):
            mx = fx + (w - m) / 2
            my = y0 + seg * j + (seg - m) / 2
            for r in _marker_rects(design.marker_id(k, j), mx, my, m):
                page.rect(*r)
        page.text(fx + 1.5, y0 + L + 4.5, f"face {k}  ids {design.marker_id(k, 0)}-{design.marker_id(k, design.per_face - 1)}", size_pt=5.5)
    page.text(x0, y0 - 2.5, "TOP (away from the tip)", size_pt=6)
    return L


def template_pages(design: PrismDesign) -> list[PdfPage]:
    """Letter pages with the wrap-around strip(s). Faces that don't fit across the page
    are split into several strips, each with its own glue tab."""
    pw, ph = LETTER
    margin = 12.0
    per_strip = max(1, int((pw - 2 * margin - TAB) // design.face_w))
    groups = [list(range(s, min(s + per_strip, design.sides))) for s in range(0, design.sides, per_strip)]
    pages, page, y = [], None, 0.0
    for gi, faces in enumerate(groups):
        if page is None or y + design.length + 12 > ph - margin:
            page = PdfPage(pw, ph)
            pages.append(page)
            _header(page, design, len(groups))
            y = 58.0
        _strip(page, design, faces, margin, y)
        y += design.length + 16
    return pages


def _header(page: PdfPage, design: PrismDesign, strips: int) -> None:
    x = 12.0
    page.text(x, 14, f"Spatial Point Tracker - prism probe '{design.name}' folding template", size_pt=12)
    page.text(x, 20.5, f"Hex prism: {design.sides} faces of {design.face_w:g} mm x {design.length:g} mm long "
                       f"({design.across_flats:.2f} mm across flats). Markers {design.marker_mm:g} mm, "
                       f"{DICTIONARY_NAME}, IDs {design.ids[0]}-{design.ids[-1]}.", size_pt=7.5)
    page.text(x, 26, "Print at 100% / Actual size. Check the bar below, then cut on solid lines, crease the dashed lines,",
              size_pt=7.5)
    page.text(x, 31, "and wrap printed side out with TOP towards the top of the probe (away from the tip). Glue each tab under"
                     + (" the next strip." if strips > 1 else " face 0."), size_pt=7.5)
    page.text(x, 36, "Keep every face flat: glue fully, no bubbles. Small errors are fine - calibration measures the real layout.",
              size_pt=7.5)
    # 100 mm check bar with 10 mm ticks
    y = 43.0
    page.rect(x, y, 100.0, LINE)
    for i in range(11):
        page.rect(x + i * 10 - LINE / 2, y, LINE, 3.0 if i % 5 else 4.5)
    page.text(x + 102, y + 2.5, "100 mm (check with a ruler)", size_pt=7)
