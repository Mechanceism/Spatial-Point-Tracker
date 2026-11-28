"""Link to the SPT Camera phone app over the USB cable.

The app (android/) detects markers and ChArUco boards on the phone and serves the
results on a local socket. ``adb forward tcp:<port> localabstract:sptcamera`` makes
that socket reachable at localhost:<port>: no network, no Wi-Fi, no extra phone
permission. Messages are JSON, one per line:

- phone -> PC: ``hello`` (device, lenses, streams, boards) on connect, then ``frame``
  (one lens, one frame: markers + ChArUco corners per board) and ``status`` (thermal,
  camera state) about once a second, and ``reply`` to commands.
- PC -> phone: ``{"cmd": "lock_focus" | "unlock_focus" | "focus" | "boards" |
  "detector" | "streams" | "pc_preview" | "preview" | "ping", ...}``; the phone also
  sends ``streams`` after (re)starting the camera and ``image`` (a small JPEG of one
  lens, when asked with ``pc_preview``).

The phone app must have the Live screen open. :class:`PhoneCamera` turns one lens
into a camera for the live tools (``--camera phone:main``).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .boards import BOARDS, BoardSpec
from .detect import Detection, Observed

SOCKET_NAME = "sptcamera"
DEFAULT_PORT = 8765


def find_adb() -> str:
    """adb from $SPT_ADB, the SDK in %LOCALAPPDATA%\\Android\\Sdk, or PATH."""
    env = os.environ.get("SPT_ADB")
    if env:
        return env
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    if local.is_file():
        return str(local)
    found = shutil.which("adb")
    if found:
        return found
    raise FileNotFoundError("adb not found: install platform-tools or set SPT_ADB (see docs/PHONE.md)")


def board_json(spec: BoardSpec) -> dict:
    """A board definition in the phone app's format (same fields as spt.boards)."""
    return {"name": spec.name, "squares_x": spec.squares_x, "squares_y": spec.squares_y,
            "square_mm": spec.square_mm, "marker_mm": spec.marker_mm, "first_id": spec.first_id}


@dataclass
class PhoneFrame:
    lens: str
    role: str | None
    t_ns: int
    width: int
    height: int
    markers: dict[int, np.ndarray]  # marker id -> (4, 2) corners TL, TR, BR, BL
    boards: dict[str, Detection]  # board name -> ChArUco corners
    detect_ms: float
    focus_d: float | None = None
    focus_locked: bool | None = None
    received: float = field(default_factory=time.monotonic)

    @property
    def image_size(self) -> tuple[int, int]:
        return self.width, self.height


def parse_frame(msg: dict) -> PhoneFrame:
    markers = {int(m["id"]): np.asarray(m["c"], dtype=np.float32).reshape(4, 2) for m in msg.get("markers", [])}
    boards = {}
    for name, b in msg.get("boards", {}).items():
        ids = np.asarray(b["ids"], dtype=np.int32).reshape(-1)
        if len(ids):
            boards[name] = Detection(np.asarray(b["c"], dtype=np.float32).reshape(-1, 2), ids)
    return PhoneFrame(
        lens=str(msg["lens"]), role=msg.get("role"), t_ns=int(msg["t_ns"]),
        width=int(msg["width"]), height=int(msg["height"]), markers=markers, boards=boards,
        detect_ms=float(msg.get("detect_ms", 0.0)), focus_d=msg.get("focus_d"), focus_locked=msg.get("focus_locked"),
    )


