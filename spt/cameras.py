"""Live cameras: finding them by name, their modes, and low-latency frame capture.

On Windows, cameras are enumerated through DirectShow (pygrabber), which gives the
names and supported modes in the same order OpenCV's DirectShow backend uses for
indices, so a camera can be chosen by name ("nexigo") instead of a shifting index.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field

import cv2
import numpy as np

# Software cameras that only re-publish other sources; hidden unless asked for.
VIRTUAL_HINTS = ("virtual", "broadcast")


@dataclass(frozen=True)
class CameraMode:
    fourcc: str
    width: int
    height: int
    fps: float

    @property
    def area(self) -> int:
        return self.width * self.height

    def __str__(self) -> str:
        return f"{self.width}x{self.height} {self.fourcc} {self.fps:.0f} fps"


@dataclass
class CameraInfo:
    index: int
    name: str
    modes: list[CameraMode] = field(default_factory=list)

    @property
    def virtual(self) -> bool:
        return any(h in self.name.lower() for h in VIRTUAL_HINTS)

    def best_mode(self, min_fps: float = 15.0, fourcc: str = "MJPG") -> CameraMode | None:
        """Largest mode of the preferred format that still runs at min_fps."""
        candidates = [m for m in self.modes if m.fps >= min_fps - 0.5]
        preferred = [m for m in candidates if m.fourcc == fourcc] or candidates
        return max(preferred, key=lambda m: (m.area, m.fps), default=None)


def parse_modes(formats: list[dict]) -> list[CameraMode]:
    """Modes from pygrabber's format list (its min/max frame rate fields are
    frame-interval based, so the larger of the two is the real maximum)."""
    modes = {
        CameraMode(
            str(f["media_type_str"]), int(f["width"]), int(f["height"]),
            round(max(f.get("min_framerate", 0), f.get("max_framerate", 0)), 1),
        )
        for f in formats
    }
    return sorted(modes, key=lambda m: (m.fourcc, -m.area, -m.fps))


@contextmanager
def _quiet_native_output():
    """Silence output written by camera driver DLLs (e.g. NVIDIA Broadcast logs)."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (*saved, devnull):
            os.close(fd)


def list_cameras(with_modes: bool = True, include_virtual: bool = False) -> list[CameraInfo]:
    if sys.platform != "win32":
        raise RuntimeError("camera listing is only implemented on Windows (DirectShow)")
    from pygrabber.dshow_graph import FilterGraph

    cameras = []
    with _quiet_native_output():
        names = FilterGraph().get_input_devices()
        for index, name in enumerate(names):
            info = CameraInfo(index, name)
            if info.virtual and not include_virtual:
                continue
            if with_modes:
                try:
                    graph = FilterGraph()
                    graph.add_video_input_device(index)
                    info.modes = parse_modes(graph.get_input_device().get_formats())
                except Exception:
                    info.modes = []
            cameras.append(info)
    return cameras


def find_camera(query: str | int, cameras: list[CameraInfo]) -> CameraInfo:
    """A camera by index or by a case-insensitive part of its name."""
    text = str(query).strip()
    if text.isdigit():
        for cam in cameras:
            if cam.index == int(text):
                return cam
        raise LookupError(f"no camera with index {text}")
    matches = [c for c in cameras if text.lower() in c.name.lower()]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(f"{c.index}: {c.name}" for c in cameras)
    if not matches:
        raise LookupError(f"no camera matching '{text}' (cameras: {names})")
    raise LookupError(f"'{text}' matches several cameras: " + ", ".join(c.name for c in matches))


class FpsMeter:
    def __init__(self, window: int = 30):
        self._times: deque[float] = deque(maxlen=window)

    def tick(self, t: float | None = None) -> None:
        self._times.append(time.perf_counter() if t is None else t)

    @property
    def fps(self) -> float:
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else 0.0


@dataclass
class Frame:
    image: np.ndarray
    number: int  # increases by one per frame received from the camera
    time: float  # perf_counter() when it arrived


class LiveCamera:
    """A camera read on a background thread that keeps only the newest frame.

    Processing then always works on the latest image instead of a queue of stale,
    buffered ones, which keeps the lag to one frame.
    """

    def __init__(
        self,
        index: int,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        fourcc: str = "MJPG",
        backend: int = cv2.CAP_DSHOW,
        capture_factory=cv2.VideoCapture,
    ):
        self.index = index
        self.requested = (width, height, fps, fourcc)
        self._backend = backend
        self._factory = capture_factory
        self._cap = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Condition()
        self._latest: Frame | None = None
        self._count = 0
        self.meter = FpsMeter()
        self.size: tuple[int, int] = (0, 0)
        self.fourcc: str = ""  # format the driver actually delivers
        self.error: str = ""

    def start(self) -> "LiveCamera":
        width, height, fps, fourcc = self.requested
        cap = self._factory(self.index, self._backend)
        if not cap.isOpened():
            raise RuntimeError(f"could not open camera {self.index} (is another app using it?)")
        # Order matters with DirectShow: frame rate, then size, then format LAST.
        # Setting size or frame rate after the format silently resets it to the
        # uncompressed default (YUY2), which runs at 1-5 fps at high resolutions.
        if fps:
            cap.set(cv2.CAP_PROP_FPS, fps)
        if width and height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        self.size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        code = int(cap.get(cv2.CAP_PROP_FOURCC))
        self.fourcc = "".join(chr((code >> 8 * i) & 0xFF) for i in range(4)) if code > 0 else ""
        self._running = True
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.index}", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        failures = 0
        while self._running:
            ok, image = self._cap.read()
            if not ok or image is None:
                failures += 1
                if failures > 50:
                    self.error = "camera stopped delivering frames"
                    self._running = False
                    with self._lock:
                        self._lock.notify_all()
                    return
                time.sleep(0.01)
                continue
            failures = 0
            now = time.perf_counter()
            self.meter.tick(now)
            with self._lock:
                self._count += 1
                self._latest = Frame(image, self._count, now)
                self.size = (image.shape[1], image.shape[0])
                self._lock.notify_all()

    def read(self, after: int = 0, timeout: float = 5.0) -> Frame:
        """The newest frame numbered higher than `after` (waits for one)."""
        deadline = time.perf_counter() + timeout
        with self._lock:
            while self._latest is None or self._latest.number <= after:
                remaining = deadline - time.perf_counter()
                if not self._running or remaining <= 0:
                    raise TimeoutError(self.error or f"no frame from camera {self.index} within {timeout:g} s")
                self._lock.wait(remaining)
            return self._latest

    @property
    def fps(self) -> float:
        return self.meter.fps

    def open_settings(self) -> None:
        """Open the driver's own settings dialog (focus, exposure...) where supported."""
        if self._cap is not None:
            self._cap.set(cv2.CAP_PROP_SETTINGS, 1)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "LiveCamera":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian over the central half of the image (higher = sharper)."""
    h, w = gray.shape[:2]
    centre = gray[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
    return float(cv2.Laplacian(centre, cv2.CV_64F).var())
