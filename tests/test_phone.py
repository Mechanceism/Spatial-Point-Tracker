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


# ---- PhoneCamera: one lens of the phone as a camera for the live tools -------------

import base64  # noqa: E402

import cv2  # noqa: E402

from spt.detect import BoardDetector, Observed  # noqa: E402
from spt.phone import PhoneCamera, is_phone, resolve_lens  # noqa: E402

HELLO = {"type": "hello", "app": "SPT Camera", "version": "test", "streams": [],
         "lenses": [{"id": "2", "role_guess": "ultrawide"}, {"id": "5", "role_guess": "main"},
                    {"id": "6", "role_guess": "telephoto"}]}


def test_resolve_lens_and_is_phone():
    assert resolve_lens(HELLO, "main") == "5"
    assert resolve_lens(HELLO, "tele") == "6"
    assert resolve_lens(HELLO, "2") == "2"
    with pytest.raises(ValueError, match="5=main"):
        resolve_lens(HELLO, "macro")
    assert is_phone("phone:main") and is_phone("Phone") and not is_phone("NexiGo") and not is_phone(None)


def test_observed_stands_in_for_an_image():
    det = parse_frame(frame_msg()).boards["reference"]
    obs = Observed({"reference": det}, (3840, 2160))
    assert BoardDetector(REFERENCE).detect(obs) is det
    assert BoardDetector(BOARDS["target"]).detect(obs) is None


class FakeApp:
    """Like the app: frames for a lens only flow after the PC picks the lenses."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.commands = []
        threading.Thread(target=self.run, daemon=True).start()

    def send(self, conn, msg):
        conn.sendall((json.dumps(msg) + "\n").encode())

    def run(self):
        conn, _ = self.srv.accept()
        with conn:
            self.send(conn, HELLO)
            conn.settimeout(3)
            buf = b""
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        cmd = json.loads(line)
                        self.commands.append(cmd)
                        if cmd["cmd"] == "streams" and cmd["lenses"]:
                            self.send(conn, {"type": "streams", "streams": [{"lens": cmd["lenses"][0]}]})
                            ok, jpg = cv2.imencode(".jpg", np.full((54, 96), 200, np.uint8))
                            self.send(conn, {"type": "image", "lens": "5", "t_ns": 7, "width": 3840, "height": 2160,
                                             "jpeg": base64.b64encode(jpg.tobytes()).decode()})
                            for t in range(6):  # spaced out like a camera
                                self.send(conn, frame_msg("2", t=t))  # another lens: ignored
                                self.send(conn, frame_msg("5", ids=tuple(range(8)), t=t))
                                time.sleep(0.05)
            except OSError:
                pass


def test_phone_camera_reads_one_lens_with_detections_and_picture():
    app = FakeApp()
    cam = PhoneCamera("phone:main", link=PhoneLink(port=app.port, forward=False, log=lambda s: None),
                      log=lambda s: None).start(timeout=3)
    assert cam.lens == "5" and cam.role == "main" and cam.name == "phone:5"
    first = cam.read(timeout=3)
    assert first.observed.size == (3840, 2160)
    assert first.observed.boards["reference"].ids.tolist() == list(range(8))
    assert first.image.shape == (2160, 3840, 3)  # preview scaled up to the stream size
    assert 190 < first.image.mean() < 210  # it is the phone's picture, not the blank fallback
    assert wait_for(lambda: cam._number >= 3)
    later = cam.read(after=first.number, timeout=3)
    assert later.number > first.number
    sent = [c["cmd"] for c in app.commands]
    assert sent[:4] == ["boards", "detector", "pc_preview", "streams"]
    assert app.commands[3]["lenses"] == ["5"]  # only the chosen lens streams
    assert wait_for(lambda: cam._number >= 6)  # the fake is done sending before we hang up
    cam.stop()
    assert wait_for(lambda: len(app.commands) == 6)
    assert [c["cmd"] for c in app.commands][4:] == ["pc_preview", "streams"]
    assert app.commands[-1]["lenses"] is None  # all lenses given back to the phone


@pytest.mark.parametrize("focus, expected", [("lock", {"cmd": "lock_focus"}),
                                             (2.5, {"cmd": "focus", "lens": "5", "diopters": 2.5})])
def test_phone_camera_sets_focus(focus, expected):
    app = FakeApp()
    cam = PhoneCamera("phone:main", link=PhoneLink(port=app.port, forward=False, log=lambda s: None),
                      log=lambda s: None, focus=focus, focus_settle=0.0).start(timeout=3)
    assert cam.focus_locked and cam.focus_d == 2.5
    assert wait_for(lambda: expected in app.commands)
    assert wait_for(lambda: cam._number >= 6)
    cam.stop()


def test_focus_lock_waits_for_the_board():
    app = FakeApp()  # its frames show the reference board, not the target
    cam = PhoneCamera("phone:main", link=PhoneLink(port=app.port, forward=False, log=lambda s: None),
                      log=lambda s: None, focus="lock", focus_settle=0.1, focus_on="reference").start(timeout=3)
    assert wait_for(lambda: {"cmd": "lock_focus"} in app.commands)
    cmds = [c["cmd"] for c in app.commands]
    assert cmds.index("unlock_focus") < cmds.index("lock_focus")  # autofocus runs fresh first
    assert wait_for(lambda: cam._number >= 6)
    cam.stop()
    cam2 = PhoneCamera("phone:main", link=PhoneLink(port=FakeApp().port, forward=False, log=lambda s: None),
                       log=lambda s: None, focus="lock", focus_on="target")
    cam2._wait_for_board  # noqa: B018 - exists
    with pytest.raises(TimeoutError, match="target"):
        cam2.link.connect(timeout=3)
        cam2.lens = "5"
        cam2.link.set_streams(["5"])
        cam2._wait_for_board("target", 0.1, timeout=0.5)
    cam2.link.close()


def test_phone_multi_groups_lenses_by_timestamp():
    from spt.phone import PhoneMulti
    app = FakeApp()  # sends lens 2 and lens 5 frames with the same timestamps
    multi = PhoneMulti(["main", "ultrawide"], link=PhoneLink(port=app.port, forward=False, log=lambda s: None),
                       log=lambda s: None).start(timeout=3)
    assert multi.lenses == ["5", "2"] and multi.preview == "5"
    g = multi.read(timeout=3)
    assert set(g.observed) == {"5", "2"}
    assert g.observed["5"].boards["reference"].ids.tolist() == list(range(8))
    assert wait_for(lambda: multi.complete >= 5)
    assert multi.partial == 0
    multi.stop()