class PhoneLink:
    """Connection to the phone app. Frames arrive on a background thread; the newest
    frame per lens is kept in ``latest`` and every frame goes to ``on_frame``."""

    def __init__(self, port: int = DEFAULT_PORT, forward: bool = True, adb: str | None = None,
                 on_frame: Callable[[PhoneFrame], None] | None = None, log: Callable[[str], None] = print,
                 record: str | Path | None = None):
        self.port = port
        self.forward = forward
        self.adb = adb
        self.on_frame = on_frame
        self.log = log
        self.hello: dict | None = None
        self.status: dict | None = None
        self.latest: dict[str, PhoneFrame] = {}
        self.counts: dict[str, int] = defaultdict(int)
        self.replies: deque[dict] = deque(maxlen=50)
        self.streams: list[dict] = []
        self.previews: dict[str, tuple[int, int, int, bytes]] = {}  # lens -> (t_ns, width, height, jpeg)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._hello_event = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._record = open(record, "w", encoding="utf-8") if record else None
        self.error: str | None = None

    # -- connection ---------------------------------------------------------------
    def _adb(self, *args: str) -> None:
        adb = self.adb or find_adb()
        self.log("adb " + " ".join(args))
        subprocess.run([adb, *args], check=True, capture_output=True, text=True, timeout=15)

    def connect(self, timeout: float = 10.0) -> dict:
        if self.forward:
            self._adb("forward", f"tcp:{self.port}", f"localabstract:{SOCKET_NAME}")
        self._sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        self._sock.settimeout(None)
        self._thread = threading.Thread(target=self._read_loop, name="phone-link", daemon=True)
        self._thread.start()
        self._hello_event.wait(timeout)
        if self.hello is None:  # timed out, or adb hung up because the app isn't listening
            self.close()
            raise ConnectionError("no answer from the phone: open SPT Camera and tap Live "
                                  "(adb forward reaches the app only while the Live screen is open)")
        return self.hello

    def _read_loop(self) -> None:
        buf = b""
        try:
            while not self._closed:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle(json.loads(line))
        except OSError as e:
            if not self._closed:
                self.error = str(e)
        finally:
            self._hello_event.set()  # unblock connect() if the phone hung up at once
            if not self._closed:
                self.error = self.error or "phone disconnected"

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "image":
            # Not recorded: pictures are for the window only (and large).
            self.previews[str(msg["lens"])] = (int(msg["t_ns"]), int(msg["width"]), int(msg["height"]),
                                               base64.b64decode(msg["jpeg"]))
            return
        if self._record is not None:
            self._record.write(json.dumps(msg) + "\n")
        if kind == "hello":
            self.hello = msg
            self.streams = msg.get("streams", [])
            self._hello_event.set()
        elif kind == "streams":
            self.streams = msg.get("streams", [])
        elif kind == "frame":
            frame = parse_frame(msg)
            with self._lock:
                self.latest[frame.lens] = frame
                self.counts[frame.lens] += 1
            if self.on_frame is not None:
                self.on_frame(frame)
        elif kind == "status":
            self.status = msg
        elif kind in ("reply", "error"):
            self.replies.append(msg)
            if kind == "error" or msg.get("ok") is False:
                self.log(f"phone: {msg}")

    def close(self) -> None:
        self._closed = True
        if self._sock is not None:
            # Close politely: send "no more data" and let the phone hang up first.
            # Closing while the reader thread is blocked makes Windows reset the
            # connection, and the last commands (e.g. "give all lenses back") get lost.
            try:
                self._sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=2.0)
            try:
                self._sock.close()
            except OSError:
                pass
        if self._record is not None:
            self._record.close()
            self._record = None
        if self.forward:
            try:
                self._adb("forward", "--remove", f"tcp:{self.port}")
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass

    def __enter__(self) -> "PhoneLink":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed and self.error is None

    # -- commands -----------------------------------------------------------------
    def send(self, cmd: dict) -> None:
        if self._sock is None:
            raise ConnectionError("not connected")
        self._sock.sendall((json.dumps(cmd) + "\n").encode("utf-8"))

    def lock_focus(self) -> None:
        self.send({"cmd": "lock_focus"})

    def unlock_focus(self) -> None:
        self.send({"cmd": "unlock_focus"})

    def set_focus(self, lens: str, diopters: float | None) -> None:
        """Fixed focus in diopters (1 / metres) for one lens; None = autofocus."""
        self.send({"cmd": "focus", "lens": lens, "diopters": diopters})

    def send_boards(self, specs: list[BoardSpec]) -> None:
        self.send({"cmd": "boards", "boards": [board_json(s) for s in specs]})

    def set_detector(self, max_fps: float | None = None, aruco3: bool | None = None,
                     min_marker_px: float | None = None) -> None:
        """Detection settings on the phone: detections per lens per second, ArUco3 fast
        mode, and the smallest marker side (px) it must still find."""
        cmd = {"cmd": "detector"}
        for key, value in (("max_fps", max_fps), ("aruco3", aruco3), ("min_marker_px", min_marker_px)):
            if value is not None:
                cmd[key] = value
        self.send(cmd)

    def set_streams(self, lenses: list[str] | None) -> None:
        """Run only these lenses on the phone (None = all). Restarts its camera."""
        self.send({"cmd": "streams", "lenses": lenses})

    def set_pc_preview(self, lens: str | None, fps: float = 5.0, width: int = 960) -> None:
        """Have the phone send a small JPEG of one lens (None = stop)."""
        self.send({"cmd": "pc_preview", "lens": lens, "fps": fps, "width": width})

    def set_preview(self, lens: str) -> None:
        self.send({"cmd": "preview", "lens": lens})

    def take_counts(self) -> dict[str, int]:
        """Frames received per lens since the last call."""
        with self._lock:
            out = dict(self.counts)
            self.counts.clear()
        return out


