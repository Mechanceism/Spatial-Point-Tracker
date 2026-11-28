"""Collecting views of a prism probe, then building its map from them."""

import cv2
import numpy as np

from spt.markermap import MarkerMap, refine
from spt.prism import SMALL
from spt.probemap import ProbeMapSession
from test_markermap import CALIB, K, aligned_to, look_from, perturbed


def markers_seen(true_map, R, t, cam, rng, noise=0.2):
    out = {}
    for k, e in true_map.elements.items():
        pts = e.points()
        to_cam = cam - pts.mean(0)
        if e.R[:, 2] @ to_cam / np.linalg.norm(to_cam) < 0.35:
            continue
        px, _ = cv2.projectPoints(pts, cv2.Rodrigues(R)[0], t, K, None)
        out[int(k[1:])] = px.reshape(4, 2) + rng.normal(0, noise, (4, 2))
    return out


def test_turning_the_probe_collects_views_and_builds_the_map():
    rng = np.random.default_rng(5)
    nominal = MarkerMap.from_prism(SMALL)
    truth = aligned_to(perturbed(nominal, rng), nominal)
    session = ProbeMapSession(SMALL, nominal, CALIB)
    states = []
    for i in range(36):  # turn the probe 10 degrees at a time, up and down a little
        az = np.radians(10 * i)
        el = np.radians(20 * np.sin(i))
        cam = 300 * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
        R, t = look_from(cam)
        for _ in range(8):  # hold still for a few frames
            states.append(session.update(markers_seen(truth, R, t, cam, rng)).state)
    assert states[:4] == ["moving", "holding", "holding", "kept"]  # 3 still frames, then kept
    assert states.count("kept") >= 30
    assert "same" in states  # holding on after a kept view does not keep it again
    weak, missing = session.coverage()
    assert weak == [] and missing == []
    assert session.enough(30)
    refined, rep = refine(nominal, session.frames, CALIB)
    err = np.sqrt(np.mean(np.sum((refined.all_points() - truth.all_points()) ** 2, 1)))
    assert err < 0.05 and rep.rms_after < 0.3


def test_one_face_only_is_never_kept():
    rng = np.random.default_rng(6)
    nominal = MarkerMap.from_prism(SMALL)
    session = ProbeMapSession(SMALL, nominal, CALIB)
    cam = np.array([300.0, 0.0, 0.0])  # straight at face 0 (the neighbours show too, at 60 deg) ...
    R, t = look_from(cam)
    seen = markers_seen(nominal, R, t, cam, rng)
    assert {session.face_of(i) for i in seen} == {0, 1, 5}
    seen = {i: c for i, c in seen.items() if session.face_of(i) == 0}  # ... so keep only face 0's markers
    states = [session.update(seen).state for _ in range(6)]
    assert "kept" not in states and session.coverage()[1]  # neighbours never linked
