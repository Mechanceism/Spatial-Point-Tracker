import json

import cv2
import numpy as np

from spt.markermap import MARKER_DICTIONARY, MarkerDetector
from spt.markersheet import DEFAULT_SIZES, LETTER, layout, page_boxes, quiet, sheet_pages, write_sheets
from spt.prism import _marker_rects


def test_sizes_ids_and_counts(tmp_path):
    pages, sizes = sheet_pages()
    assert len(pages) == len(DEFAULT_SIZES)
    assert [layout(s)[0] * layout(s)[1] for s in DEFAULT_SIZES] == [1, 2, 4, 9, 20, 42]
    ids = sorted(sizes)
    assert ids[0] == 100 and ids == list(range(100, 100 + len(ids)))  # unique, clear of boards and prisms
    pdf, meta, _ = write_sheets(tmp_path)
    assert pdf.stat().st_size > 1000
    assert json.loads(meta.read_text())["sizes_mm"]["100"] == 165.0


def test_boxes_fit_the_page_without_overlap():
    pw, ph = LETTER
    for size in DEFAULT_SIZES:
        q = quiet(size)
        boxes = page_boxes(size)
        for bx, by in boxes:  # the marker itself is on the paper with its full border
            assert bx + q >= q - 1e-9 and by + q > 0
            assert bx + q + size + q <= pw + 1e-9 and by + 2 * q + size <= ph + 1e-9
        pitch = size + 2 * q
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert abs(a[0] - b[0]) >= pitch - 1e-9 or abs(a[1] - b[1]) >= pitch - 1e-9


def test_a_page_drawn_as_an_image_is_detected():
    """Rasterise the 25 mm page (the smallest markers) at 6 px/mm and detect every ID."""
    scale = 6.0
    pw, ph = LETTER
    img = np.full((int(ph * scale), int(pw * scale)), 255, np.uint8)
    size = 25.0
    pages, sizes = sheet_pages(sizes=(size,), first_id=150)
    ids = sorted(sizes)
    q = quiet(size)
    for mid, (bx, by) in zip(ids, page_boxes(size)):
        for x, y, w, h, g in _marker_rects(mid, bx + q, by + q, size, MARKER_DICTIONARY):
            img[int(round(y * scale)):int(round((y + h) * scale)), int(round(x * scale)):int(round((x + w) * scale))] = 255 * g
    found = MarkerDetector().detect(img)
    assert set(found) == set(ids)
    side = np.linalg.norm(found[ids[0]][1] - found[ids[0]][0]) / scale
    assert abs(side - size) < 0.5
