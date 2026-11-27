import numpy as np

from spt.boards import BOARDS, REFERENCE, TARGET, render_raster
from spt.detect import BoardDetector


def test_boards_use_disjoint_marker_ids():
    assert set(REFERENCE.ids).isdisjoint(TARGET.ids)
    assert REFERENCE.ids.max() < 100 and TARGET.ids.max() < 100  # DICT_5X5_100


def test_layout_matches_opencv_rendering():
    # 10 px/mm makes squares whole pixels; marker edges can fall on half pixels
    # (the probe's 10.5 mm markers in 15 mm squares sit 22.5 px in), so allow a
    # sliver of rounding difference along marker and cell edges. Square corners,
    # which carry the measurement, are checked exactly by detection tests.
    for spec in BOARDS.values():
        ours = render_raster(spec, px_per_mm=10)
        w, h = ours.shape[1], ours.shape[0]
        theirs = spec.cv_board().generateImage((w, h), marginSize=0, borderBits=1)
        assert (ours == theirs).mean() > 0.985, spec.name
        assert ours[5, 5] == theirs[5, 5] == 0, spec.name  # same square parity (top-left black)


def test_rendered_board_is_fully_detected():
    for spec in BOARDS.values():
        img = render_raster(spec, px_per_mm=6, margin_mm=10)
        det = BoardDetector(spec).detect(img)
        assert det is not None and len(det) == spec.corner_count
        # Detected corner 0 sits at (square, square) mm from the pattern corner.
        i = list(det.ids).index(0)
        expected = (spec.square_mm + 10) * 6 - 0.5  # pixel-centre convention
        assert np.allclose(det.corners[i], [expected, expected], atol=0.3)


def test_scaled_spec_keeps_marker_ratio():
    s = REFERENCE.scaled(29.85)
    assert s.square_mm == 29.85
    assert np.isclose(s.marker_mm / s.square_mm, REFERENCE.marker_mm / REFERENCE.square_mm)
    assert REFERENCE.scaled(None) is REFERENCE


def test_corner_xyz():
    assert np.allclose(REFERENCE.corner_xyz(0), [30, 30, 0])
    assert np.allclose(REFERENCE.corner_xyz(REFERENCE.squares_x - 1), [30, 60, 0])
