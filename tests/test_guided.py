import numpy as np
import pytest

from spt.boards import REFERENCE
from spt.detect import BoardDetector, Detection
from spt.guided import GuideConfig, GuidedCalibrator

from synth import K, SIZE
from test_pipeline import calibration_photos

FPS = 30.0


@pytest.fixture(scope="module")
def detections():
    d = BoardDetector(REFERENCE)
    return [d.detect(img) for _, img in calibration_photos()]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1 / FPS
        return self.t


def hold(guide, det, clock, frames):
    """Feed the same (still) detection for a number of frames; return the states."""
    return [guide.update(det, clock()).state for _ in range(frames)]


def test_captures_only_after_holding_still(detections):
    guide, clock = GuidedCalibrator(REFERENCE, SIZE), Clock()
    states = hold(guide, detections[0], clock, 8)
    # first frame has nothing to compare with, then 5 "holding", then the capture
    assert states[0] == "moving"
    assert states[1:6] == ["holding"] * 5
    assert states[6] == "captured"
    assert states[7] == "duplicate"  # keeps holding: no second capture
    assert len(guide.views) == 1


def test_same_view_again_is_not_captured(detections):
    guide, clock = GuidedCalibrator(REFERENCE, SIZE), Clock()
    hold(guide, detections[0], clock, 8)
    hold(guide, detections[5], clock, 20)  # moved away: a new view
    states = hold(guide, detections[0], clock, 20)  # back to the first spot and angle
    assert "captured" not in states
    assert len(guide.views) == 2


def test_lost_board_resets_the_hold(detections):
    guide, clock = GuidedCalibrator(REFERENCE, SIZE), Clock()
    hold(guide, detections[0], clock, 4)
    assert guide.update(None, clock()).state == "none"
    states = hold(guide, detections[0], clock, 6)
    assert "captured" not in states  # the count restarted


def test_full_session_reaches_done(detections):
    cfg = GuideConfig(min_views=10, target_coverage=0.7)
    guide, clock = GuidedCalibrator(REFERENCE, SIZE, cfg), Clock()
    for det in detections:
        hold(guide, det, clock, 20)
    assert len(guide.views) == 12
    assert guide.coverage >= 0.7
    assert min(guide.tilt_range) >= 20
    assert guide.rms < 0.3
    assert abs(guide.calibration.camera_matrix[0, 0] - K[0, 0]) / K[0, 0] < 0.01
    assert guide.done()
    assert guide.hint().startswith("DONE")


def test_bad_view_is_dropped(detections):
    guide, clock = GuidedCalibrator(REFERENCE, SIZE), Clock()
    for det in detections[:8]:
        hold(guide, det, clock, 20)
    assert len(guide.views) == 8
    # A view whose corners are off by a few pixels (like blur or glare) and in a
    # spot not yet covered, so it counts as new.
    rng = np.random.default_rng(0)
    bad = detections[9]
    noisy = Detection(bad.corners + rng.normal(0, 4, bad.corners.shape).astype(np.float32), bad.ids)
    states = hold(guide, noisy, clock, 20)
    assert "rejected" in states
    assert len(guide.views) == 8
    assert guide.rms < 0.3


def test_hints_follow_progress(detections):
    guide = GuidedCalibrator(REFERENCE, SIZE)
    assert "RED" in guide.hint()
    assert not guide.done()


def test_config_for_another_stream_keeps_the_same_steadiness():
    from spt.guided import GuideConfig
    base = GuideConfig()
    same = base.for_stream(1920, 30)
    assert same.still_px == base.still_px and same.still_frames == base.still_frames
    phone = base.for_stream(3840, 15)  # 4K at 15 detections/s
    assert phone.still_px == pytest.approx(4 * base.still_px)
    assert phone.rearm_px == pytest.approx(4 * base.rearm_px)
    assert phone.still_frames == 3  # still 0.2 s of holding
    assert base.for_stream(3840, 5).still_frames == 2  # never fewer than two frames
