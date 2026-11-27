import threading
import time

import cv2
import numpy as np
import pytest

from spt.cameras import CameraInfo, CameraMode, FpsMeter, LiveCamera, find_camera, parse_modes, sharpness


def test_parse_modes_takes_real_max_fps_and_dedupes():
    formats = [
        {"media_type_str": "MJPG", "width": 3840, "height": 2160, "min_framerate": 30.00003, "max_framerate": 3.0},
        {"media_type_str": "MJPG", "width": 3840, "height": 2160, "min_framerate": 30.00003, "max_framerate": 3.0},
        {"media_type_str": "MJPG", "width": 1920, "height": 1080, "min_framerate": 30.0, "max_framerate": 5.0},
        {"media_type_str": "YUY2", "width": 3840, "height": 2160, "min_framerate": 1.0, "max_framerate": 1.0},
    ]
    modes = parse_modes(formats)
    assert len(modes) == 3
    assert modes[0] == CameraMode("MJPG", 3840, 2160, 30.0)


def test_best_mode_prefers_largest_mjpg_that_is_fast_enough():
    cam = CameraInfo(0, "UC40M Video", [
        CameraMode("YUY2", 3840, 2160, 1.0),
        CameraMode("MJPG", 3840, 2160, 30.0),
        CameraMode("MJPG", 1920, 1080, 30.0),
    ])
    assert cam.best_mode() == CameraMode("MJPG", 3840, 2160, 30.0)
    slow = CameraInfo(1, "x", [CameraMode("YUY2", 3840, 2160, 1.0), CameraMode("YUY2", 1280, 720, 30.0)])
    assert slow.best_mode() == CameraMode("YUY2", 1280, 720, 30.0)
    assert CameraInfo(2, "empty").best_mode() is None


CAMS = [
    CameraInfo(0, "UC40M Video"),
    CameraInfo(1, "NexiGo N930E FHD Webcam"),
    CameraInfo(2, "OsmoAction4"),
    CameraInfo(3, "Camera (NVIDIA Broadcast)"),
]


def test_find_camera_by_name_or_index():
    assert find_camera("nexigo", CAMS).index == 1
    assert find_camera("OSMO", CAMS).index == 2
    assert find_camera(0, CAMS).name == "UC40M Video"
    assert find_camera("3", CAMS).virtual
    with pytest.raises(LookupError):
        find_camera("onn", CAMS)
    with pytest.raises(LookupError):
        find_camera("a", CAMS)  # ambiguous


def test_fps_meter():
    m = FpsMeter()
    for i in range(31):
        m.tick(i / 30)
    assert m.fps == pytest.approx(30.0)


def test_sharpness_drops_with_blur():
    rng = np.random.default_rng(0)
    img = (rng.random((200, 200)) * 255).astype(np.uint8)
    assert sharpness(cv2.GaussianBlur(img, (9, 9), 3)) < sharpness(img) / 10


class FakeCapture:
    """Stands in for cv2.VideoCapture: numbered frames at ~100 fps."""

    def __init__(self, index, backend):
        self.props = {}
        self.order = []
        self.n = 0
        self.released = threading.Event()

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.props[prop] = value
        self.order.append(prop)
        return True

    def get(self, prop):
        return {cv2.CAP_PROP_FRAME_WIDTH: 64, cv2.CAP_PROP_FRAME_HEIGHT: 48}.get(prop, 0)

    def read(self):
        time.sleep(0.01)
        self.n += 1
        return True, np.full((48, 64, 3), self.n % 256, dtype=np.uint8)

    def release(self):
        self.released.set()


def test_live_camera_returns_newest_frames():
    fakes = []

    def factory(index, backend):
        fakes.append(FakeCapture(index, backend))
        return fakes[-1]

    with LiveCamera(5, 64, 48, 30, capture_factory=factory) as cam:
        a = cam.read()
        time.sleep(0.1)  # several frames arrive meanwhile
        b = cam.read(after=a.number)
        assert b.number > a.number + 1  # skipped straight to the newest
        assert cam.size == (64, 48)
        assert cam.fps > 20
    fake = fakes[0]
    assert fake.released.is_set()
    assert fake.props[cv2.CAP_PROP_FOURCC] == cv2.VideoWriter_fourcc(*"MJPG")
    assert fake.props[cv2.CAP_PROP_FRAME_WIDTH] == 64
    # DirectShow resets the format if size or fps are set after it.
    fourcc_at = fake.order.index(cv2.CAP_PROP_FOURCC)
    for prop in (cv2.CAP_PROP_FPS, cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT):
        assert fake.order.index(prop) < fourcc_at


def test_live_camera_reports_dead_camera():
    class Dead(FakeCapture):
        def read(self):
            time.sleep(0.001)
            return False, None

    cam = LiveCamera(0, capture_factory=lambda i, b: Dead(i, b)).start()
    try:
        with pytest.raises(TimeoutError):
            cam.read(timeout=2.0)
    finally:
        cam.stop()
