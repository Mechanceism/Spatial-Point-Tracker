"""Printable sheets of loose ArUco markers for reference marker maps.

Loose markers (stuck around a car corner, on a fixture, ...) use DICT_5X5_250 IDs from
100 upward, so they never clash with the boards (0-55) or the prisms (60-83). Each
size gets its own page(s), packed as tightly as letter paper allows while every
marker keeps a white border of at least one cell (marker / 7): detection needs it.
Grey lines mark where to cut; cut on them to keep that border.

The marker size printed here is what a marker map assumes, so print at 100 % and
check the 100 mm bar; a marker map can also be scaled to a measured size later.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .markermap import FIRST_LOOSE_ID, MARKER_DICTIONARY, MARKER_DICTIONARY_NAME
from .pdf import PdfPage, write_pdf
from .prism import LETTER, _marker_rects

DEFAULT_SIZES = (165.0, 90.0, 75.0, 50.0, 35.0, 25.0)  # mm: 1, 2, 4, 9, 20 and 42 per letter page
MARGIN = 6.0  # mm, white paper edge (printers cannot print to the edge)
HEADER = 30.0  # mm, title, instructions and the check bar
CUT_GRAY = 0.65
CUT = 0.15  # mm, cut line width
MAX_ID = 249  # last ID of DICT_5X5_250


def quiet(size: float) -> float:
    """White border kept around a marker: one marker cell, at least 3 mm."""
    return max(size / 7.0, 3.0)


def layout(size: float, page=LETTER) -> tuple[int, int]:
    """(columns, rows) of `size` markers that fit on a page with their borders."""
    pw, ph = page
    pitch = size + 2 * quiet(size)
    cols = int((pw - 2 * MARGIN) // pitch)
    rows = int((ph - 2 * MARGIN - HEADER) // pitch)
    if cols == 0 or rows == 0:
        # a single marker as large as the page allows: the paper edge is its border
        return (1, 1) if size + 2 * quiet(size) <= pw and size + 2 * quiet(size) <= ph - HEADER else (0, 0)
    return cols, rows


def largest_single(page=LETTER, step: float = 5.0) -> float:
    """The largest marker (multiple of `step` mm) that fits one per page with its border."""
    pw, ph = page
    s = min(pw, ph - HEADER) / (1 + 2 / 7)
    return math.floor(s / step) * step


def page_boxes(size: float, page=LETTER) -> list[tuple[float, float]]:
    """Top-left corners of the cut boxes (marker + border on every side) on one page."""
    pw, ph = page
    cols, rows = layout(size, page)
    q = quiet(size)
    pitch = size + 2 * q
    if cols * rows == 1:
        x0 = (pw - size) / 2
        y0 = MARGIN + HEADER + (ph - MARGIN - HEADER - size) / 2
        return [(x0 - q, y0 - q)]
    ox = (pw - cols * pitch) / 2
    oy = MARGIN + HEADER + (ph - 2 * MARGIN - HEADER - rows * pitch) / 2
    return [(ox + c * pitch, oy + r * pitch) for r in range(rows) for c in range(cols)]


def _cut_box(page: PdfPage, x: float, y: float, w: float, h: float) -> None:
    for (x0, y0, ww, hh) in ((x, y, w, CUT), (x, y + h - CUT, w, CUT), (x, y, CUT, h), (x + w - CUT, y, CUT, h)):
        page.rect(x0, y0, ww, hh, CUT_GRAY)


def _header(page: PdfPage, size: float, ids: list[int], per_page: int) -> None:
    x = MARGIN + 4
    page.text(x, MARGIN + 6, f"Spatial Point Tracker - loose markers, {size:g} mm  "
                             f"(IDs {ids[0]}-{ids[-1]}, {MARKER_DICTIONARY_NAME})", size_pt=11)
    page.text(x, MARGIN + 12, "Print at 100% / Actual size and check the bar. Cut on the grey lines (they keep the white "
                              "border detection needs).", size_pt=7)
    page.text(x, MARGIN + 16.5, "Stick flat and rigid; every ID only once in a setup. The size is the black square's "
                                "outer edge.", size_pt=7)
    y = MARGIN + 21.5
    page.rect(x, y, 100.0, 0.2)
    for i in range(11):
        page.rect(x + i * 10 - 0.1, y, 0.2, 3.0 if i % 5 else 4.5)
    page.text(x + 102, y + 2.5, "100 mm (check with a ruler)", size_pt=7)


def sheet_pages(sizes=DEFAULT_SIZES, first_id: int = FIRST_LOOSE_ID, pages_per_size: dict | None = None,
                page=LETTER) -> tuple[list[PdfPage], dict[int, float]]:
    """Pages of markers, one size per page; returns the pages and id -> size (mm)."""
    pw, ph = page
    pages, sizes_by_id = [], {}
    next_id = first_id
    for size in sizes:
        cols, rows = layout(size, page)
        if cols == 0:
            raise ValueError(f"a {size:g} mm marker does not fit on the page with its border")
        per_page = cols * rows
        for _ in range((pages_per_size or {}).get(size, 1)):
            ids = list(range(next_id, next_id + per_page))
            if ids[-1] > MAX_ID:
                raise ValueError(f"out of marker IDs (last is {MAX_ID})")
            next_id += per_page
            p = PdfPage(pw, ph)
            _header(p, size, ids, per_page)
            q = quiet(size)
            pitch = size + 2 * q
            for mid, (bx, by) in zip(ids, page_boxes(size, page)):
                _cut_box(p, bx, by, pitch, pitch)
                for rect in _marker_rects(mid, bx + q, by + q, size, MARKER_DICTIONARY):
                    p.rect(*rect)
                label = f"{mid}  {size:g} mm"
                p.text(bx + q, by + q + size + min(q * 0.55, 6.0), label, size_pt=max(4.5, min(8.0, q * 0.9)))
                sizes_by_id[mid] = size
            pages.append(p)
    return pages, sizes_by_id


def write_sheets(out_dir: str | Path, sizes=DEFAULT_SIZES, first_id: int = FIRST_LOOSE_ID,
                 pages_per_size: dict | None = None) -> tuple[Path, Path, dict[int, float]]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pages, sizes_by_id = sheet_pages(sizes, first_id, pages_per_size)
    pdf = out / "loose_markers.pdf"
    write_pdf(pdf, pages)
    meta = out / "loose_markers.json"
    meta.write_text(json.dumps({"format": 1, "dictionary": MARKER_DICTIONARY_NAME,
                                "sizes_mm": {str(k): v for k, v in sizes_by_id.items()}}, indent=1))
    return pdf, meta, sizes_by_id
