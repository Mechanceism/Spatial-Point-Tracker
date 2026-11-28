"""Link to the SPT Camera phone app over the USB cable.

The app (android/) detects markers and ChArUco boards on the phone and serves the
results on a local socket. ``adb forward tcp:<port> localabstract:sptcamera`` makes
that socket reachable at localhost:<port>: no network, no Wi-Fi, no extra phone
permission. Messages are JSON, one per line:

- phone -> PC: ``hello`` (device, lenses, streams, boards) on connect, then ``frame``
  (one lens, one frame: markers + ChArUco corners per board) and ``status`` (thermal,
  camera state) about once a second, and ``reply`` to commands.
- PC -> phone: ``{"cmd": "lock_focus" | "unlock_focus" | "focus" | "boards" |
  "detector" | "preview" | "ping", ...}``.

The phone app must have the Live screen open.
"""

from __future__ import annotations

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

import numpy as np

from .boards import BoardSpec
from .detect import Detection

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
        if self._record is not None:
            self._record.write(json.dumps(msg) + "\n")
        if kind == "hello":
            self.hello = msg
            self._hello_event.set()
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

    def set_preview(self, lens: str) -> None:
        self.send({"cmd": "preview", "lens": lens})

    def take_counts(self) -> dict[str, int]:
        """Frames received per lens since the last call."""
        with self._lock:
            out = dict(self.counts)
            self.counts.clear()
        return out
