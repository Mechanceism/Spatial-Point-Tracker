"""End-to-end on synthetic photos: calibration recovers the camera, and the
measurement recovers a known target position."""

import json

import numpy as np
import pytest

from spt.boards import REFERENCE, TARGET
from spt.calibration import CameraCalibration, calibrate
from spt.detect import BoardDetector
from spt.measure import PointMeasurer, summarize
from spt.pose import Pose, relative_pose

from synth import K, SIZE, look_at, render, rot

REF_CENTRE = np.array([105.0, 75.0, 0.0])
# Target placed 300 mm along x from the reference, rotated 20 deg in plane and
# raised 15 mm towards the camera (z is into the board).
TGT_IN_REF = Pose(rot(0, 0, 20), np.array([300.0, 20.0, -15.0]))


def calibration_photos():
    """12 tilted views with the board moved over a 3x3 grid of the frame (the
    capture rule: cover the whole frame, corners included)."""
    tilts = [(0, 0), (25, 0), (-25, 0), (0, 25), (0, -25), (20, 20), (-20, 20), (20, -20),
             (-20, -20), (30, 10), (-10, 30), (10, -30)]
    grid = [(gx, gy) for gy in (-1, 0, 1) for gx in (-1, 0, 1)]
    for i, (rx, ry) in enumerate(tilts):
        gx, gy = grid[(i * 4) % len(grid)]
        distance = 620 + 20 * (i % 3)
        R, t = look_at(REF_CENTRE, rx, ry, distance)
        # Shift so the board centre lands ~500 px / ~330 px off the image centre.
        t = t + np.array([gx * 500, gy * 330, 0.0]) * distance / K[0, 0]
        yield f"view{i:02d}", render([(REFERENCE, R, t)])


@pytest.fixture(scope="module")
def calibration_views():
    detector = BoardDetector(REFERENCE)
    return [(name, detector.detect(img)) for name, img in calibration_photos()]


@pytest.fixture(scope="module")
def calib(calibration_views):
    return calibrate(calibration_views, REFERENCE, SIZE)


def test_calibration_recovers_camera(calib):
    assert calib.rms_px < 0.3
    Kc = calib.camera_matrix
    assert abs(Kc[0, 0] - K[0, 0]) / K[0, 0] < 0.01
    assert abs(Kc[1, 1] - K[1, 1]) / K[1, 1] < 0.01
    assert abs(Kc[0, 2] - K[0, 2]) < 5 and abs(Kc[1, 2] - K[1, 2]) < 5
    assert len(calib.views) == 12
    assert calib.coverage > 0.7
    assert np.all(np.abs(calib.dist_coeffs) < 0.5)  # the true lens has none


def test_calibration_json_round_trip(calib, tmp_path):
    path = tmp_path / "cam.calib.json"
    calib.save(path)
    loaded = CameraCalibration.load(path)
    assert np.allclose(loaded.camera_matrix, calib.camera_matrix)
    assert np.allclose(loaded.dist_coeffs, calib.dist_coeffs)
    assert loaded.image_size == calib.image_size
    assert json.loads(path.read_text())["format"] == 1


def test_calibration_needs_enough_views(calibration_views):
    with pytest.raises(ValueError):
        calibrate(calibration_views[:3], REFERENCE, SIZE)


def _scene_photo(rx, ry, distance):
    mid = (REF_CENTRE + TGT_IN_REF.apply([62.5, 50.0, 0.0])) / 2
    R, t = look_at(mid, rx, ry, distance)
    ref = Pose(R, t)
    tgt = ref.compose(TGT_IN_REF)
    return render([(REFERENCE, ref.R, ref.t), (TARGET, tgt.R, tgt.t)])


def test_each_board_ignores_the_other():
    img = _scene_photo(10, -10, 800)
    ref = BoardDetector(REFERENCE).detect(img)
    tgt = BoardDetector(TARGET).detect(img)
    assert len(ref) == REFERENCE.corner_count
    assert len(tgt) == TARGET.corner_count


def test_measurement_recovers_known_point():
    # Exact intrinsics, so this isolates detection + pose + frame bookkeeping (a
    # frame mix-up would be off by centimetres). Even on clean renders corners are
    # only good to ~0.1 px, which tilts the reference pose by ~0.05-0.2 deg; over
    # the 300 mm to the target that is up to ~1 mm along the board normal (z) per
    # view, while the in-plane error stays small. Averaging views helps. See
    # docs/PHASE0.md ("what the synthetic tests already show").
    exact = CameraCalibration(SIZE, K.copy(), np.zeros(5))
    m = PointMeasurer(exact, REFERENCE, TARGET)
    truth = TGT_IN_REF.apply(TARGET.corner_xyz(0))
    results = []
    for i, (rx, ry) in enumerate([(20, 0), (15, 10), (-15, 10), (10, -20), (-10, -15), (25, 0)]):
        result, why = m.measure(f"v{i}", _scene_photo(rx, ry, 550 + 30 * i))
        assert result is not None, why
        err = result.point - truth
        assert np.linalg.norm(err[:2]) < 0.3, (i, err)  # in the board plane
        assert np.linalg.norm(err) < 1.0, (i, err)
        results.append(result)
    s = summarize(results, known_mm=float(np.linalg.norm(truth - REFERENCE.corner_xyz(0))))
    assert s.views == 6
    assert np.linalg.norm(s.mean_point - truth) < 0.4
    assert abs(s.error_mm) < 0.3


def test_measurement_with_calibrated_camera(calib):
    m = PointMeasurer(calib, REFERENCE, TARGET)
    truth = TGT_IN_REF.apply(TARGET.corner_xyz(0))
    results = []
    for i, (rx, ry) in enumerate([(20, 0), (15, 10), (-15, 10), (10, -20), (-10, -15), (25, 0)]):
        result, why = m.measure(f"v{i}", _scene_photo(rx, ry, 550 + 30 * i))
        assert result is not None, why
        results.append(result)
    s = summarize(results)
    assert np.linalg.norm(s.mean_point - truth) < 1.0
    assert s.std_3d < 1.0


def test_measure_rejects_poorly_fitting_views():
    # A wrong focal length makes the board fit badly; the view must be rejected
    # rather than averaged in.
    wrong = CameraCalibration(SIZE, K * np.array([[1.08], [1.08], [1.0]]), np.array([0.3, 0, 0, 0, 0]))
    m = PointMeasurer(wrong, REFERENCE, TARGET, max_rms_px=1.0)
    result, why = m.measure("v", _scene_photo(20, 0, 550))
    assert result is None and "reprojection" in why


def test_measure_reports_missing_board():
    exact = CameraCalibration(SIZE, K.copy(), np.zeros(5))
    m = PointMeasurer(exact, REFERENCE, TARGET)
    R, t = look_at(REF_CENTRE, 0, 0, 500)
    result, why = m.measure("ref only", render([(REFERENCE, R, t)]))
    assert result is None and why == "target board not found"


def test_relative_pose_round_trip():
    a = Pose(rot(10, 20, 30), np.array([1.0, 2.0, 3.0]))
    b = a.compose(TGT_IN_REF)
    rel = relative_pose(a, b)
    assert np.allclose(rel.R, TGT_IN_REF.R) and np.allclose(rel.t, TGT_IN_REF.t)
