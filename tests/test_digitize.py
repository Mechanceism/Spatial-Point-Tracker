import numpy as np
import pytest

from spt.calibration import CameraCalibration
from spt.digitize import DigitizeSession
from spt.display import make_scene
from spt.pose import Pose
from spt.probe import screen_probe

from synth import K, SIZE, look_at, render, rot

TIP = np.array([29.9, -60.2, 15.0])
SCENE = make_scene(1920, 1080, 0.153, "touch")
REF = SCENE.board("reference").spec


def calibrated_probe():
    probe = screen_probe("s23", 1080, 2340, 0.06)
    probe.tip.offset = TIP.tolist()
    return probe


def photo_touching(target_xyz, tilt=(0, 0, 0), camera=(10, -10, 520)):
    """The reference board, and the probe posed so its tip is at target_xyz."""
    centre = np.array([130.0, 60.0, 0.0])
    R, t = look_at(centre, camera[0], camera[1], camera[2])
    ref = Pose(R, t)
    R_probe_in_ref = rot(*tilt)
    probe_in_ref = Pose(R_probe_in_ref, target_xyz - R_probe_in_ref @ TIP)
    probe = ref.compose(probe_in_ref)
    board = calibrated_probe().board
    return render([(REF, ref.R, ref.t), (board, probe.R, probe.t)], px_per_mm=10)


def session(frames=5):
    calib = CameraCalibration(SIZE, K.copy(), np.zeros(5))
    return DigitizeSession(calib, REF, calibrated_probe(), SCENE.target_points(), frames_per_point=frames)


def test_touch_layout_targets():
    pts = SCENE.target_points()
    assert len(pts) == 8 and all(p[2] == 0 for p in pts.values())
    ref = SCENE.board("reference")
    x, y = SCENE.targets["T1"]
    assert np.allclose(pts["T1"][:2], (np.array([x, y]) - ref.origin_px) * SCENE.pitch_mm)


def test_point_on_target_is_captured_and_matched():
    target = SCENE.target_points()["T1"]
    img = photo_touching(target, tilt=(10, -15, 5))
    s = session(frames=5)
    states = [s.update("f", img).state for _ in range(7)]
    assert states[:4] == ["moving", "holding", "holding", "holding"]
    assert states[4] == "captured"
    assert states[5:] == ["move", "move"]  # no second capture without moving away
    p = s.points[0]
    assert p.name == "T1"
    assert np.linalg.norm(p.error) < 1.0, p.error
    assert len(s.samples) == 7 and sum(1 for n, _ in s.samples if n == 1) == 5
    summary = s.summary()
    assert summary["matched"] == 1 and summary["xy_rms"] < 1.0


def test_moving_to_another_target_rearms():
    pts = SCENE.target_points()
    s = session(frames=4)
    for name in ("T1", "T5", "T8"):
        img = photo_touching(pts[name], tilt=(0, 10, 0))
        for _ in range(6):
            s.update("f", img)
    assert [p.name for p in s.points] == ["T1", "T5", "T8"]
    assert s.summary()["rms_3d"] < 1.5


def test_missing_probe_and_uncalibrated_tip():
    s = session()
    centre = np.array([130.0, 60.0, 0.0])
    R, t = look_at(centre, 10, -10, 520)
    step = s.update("f", render([(REF, R, t)], px_per_mm=10))
    assert step.state == "none" and step.reason == "probe not found"
    with pytest.raises(ValueError):
        DigitizeSession(CameraCalibration(SIZE, K.copy(), np.zeros(5)), REF, screen_probe("x", 1080, 2340, 0.06))
