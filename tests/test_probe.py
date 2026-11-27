import cv2
import numpy as np
import pytest

from spt.calibration import CameraCalibration
from spt.detect import BoardDetector
from spt.pivot import rotation_spread, solve_pivot, solve_pivot_robust
from spt.pose import Pose, estimate_pose
from spt.probe import ProbeSpec, screen_image, screen_probe

from synth import K, SIZE, render, rot

TIP = np.array([29.9, -60.2, 15.0])  # probe frame, like the S23 + pencil
PIVOT = np.array([20.0, -10.0, 420.0])  # camera frame


def poses_about_pivot(angles, tip=TIP, pivot=PIVOT):
    """Probe poses with the tip held at the pivot: R @ tip + t = pivot."""
    base = np.eye(3)  # board facing the camera; the tip (negative y) points up in the image
    out = []
    for rx, ry, rz in angles:
        R = rot(rx, ry, rz) @ base
        out.append(Pose(R, pivot - R @ tip))
    return out


ANGLES = [(rx, ry, rz) for rx in (-20, 0, 20) for ry in (-25, 0, 25) for rz in (-10, 10)]


def test_exact_poses_recover_tip_and_pivot():
    r = solve_pivot(poses_about_pivot(ANGLES))
    assert np.allclose(r.tip, TIP, atol=1e-6)
    assert np.allclose(r.pivot, PIVOT, atol=1e-6)
    assert r.rms_mm < 1e-6
    assert r.spread_deg > 20


def test_noisy_poses_and_outlier_rejection():
    rng = np.random.default_rng(1)
    poses = poses_about_pivot(ANGLES)
    noisy = [Pose(p.R, p.t + rng.normal(0, 0.2, 3)) for p in poses]
    noisy[3] = Pose(noisy[3].R, noisy[3].t + np.array([8.0, 0, 0]))  # tip slipped
    plain = solve_pivot(noisy)
    robust, keep = solve_pivot_robust(noisy)
    assert 3 not in keep
    assert np.linalg.norm(robust.tip - TIP) < np.linalg.norm(plain.tip - TIP)
    assert np.linalg.norm(robust.tip - TIP) < 0.6


def test_needs_rotation_to_be_determined():
    same = poses_about_pivot([(0, 0, 0)] * 5)
    r = solve_pivot(same)
    assert r.spread_deg < 1e-6
    assert r.condition > 1e6  # one orientation cannot separate tip from pivot


def test_rotation_spread():
    assert rotation_spread(poses_about_pivot([(0, 0, 0), (0, 30, 0)])) == pytest.approx(15, abs=0.1)


def test_screen_probe_layout_and_round_trip(tmp_path):
    p = screen_probe("s23", 1080, 2340, 25.4 / 425)
    assert p.screen["square_px"] == 250
    assert p.screen["origin_px"] == [40, 170]
    assert p.board.square_mm == pytest.approx(250 * 25.4 / 425)
    img = screen_image(p)
    assert img.shape == (2340, 1080)
    det = BoardDetector(p.board).detect(img)
    assert len(det) == p.board.corner_count
    p.tip.guess = [1.0, 2.0, 3.0]
    path = tmp_path / "p.probe.json"
    p.save(path)
    q = ProbeSpec.load(path)
    assert q.board == p.board and q.tip.guess == [1.0, 2.0, 3.0] and q.screen == p.screen
    m = q.with_square_mm(15.1)
    assert m.board.square_mm == 15.1 and m.screen["pitch_mm"] == pytest.approx(15.1 / 250)


def test_pivot_from_rendered_probe_images():
    """End to end: render the probe board rotating about the tip, detect, estimate
    poses, solve. Checks the probe frame conventions as well as the maths."""
    probe = screen_probe("s23", 1080, 2340, 25.4 / 425)
    calib = CameraCalibration(SIZE, K.copy(), np.zeros(5))
    detector = BoardDetector(probe.board)
    est = []
    for pose in poses_about_pivot(ANGLES, pivot=np.array([0.0, -80.0, 380.0])):  # board centred in view
        img = render([(probe.board, pose.R, pose.t)], px_per_mm=12)
        p = estimate_pose(detector, detector.detect(img), calib)
        assert p is not None
        est.append(p)
    r, keep = solve_pivot_robust(est)
    assert np.linalg.norm(r.tip - TIP) < 1.0, r.tip
    assert r.rms_mm < 0.5
