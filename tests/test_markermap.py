"""Marker maps: pose from any visible markers, and bundle adjustment of a glued prism."""

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from spt.boards import REFERENCE
from spt.markermap import (MarkerMap, MarkerDetector, locate, observe_markers, refine, rigid_fit,
                           square_corners, MARKER_DICTIONARY)
from spt.prism import SMALL

K = np.array([[2500.0, 0, 960], [0, 2500.0, 540], [0, 0, 1]])
CALIB = SimpleNamespace(camera_matrix=K, dist_coeffs=np.zeros(5))


def look_from(position, target=np.zeros(3)):
    """World -> camera pose for a camera at `position` looking at `target` (z up)."""
    c = np.asarray(position, float)
    z = (target - c) / np.linalg.norm(target - c)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ c


def perturbed(mmap, rng, deg=1.5, mm=0.5):
    """The 'real' prism: every marker glued a little off (rotation about its centre + shift)."""
    out = mmap.copy()
    for e in out.elements.values():
        centre = e.points().mean(0)
        dR = cv2.Rodrigues(rng.normal(size=3) / np.sqrt(3) * np.radians(deg))[0]
        e.R = dR @ e.R
        e.t = dR @ (e.t - centre) + centre + rng.uniform(-mm, mm, 3)
    return out


def views(true_map, rng, n=40, dist=300.0, noise=0.2):
    """Frames of the true map from around it: visible markers' corners with pixel noise."""
    frames, poses = [], []
    for i in range(n):
        az = 2 * np.pi * i / n + rng.normal(0, 0.1)
        el = np.radians(rng.uniform(-35, 35))
        cam = dist * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
        R, t = look_from(cam)
        markers = {}
        for k, e in true_map.elements.items():
            pts = e.points()
            normal = e.R[:, 2]
            to_cam = cam - pts.mean(0)
            if normal @ to_cam / np.linalg.norm(to_cam) < 0.35:  # facing away or too oblique
                continue
            px, _ = cv2.projectPoints(pts, cv2.Rodrigues(R)[0], t, K, None)
            markers[int(k[1:])] = px.reshape(4, 2) + rng.normal(0, noise, (4, 2))
        frames.append(observe_markers(markers))
        poses.append((R, t))
    return frames, poses


def aligned_to(m, ref):
    keys = sorted(ref.elements)
    R, t = rigid_fit(m.all_points(keys), ref.all_points(keys))
    return m.transformed(R, t)


def test_nominal_prism_map_matches_the_design():
    m = MarkerMap.from_prism(SMALL)
    assert len(m.elements) == 12
    for mid, corners in SMALL.nominal_markers().items():
        assert np.allclose(m.elements[f"m{mid}"].points(), corners, atol=1e-9)
    assert np.allclose(square_corners(24)[0], [-12, 12, 0])


def test_refine_recovers_a_badly_glued_prism():
    rng = np.random.default_rng(3)
    nominal = MarkerMap.from_prism(SMALL)
    truth = aligned_to(perturbed(nominal, rng), nominal)  # same frame convention as refine's output
    frames, _ = views(truth, rng)
    refined, rep = refine(nominal, frames, CALIB)
    assert rep.frames_used >= 35 and rep.rms_after < 0.3 < rep.rms_before
    err_nominal = np.sqrt(np.mean(np.sum((nominal.all_points() - truth.all_points()) ** 2, 1)))
    err_refined = np.sqrt(np.mean(np.sum((refined.all_points() - truth.all_points()) ** 2, 1)))
    assert err_nominal > 0.3  # the glue errors are real ...
    assert err_refined < 0.05, err_refined  # ... and measured to a few hundredths of a mm


def test_locate_uses_every_visible_face_and_the_refined_map_finds_the_tip_better():
    rng = np.random.default_rng(4)
    nominal = MarkerMap.from_prism(SMALL)
    truth = aligned_to(perturbed(nominal, rng, deg=2.0, mm=0.7), nominal)
    frames, _ = views(truth, rng, n=40)
    refined, _ = refine(nominal, frames, CALIB)
    tip = SMALL.nominal_tip
    test_frames, test_poses = views(truth, np.random.default_rng(9), n=20)
    errs = {"nominal": [], "refined": []}
    for obs, (R, t) in zip(test_frames, test_poses):
        assert len(obs) >= 2  # a hex prism always shows several markers
        true_tip = R @ tip + t
        for name, m in (("nominal", nominal), ("refined", refined)):
            p = locate(m, obs, CALIB)
            errs[name].append(np.linalg.norm(p.apply(tip) - true_tip))
    rms = {k: float(np.sqrt(np.mean(np.square(v)))) for k, v in errs.items()}
    assert rms["refined"] < 0.5 * rms["nominal"], rms


def test_save_load_roundtrip(tmp_path):
    m = MarkerMap.from_prism(SMALL)
    m.add_board(REFERENCE, t=np.array([500.0, 0, 0]))
    m.save(tmp_path / "m.json")
    back = MarkerMap.load(tmp_path / "m.json")
    assert set(back.elements) == set(m.elements)
    assert np.allclose(back.all_points(), m.all_points(), atol=1e-5)
    assert back.extras["nominal_tip"] == [0.0, 0.0, -80.0]


def test_marker_detector_reads_loose_and_prism_ids():
    d = cv2.aruco.getPredefinedDictionary(MARKER_DICTIONARY)
    canvas = np.full((400, 800), 255, np.uint8)
    for x, mid in ((50, 7), (300, 65), (550, 140)):  # a board id, a prism id, a loose id
        canvas[100:300, x:x + 200] = cv2.aruco.generateImageMarker(d, mid, 200, borderBits=1)
    found = MarkerDetector().detect(canvas)
    assert set(found) == {7, 65, 140}
    assert found[140].shape == (4, 2)
    assert np.allclose(found[140][0], [550, 100], atol=1.5)  # corner 0 = top-left


def test_refine_needs_frames():
    with pytest.raises(ValueError):
        refine(MarkerMap.from_prism(SMALL), [], CALIB)
