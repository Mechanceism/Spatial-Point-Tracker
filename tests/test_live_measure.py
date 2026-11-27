import numpy as np
import pytest

from spt.calibration import CameraCalibration
from spt.display import make_scene
from spt.live_measure import LiveMeasureSession
from spt.measure import PointMeasurer
from spt.pose import Pose

from synth import K, SIZE, look_at, render

SCENE = make_scene(1920, 1080, 0.153, "measure")


def photo(rx, ry, distance, shift=(0.0, 0.0)):
    ref_b, tgt_b = SCENE.board("reference"), SCENE.board("target")
    offset = np.array([*(np.subtract(tgt_b.origin_px, ref_b.origin_px) * SCENE.pitch_mm), 0.0])
    centre = np.array([SCENE.width_px / 2 - ref_b.origin_px[0], SCENE.height_px / 2 - ref_b.origin_px[1], 0]) * SCENE.pitch_mm
    R, t = look_at(centre, rx, ry, distance)
    ref = Pose(R, t + np.array([shift[0], shift[1], 0.0]))
    tgt = ref.compose(Pose(np.eye(3), offset))
    return render([(ref_b.spec, ref.R, ref.t), (tgt_b.spec, tgt.R, tgt.t)], px_per_mm=8)


@pytest.fixture(scope="module")
def photos():
    return [photo(15, 0, 420), photo(-15, 10, 450), photo(10, -15, 400, (30, 0))]


def session(frames=5):
    m = PointMeasurer(CameraCalibration(SIZE, K.copy(), np.zeros(5)),
                      SCENE.board("reference").spec, SCENE.board("target").spec)
    return LiveMeasureSession(m, SCENE.truth(), frames_per_capture=frames)


def feed(s, img, n):
    return [s.update("f", img).state for _ in range(n)]


def test_capture_after_holding_still_then_requires_a_move(photos):
    s = session(frames=5)
    states = feed(s, photos[0], 9)
    assert states[0] == "moving"  # first frame: nothing to compare with
    assert states[1:5] == ["collecting"] * 4
    assert states[5] == "captured"
    assert states[6:] == ["move"] * 3  # same pose: no second capture
    assert len(s.captures) == 1 and len(s.captures[0].results) == 5
    assert len(s.frames) == 9  # every measured frame is recorded
    assert sum(1 for c, _ in s.frames if c == 1) == 5  # tagged with their capture


def test_moving_rearms_and_stats(photos):
    s = session(frames=5)
    for img in photos:
        feed(s, img, 8)
    assert len(s.captures) == 3
    st = s.stats()
    assert st["frames"] == 24
    assert st["frame_noise"] < 1e-6  # identical frames within a capture
    assert st["capture_rms"] < 1.0
    assert np.linalg.norm(st["all_mean_error"]) < 0.6


def test_lost_boards_reset_collection(photos):
    s = session(frames=5)
    feed(s, photos[0], 3)
    blank = np.full_like(photos[0], 255)
    step = s.update("f", blank)
    assert step.state == "none" and "not found" in step.reason
    states = feed(s, photos[0], 5)
    assert "captured" not in states  # first frame after the gap counts as moving
    assert feed(s, photos[0], 1) == ["captured"]
