import numpy as np
import pytest

from spt.boards import REFERENCE, TARGET
from spt.calibration import CameraCalibration
from spt.detect import BoardDetector
from spt.display import LAYOUTS, Scene, make_scene, pitch_from_diagonal
from spt.measure import PointMeasurer, summarize
from spt.pose import Pose

from synth import K, SIZE, look_at, render, rot

SCREENS = [(2560, 1080, 30.0), (1920, 1080, 24.0), (1920, 1080, 13.3), (3440, 1440, 34.0)]


def test_pitch_from_diagonal():
    # 13.3" 1920x1080 panels have a 0.153 mm pitch.
    assert abs(pitch_from_diagonal(13.3, 1920, 1080) - 0.1534) < 0.001


@pytest.mark.parametrize("w,h,diag", SCREENS)
@pytest.mark.parametrize("layout", LAYOUTS)
def test_layouts_fit_on_screen(w, h, diag, layout):
    scene = make_scene(w, h, pitch_from_diagonal(diag, w, h), layout)
    img = scene.image()
    assert img.shape == (h, w)
    boxes = []
    for b in scene.boards:
        x, y = b.origin_px
        bw, bh = b.square_px * b.spec.squares_x, b.square_px * b.spec.squares_y
        assert x >= 16 and y >= 16 and x + bw <= w - 16 and y + bh <= h - 16
        assert abs(b.spec.square_mm - b.square_px * scene.pitch_mm) < 1e-9
        boxes.append((x, x + bw))
    if len(boxes) == 2:
        assert boxes[0][1] + 16 <= boxes[1][0]  # quiet zone between the boards
        assert scene.boards[1].square_px / scene.boards[0].square_px == pytest.approx(25 / 30, abs=0.01)


def test_truth_matches_detected_screen_pixels():
    scene = make_scene(2560, 1080, 0.2743, "measure")
    img = scene.image()
    ref = BoardDetector(scene.board("reference").spec).detect(img)
    tgt = BoardDetector(scene.board("target").spec).detect(img)
    assert len(ref) == REFERENCE.corner_count and len(tgt) == TARGET.corner_count
    ref0 = ref.corners[list(ref.ids).index(0)]
    tgt0 = tgt.corners[list(tgt.ids).index(0)]
    ref_xyz = scene.board("reference").spec.corner_xyz(0)
    expected = (tgt0 - ref0) * scene.pitch_mm + ref_xyz[:2]
    assert np.allclose(scene.truth()[:2], expected, atol=0.1 * scene.pitch_mm)
    assert scene.truth()[2] == 0.0


def test_scene_json_round_trip(tmp_path):
    scene = make_scene(1920, 1080, 0.2767, "measure", monitor=r"\\.\DISPLAY2")
    path = tmp_path / "scene.json"
    scene.save(path)
    loaded = Scene.load(path)
    assert np.allclose(loaded.truth(), scene.truth())
    assert loaded.known_mm() == pytest.approx(scene.known_mm())
    assert [b.square_px for b in loaded.boards] == [b.square_px for b in scene.boards]
    assert np.array_equal(loaded.image(), scene.image())


def test_single_board_layouts_have_no_truth():
    for layout in ("calib", "reference", "target"):
        assert make_scene(1920, 1080, 0.2767, layout).truth() is None


def test_photo_of_screen_measures_truth():
    """Synthetic photos of the 'measure' screen recover the exact truth."""
    scene = make_scene(2560, 1080, 0.2743, "measure")
    ref_b, tgt_b = scene.board("reference"), scene.board("target")
    offset = np.array([*(np.subtract(tgt_b.origin_px, ref_b.origin_px) * scene.pitch_mm), 0.0])
    tgt_in_ref = Pose(np.eye(3), offset)
    centre = np.array([scene.width_px / 2 - ref_b.origin_px[0], scene.height_px / 2 - ref_b.origin_px[1], 0]) * scene.pitch_mm

    exact = CameraCalibration(SIZE, K.copy(), np.zeros(5))
    m = PointMeasurer(exact, ref_b.spec, tgt_b.spec)
    results = []
    for rx, ry in [(20, 0), (-20, 10), (15, -20), (-10, -25)]:
        R, t = look_at(centre, rx, ry, 850)
        ref = Pose(R, t)
        tgt = ref.compose(tgt_in_ref)
        img = render([(ref_b.spec, ref.R, ref.t), (tgt_b.spec, tgt.R, tgt.t)], px_per_mm=6)
        result, why = m.measure("v", img)
        assert result is not None, why
        results.append(result)
    s = summarize(results, scene.known_mm(), scene.truth())
    assert s.rms_error_3d < 1.0
    assert np.linalg.norm(s.mean_error) < 0.5
    assert abs(s.error_mm) < 0.3
