"""Two cameras from different directions: the point covariance from each camera's
board fits is right, and covariance-weighted fusion beats each camera alone."""

from types import SimpleNamespace

import cv2
import numpy as np

from spt.boards import REFERENCE, TARGET
from spt.detect import Detection, Observed
from spt.measure import PointMeasurer
from spt.multi import fuse_points

K = np.array([[2000.0, 0, 960], [0, 2000.0, 540], [0, 0, 1]])
TGT_IN_REF = np.array([260.0, 0.0, 0.0])  # target board beside the reference, same plane
SIGMA_PX = 0.3


def rot_y(deg):
    return cv2.Rodrigues(np.array([0.0, np.radians(deg), 0.0]))[0]


def camera(deg, dist=550.0):
    """Board -> camera pose looking at the middle of both boards from `deg` to the side."""
    R = rot_y(deg)
    centre = np.array([230.0, 75.0, 0.0])
    t = np.array([0.0, 0.0, dist]) - R @ centre
    return R, t


def observe(R, t, rng):
    boards = {}
    for spec, offset in ((REFERENCE, np.zeros(3)), (TARGET, TGT_IN_REF)):
        obj = np.asarray(spec.cv_board().getChessboardCorners(), np.float64) + offset
        img, _ = cv2.projectPoints(obj, cv2.Rodrigues(R)[0], t, K, None)
        img = img.reshape(-1, 2) + rng.normal(0, SIGMA_PX, (len(obj), 2))
        boards[spec.name] = Detection(img.astype(np.float32), np.arange(len(obj), dtype=np.int32))
    return Observed(boards, (1920, 1080))


def test_point_covariance_and_fusion():
    rng = np.random.default_rng(1)
    calib = SimpleNamespace(camera_matrix=K, dist_coeffs=np.zeros(5))
    m = PointMeasurer(calib, REFERENCE, TARGET, max_rms_px=5.0)
    truth = TARGET.corner_xyz(0) + TGT_IN_REF
    cams = {"front": camera(0.0), "side": camera(40.0)}
    errs = {k: [] for k in ["front", "side", "cov", "fx2"]}
    predicted = {k: [] for k in cams}
    for _ in range(150):
        res = {}
        for name, (R, t) in cams.items():
            r, why = m.measure(name, observe(R, t, rng))
            assert r is not None, why
            res[name] = r
            errs[name].append(r.point - truth)
            predicted[name].append(r.cov)
        errs["cov"].append(fuse_points(res, {}, "cov") - truth)
        errs["fx2"].append(fuse_points(res, {"front": 1.0, "side": 1.0}, "fx2") - truth)
    rms = {k: float(np.sqrt(np.mean(np.sum(np.square(v), axis=1)))) for k, v in errs.items()}

    for name in cams:
        empirical = np.cov(np.array(errs[name]).T)
        pred = np.mean(predicted[name], axis=0)
        # the prediction is right in size (within 30 %) ...
        assert 0.7 < np.trace(pred) / np.trace(empirical) < 1.3, (name, np.trace(pred), np.trace(empirical))
        # ... and its weak direction is the camera's line of sight
        weak = np.linalg.eigh(pred)[1][:, -1]
        R, _ = cams[name]
        sight = R.T @ np.array([0.0, 0.0, 1.0])  # camera z axis in board coordinates
        assert abs(weak @ sight) > 0.9, name

    # each camera alone is limited by depth; fused by covariance, the views cover for each other
    assert rms["cov"] < 0.75 * min(rms["front"], rms["side"]), rms
    assert rms["cov"] <= rms["fx2"] + 1e-9, rms
