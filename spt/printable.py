"""Printable board pages: the board, how to check the print scale, and where corner 0 is."""

from __future__ import annotations

from .boards import DICTIONARY_NAME, BoardSpec, layout_rects
from .pdf import PdfPage

# Landscape page sizes in mm (width, height).
PAPER_MM = {"letter": (279.4, 215.9), "a4": (297.0, 210.0)}

LEFT_MM = 18.0
TOP_MM = 38.0
LINE_MM = 0.3  # stroke width for ticks and scale bars


def _scale_bar(page: PdfPage, x: float, y: float, vertical: bool) -> None:
    """100 mm bar with ticks every 10 mm (longer every 50 mm)."""
    if vertical:
        page.rect(x, y, LINE_MM, 100.0)
    else:
        page.rect(x, y, 100.0, LINE_MM)
    for i in range(11):
        length = 4.0 if i % 5 == 0 else 2.0
        offset = i * 10.0 - LINE_MM / 2
        if vertical:
            page.rect(x - length, y + offset, length, LINE_MM)
        else:
            page.rect(x + offset, y + LINE_MM, LINE_MM, length)


def board_page(spec: BoardSpec, paper: str = "letter") -> PdfPage:
    width, height = PAPER_MM[paper]
    page = PdfPage(width, height)
    board_w, board_h = spec.size_mm
    s = spec.square_mm
    ids = spec.ids
    # Longest corner-to-corner spans between inner corners, in squares.
    across_x, across_y = spec.squares_x - 2, spec.squares_y - 2

    page.text(LEFT_MM, 12, f"Spatial Point Tracker - {spec.name} board", size_pt=13)
    page.text(
        LEFT_MM, 18.5,
        f"{spec.squares_x} x {spec.squares_y} squares, square {s:g} mm, marker {spec.marker_mm:g} mm, "
        f"{DICTIONARY_NAME}, marker IDs {ids[0]}-{ids[-1]}",
    )
    page.text(
        LEFT_MM, 24,
        "Print at 100% / Actual size (never 'fit to page'). Glue flat to a rigid backing "
        "(glass, acrylic, aluminium).",
    )
    page.text(
        LEFT_MM, 29.5,
        f"Check scale between inner corners: {across_x} squares across = {across_x * s:g} mm, "
        f"{across_y} squares down = {across_y * s:g} mm. Use measured / squares as --square-mm.",
    )

    for x, y, w, h in layout_rects(spec):
        page.rect(LEFT_MM + x, TOP_MM + y, w, h)

    # Ticks in the margin pointing at corner 0 (first inner corner, top-left).
    page.rect(LEFT_MM + s - LINE_MM / 2, TOP_MM - 5.0, LINE_MM, 3.5)
    page.rect(LEFT_MM - 5.0, TOP_MM + s - LINE_MM / 2, 3.5, LINE_MM)
    page.text(LEFT_MM + s + 1.5, TOP_MM - 2.0, "corner 0", size_pt=7)

    _scale_bar(page, LEFT_MM, TOP_MM + board_h + 8.0, vertical=False)
    page.text(LEFT_MM + 102.0, TOP_MM + board_h + 10.5, "100 mm", size_pt=7)
    _scale_bar(page, LEFT_MM + board_w + 12.0, TOP_MM, vertical=True)
    page.text(LEFT_MM + board_w + 14.0, TOP_MM + 102.0, "100 mm", size_pt=7)
    return page
