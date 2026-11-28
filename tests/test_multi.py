"""Fusing several cameras' measurements of the same point."""

from types import SimpleNamespace

import numpy as np
import pytest

from spt.live_measure import Capture
from spt.multi import FusedMeasurer, FusedResult, compare


class StubMeasurer:
    """Returns a fixed point (or nothing) like PointMeasurer.measure."""

    def __init__(self, fx, point, rms=0.2, t=(0, 0, 400.0)):
        self.calib = SimpleNamespace(camera_matrix=np.array([[fx, 0, 0], [0, fx, 0], [0, 0, 1.0]]))
        self.point = None if point is None else np.asarray(point, float)
        self.rms = rms
        self.t = np.asarray(t, float)
        self.hidden = False  # True: "board not found" this frame
        self.ref_xyz = np.zeros(3)
        self.last_detections = ("ref", "tgt")

    def measure(self, name, observed):
        if self.point is None or self.hidden:
            return None, "target board not found"
        pose = SimpleNamespace(rms_px=self.rms, t=self.t, R=np.eye(3))
        return SimpleNamespace(point=self.point, ref=pose, tgt=pose, camera_distance_mm=400.0), ""


def test_fused_point_is_fx_squared_weighted():
    fm = FusedMeasurer({"5": StubMeasurer(2500, [0, 0, 1]), "6": StubMeasurer(5000, [0, 0, 6])})
    r, why = fm.measure("g1", {"5": object(), "6": object()})
    assert isinstance(r, FusedResult) and why == ""
    assert r.point[2] == pytest.approx((1 * 1 + 6 * 4) / 5)  # weights 1 : 4
    assert set(r.points) == {"5", "6"} and r.rms_px["5"] == 0.2
    assert fm.last_detections == ("ref", "tgt")


def test_missing_lens_is_skipped_and_reported():
    fm = FusedMeasurer({"5": StubMeasurer(2500, [1, 2, 3]), "2": StubMeasurer(1500, None)})
    r, _ = fm.measure("g1", {"5": object(), "2": object()})
    assert r.point.tolist() == [1, 2, 3] and r.reasons == {"2": "target board not found"}
    r, _ = fm.measure("g2", {"5": object()})  # lens 2 had no frame in this group
    assert r.reasons == {"2": "no frame"}
    none, why = FusedMeasurer({"2": StubMeasurer(1500, None)}).measure("g3", {"2": object()})
    assert none is None and "not found" in why


def test_compare_per_lens_and_fused():
    truth = np.zeros(3)

    def res(a, b):
        p = {"a": np.array([0, 0, a]), "b": np.array([0, 0, b])}
        return FusedResult(name="", point=(p["a"] + p["b"]) / 2, distance_mm=0, ref=None, tgt=None,
                           camera_distance_mm=0, points=p)

    # lens errors opposite in sign: fusion cancels them; then the same sign: it can't
    caps = [Capture(1, [res(1, -1)] * 3), Capture(2, [res(-2, 2)] * 3), Capture(3, [res(1, 1)] * 3)]
    out = compare(caps, truth, ["a", "b"], {"a": 1.0, "b": 1.0})
    assert out["rms"]["a"] == pytest.approx(np.sqrt((1 + 4 + 1) / 3))
    assert out["rms"]["fused"] == pytest.approx(np.sqrt(1 / 3))
    assert out["rms"]["equal"] == pytest.approx(out["rms"]["fused"])
    assert out["n"]["a"] == 3
    assert out["corr"]["a-b"][2] < 0  # z errors anti-correlated in this example


def test_still_cameras_capture_even_when_one_drops_out():
    """Two still cameras see the board at very different positions in their own views.
    When one camera misses a frame, the pose used for the stillness check must not jump
    to the other camera's (that read as hundreds of mm of motion and never captured)."""
    from spt.live_measure import LiveMeasureSession
    a = StubMeasurer(2500, [0, 0, 0], t=(0, 0, 400.0))
    b = StubMeasurer(1500, [0, 0, 0], t=(300.0, 0, 500.0))
    fm = FusedMeasurer({"a": a, "b": b})
    session = LiveMeasureSession(fm, np.zeros(3), frames_per_capture=8)
    states = []
    for i in range(20):
        a.hidden = i % 2 == 1  # camera a misses every other frame
        states.append(session.update(f"g{i}", {"a": object(), "b": object()}).state)
    assert "captured" in states and states.index("captured") <= 9, states
