"""PC side of the phone link, against a fake phone on a local socket."""

import json
import socket
import threading
import time

import numpy as np
import pytest

from spt.boards import BOARDS, REFERENCE
from spt.phone import PhoneLink, board_json, parse_frame


def frame_msg(lens="5", ids=(0, 1, 2), t=1000):
    return {
        "type": "frame", "lens": lens, "role": "main", "t_ns": t, "width": 3840, "height": 2160,
        "markers": [{"id": 3, "c": [0, 0, 10, 0, 10, 10, 0, 10]}],
        "boards": {"reference": {"ids": list(ids), "c": [float(v) for i in ids for v in (i * 10, i * 20)]}},
        "detect_ms": 42.5, "focus_d": 2.5, "focus_locked": True,
    }


def test_parse_frame_gives_pc_detections():
    f = parse_frame(frame_msg())
    assert f.lens == "5" and f.image_size == (3840, 2160)
    assert f.markers[3].shape == (4, 2) and f.markers[3][2].tolist() == [10, 10]
    det = f.boards["reference"]
    assert det.ids.tolist() == [0, 1, 2]
    assert det.corners.dtype == np.float32 and det.corners.shape == (3, 2)
    assert det.corners[2].tolist() == [20, 40]
    assert f.focus_d == 2.5 and f.focus_locked is True


def test_empty_board_is_dropped():
    msg = frame_msg()
    msg["boards"]["target"] = {"ids": [], "c": []}
    assert "target" not in parse_frame(msg).boards


def test_board_json_matches_the_phone_format():
    for spec in BOARDS.values():
        d = board_json(spec)
        assert set(d) == {"name", "squares_x", "squares_y", "square_mm", "marker_mm", "first_id"}
    assert board_json(REFERENCE) == {"name": "reference", "squares_x": 7, "squares_y": 5,
                                     "square_mm": 30.0, "marker_mm": 22.0, "first_id": 0}


class FakePhone:
    """Accepts one connection, sends hello + frames, records the commands it receives."""

    def __init__(self, frames):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.frames = frames
        self.commands = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        conn, _ = self.srv.accept()
        with conn:
            hello = {"type": "hello", "app": "SPT Camera", "version": "test", "streams": [], "lenses": []}
            conn.sendall((json.dumps(hello) + "\n").encode())
            for f in self.frames:
                conn.sendall((json.dumps(f) + "\n").encode())
            conn.settimeout(2)
            buf = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.commands.append(json.loads(line))
                        conn.sendall((json.dumps({"type": "reply", "cmd": self.commands[-1]["cmd"], "ok": True}) + "\n").encode())
            except OSError:  # timeout, or the PC closing the link (a reset on Windows)
                pass


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_link_receives_frames_and_sends_commands(tmp_path):
    phone = FakePhone([frame_msg("5", t=1), frame_msg("2", t=2), frame_msg("5", ids=(4, 5), t=3)])
    seen = []
    link = PhoneLink(port=phone.port, forward=False, on_frame=seen.append, log=lambda s: None,
                     record=tmp_path / "rec.jsonl")
    hello = link.connect(timeout=3)
    assert hello["app"] == "SPT Camera"
    assert wait_for(lambda: len(seen) == 3)
    assert link.latest["5"].boards["reference"].ids.tolist() == [4, 5]  # newest frame per lens
    assert link.take_counts() == {"5": 2, "2": 1}
    assert link.take_counts() == {}
    link.lock_focus()
    link.set_focus("6", 1.5)
    link.set_focus("6", None)
    link.send_boards([REFERENCE])
    link.set_detector(max_fps=5, aruco3=False)
    assert wait_for(lambda: len(phone.commands) == 5)
    assert [c["cmd"] for c in phone.commands] == ["lock_focus", "focus", "focus", "boards", "detector"]
    assert phone.commands[4] == {"cmd": "detector", "max_fps": 5, "aruco3": False}  # unset values not sent
    assert phone.commands[1] == {"cmd": "focus", "lens": "6", "diopters": 1.5}
    assert phone.commands[2]["diopters"] is None
    assert phone.commands[3]["boards"][0]["name"] == "reference"
    assert wait_for(lambda: len(link.replies) == 5)
    link.close()
    lines = (tmp_path / "rec.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["type"] == "hello" and len(lines) >= 4


def test_connect_fails_cleanly_without_hello():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept_and_hang_up():
        conn, _ = srv.accept()
        conn.close()  # what adb does when the app isn't listening

    threading.Thread(target=accept_and_hang_up, daemon=True).start()
    link = PhoneLink(port=port, forward=False, log=lambda s: None)
    with pytest.raises(ConnectionError, match="Live"):
        link.connect(timeout=2)
    srv.close()
