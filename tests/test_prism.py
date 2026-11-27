import json

import cv2
import numpy as np
import pytest

from spt.boards import DICTIONARY
from spt.pdf import write_pdf
from spt.prism import BODY, DESIGNS, LETTER, SMALL, template_pages


def test_designs_and_ids():
    assert SMALL.across_flats == pytest.approx(51.96, abs=0.01)
    assert BODY.across_flats == pytest.approx(103.92, abs=0.01)
    assert SMALL.marker_mm == 24 and BODY.marker_mm == 48
    all_ids = SMALL.ids + BODY.ids
    assert len(all_ids) == len(set(all_ids)) == 24
    assert min(all_ids) >= 60 and max(all_ids) < 100  # clear of the boards (0-55), inside DICT_5X5_100


@pytest.mark.parametrize("design", list(DESIGNS.values()))
def test_nominal_geometry(design):
    markers = design.nominal_markers()
    assert len(markers) == design.sides * design.per_face
    for mid, c in markers.items():
        tl, tr, br, bl = c
        assert np.linalg.norm(tr - tl) == pytest.approx(design.marker_mm)
        assert np.linalg.norm(bl - tl) == pytest.approx(design.marker_mm)
        n = np.cross(bl - tl, tr - tl)  # outward normal for TL, TR, BR, BL seen from outside
        centre = c.mean(axis=0)
        assert np.dot(n, centre[:2].tolist() + [0]) > 0
        assert np.linalg.norm(centre[:2]) == pytest.approx(design.apothem)  # on the face
        assert tl[2] > bl[2]  # "up" is +z
    # slot 0 is nearest the top
    assert markers[design.marker_id(0, 0)][:, 2].mean() > markers[design.marker_id(0, 1)][:, 2].mean()


def test_face_seen_by_a_camera_decodes_with_the_same_ids_and_corner_order():
    """Render face 0 of the small prism as a camera sees it and detect the markers:
    IDs and corner 0 (top-left) must match the nominal geometry."""
    K = np.array([[1400.0, 0, 800], [0, 1400.0, 600], [0, 0, 1]])
    a = SMALL.apothem
    R = np.array([[0, 1, 0], [0, 0, -1], [-1, 0, 0]], float)  # camera looks at face 0 from outside
    t = np.array([0.0, 0.0, 250.0 + a])
    canvas = np.full((1200, 1600), 255, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(DICTIONARY)
    expected = {}
    for k, j in ((0, 0), (0, 1)):
        mid = SMALL.marker_id(k, j)
        corners = SMALL.nominal_markers()[mid]
        img_pts, _ = cv2.projectPoints(corners, cv2.Rodrigues(R)[0], t, K, None)
        img_pts = img_pts.reshape(4, 2).astype(np.float32)
        expected[mid] = img_pts
        size = 700
        marker = cv2.aruco.generateImageMarker(dictionary, mid, size, borderBits=1)
        src = np.float32([[0, 0], [size, 0], [size, size], [0, size]])
        H = cv2.getPerspectiveTransform(src, img_pts)
        warped = cv2.warpPerspective(marker, H, canvas.shape[::-1], borderValue=255)
        mask = cv2.warpPerspective(np.full_like(marker, 255), H, canvas.shape[::-1], borderValue=0) > 0
        canvas[mask] = warped[mask]
    det = cv2.aruco.ArucoDetector(dictionary)
    corners, ids, _ = det.detectMarkers(canvas)
    found = {int(i): c.reshape(4, 2) for i, c in zip(ids.ravel(), corners)}
    assert set(found) == set(expected)
    for mid, pts in expected.items():
        assert np.allclose(found[mid], pts, atol=1.5), mid


def test_templates_fit_letter(tmp_path):
    assert len(template_pages(SMALL)) == 1
    assert len(template_pages(BODY)) == 2  # 3 faces per strip, one strip per page
    for d in DESIGNS.values():
        pages = template_pages(d)
        for p in pages:
            assert (p.width_mm, p.height_mm) == LETTER
        write_pdf(tmp_path / f"{d.name}.pdf", pages)
        d.save(tmp_path / f"{d.name}.json")
        data = json.loads((tmp_path / f"{d.name}.json").read_text())
        assert len(data["nominal_markers"]) == d.sides * d.per_face