def resolve_lens(hello: dict, query: str) -> str:
    """'main', 'ultrawide', 'telephoto' / 'tele', or a lens id -> the lens id."""
    q = query.lower()
    if q in ("tele", "tel"):
        q = "telephoto"
    lenses = hello.get("lenses", [])
    for c in lenses:
        if str(c.get("id")) == q:
            return str(c["id"])
    for c in lenses:
        if c.get("role_guess") == q:
            return str(c["id"])
    roles = ", ".join(f"{c['id']}={c['role_guess']}" for c in lenses if c.get("role_guess"))
    raise ValueError(f"no phone lens '{query}' (lenses: {roles})")


def is_phone(camera: str | None) -> bool:
    return bool(camera) and str(camera).lower().startswith("phone")


@dataclass
class PhoneSourceFrame:
    image: np.ndarray  # the phone's preview scaled up to the stream size (for drawing only)
    number: int
    time: float
    observed: Observed  # the phone's detections: what the tools measure with


class PhoneCamera:
    """One lens of the phone as a camera for the live tools, with the same ``start`` /
    ``read`` / ``stop`` as :class:`spt.cameras.LiveCamera`.

    Measurements use the corners the phone detected at full resolution; the picture
    is only a small preview scaled up so the tools can draw on it. Only the chosen
    lens streams on the phone (less heat), and the PC's board definitions are sent.

    ``focus``: None leaves the lens as it is; ``"lock"`` lets autofocus settle and then
    locks it (calibration); a number fixes the focus at that many diopters (the
    distance stored in a calibration). With ``focus_on`` (a board name), "lock" waits
    until that board has been in view for ``focus_settle`` seconds, so autofocus is on
    the board and not on the room behind it. ``focus_d`` is where the lens is focused.
    """

    fourcc = "phone"

    def __init__(self, camera: str = "phone:main", port: int = DEFAULT_PORT, max_fps: float = 10.0,
                 preview_fps: float = 5.0, preview_width: int = 960, boards: list[BoardSpec] | None = None,
                 log: Callable[[str], None] = print, link: PhoneLink | None = None,
                 focus: str | float | None = None, focus_settle: float = 2.0, focus_on: str | None = None):
        self.query = camera.split(":", 1)[1] if ":" in camera else "main"
        self.focus = focus
        self.focus_settle = focus_settle
        self.focus_on = focus_on
        self.max_fps = max_fps
        self.preview_fps = preview_fps
        self.preview_width = preview_width
        self.boards = boards if boards is not None else list(BOARDS.values())
        self.log = log
        self.link = link or PhoneLink(port=port, log=log)
        self.link.on_frame = self._on_frame
        self.lens: str | None = None
        self.role: str | None = None
        self._cond = threading.Condition()
        self._latest: PhoneFrame | None = None
        self._number = 0
        self._image_key: tuple | None = None
        self._image: np.ndarray | None = None

    @property
    def name(self) -> str:
        return f"phone:{self.lens}" if self.lens else "phone"

    def start(self, timeout: float = 20.0) -> "PhoneCamera":
        hello = self.link.connect()
        self.lens = resolve_lens(hello, self.query)
        self.role = next((c.get("role_guess") for c in hello.get("lenses", []) if str(c.get("id")) == self.lens), None)
        self.link.send_boards(self.boards)
        self.link.set_detector(max_fps=self.max_fps)
        self.link.set_pc_preview(self.lens, self.preview_fps, self.preview_width)
        self.link.set_streams([self.lens])
        self.log(f"phone lens {self.lens} ({self.role}): waiting for frames...")
        try:
            self.read(timeout=timeout)
            self._apply_focus()
        except TimeoutError:
            self.stop()
            raise
        return self

    @property
    def zoom(self) -> float | None:
        """The phone's logical zoom while streaming (the app uses its minimum)."""
        return (self.link.hello or {}).get("zoom")

    @property
    def focus_d(self) -> float | None:
        """Where the lens is focused (diopters), if it has autofocus."""
        return self._latest.focus_d if self._latest is not None else None

    @property
    def focus_locked(self) -> bool:
        return bool(self._latest is not None and self._latest.focus_locked)

    def _wait_for_board(self, board: str, seconds: float, timeout: float = 180.0, min_corners: int = 6) -> None:
        self.log(f"point the phone at the {board} board at your working distance and hold it there: "
                 f"focus locks once the board has been in view for {seconds:g} s")
        seen_since = None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.link.error:
                raise TimeoutError(f"phone link lost: {self.link.error}")
            f = self._latest
            det = f.boards.get(board) if f is not None else None
            now = time.monotonic()
            if det is not None and len(det) >= min_corners and now - f.received < 0.5:
                seen_since = seen_since or now
                if now - seen_since >= seconds:
                    return
            else:
                seen_since = None
            time.sleep(0.05)
        raise TimeoutError(f"the {board} board was not seen steadily within {timeout:g} s")

    def _apply_focus(self) -> None:
        if self.focus is None or self.focus_d is None:  # nothing asked, or a fixed-focus lens
            return
        if self.focus == "lock":
            self.link.unlock_focus()
            if self.focus_on:
                self._wait_for_board(self.focus_on, self.focus_settle)
            else:
                self.log(f"letting autofocus settle for {self.focus_settle:g} s, then locking it...")
                time.sleep(self.focus_settle)
            self.link.lock_focus()
        else:
            self.link.set_focus(self.lens, float(self.focus))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.focus_locked:
            time.sleep(0.05)
        if not self.focus_locked:
            raise TimeoutError("the phone did not confirm the focus lock")
        if self.focus != "lock":  # let the lens arrive before measuring
            time.sleep(0.5)
        d = self.focus_d
        self.log(f"focus locked at {d:.3f} D" + (f" ({1 / d:.2f} m)" if d and d > 0 else ""))

    def _on_frame(self, frame: PhoneFrame) -> None:
        if frame.lens != self.lens:
            return
        with self._cond:
            self._latest = frame
            self._number += 1
            self._cond.notify_all()

    def read(self, after: int = 0, timeout: float = 5.0) -> PhoneSourceFrame:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._number <= after or self._latest is None:
                left = deadline - time.monotonic()
                if left <= 0 or self.link.error:
                    raise TimeoutError(f"no frame from the phone ({self.link.error or 'timeout'})")
                self._cond.wait(min(left, 0.2))
            frame, number = self._latest, self._number
        return PhoneSourceFrame(self._picture(frame), number, frame.received, Observed(frame.boards, frame.image_size))

    def _picture(self, frame: PhoneFrame) -> np.ndarray:
        prev = self.link.previews.get(frame.lens)
        key = (prev[0] if prev else None, frame.width, frame.height)
        if key != self._image_key:
            img = None
            if prev is not None:
                small = cv2.imdecode(np.frombuffer(prev[3], np.uint8), cv2.IMREAD_GRAYSCALE)
                if small is not None:
                    img = cv2.cvtColor(cv2.resize(small, (frame.width, frame.height), interpolation=cv2.INTER_LINEAR),
                                       cv2.COLOR_GRAY2BGR)
            if img is None:
                img = np.full((frame.height, frame.width, 3), 60, np.uint8)
            self._image, self._image_key = img, key
        return self._image.copy()

    def stop(self) -> None:
        try:
            self.link.set_pc_preview(None)
            self.link.set_streams(None)  # give the phone's Live screen all lenses back
        except Exception:  # noqa: BLE001 - best effort on the way out
            pass
        self.link.close()

    def __enter__(self) -> "PhoneCamera":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
