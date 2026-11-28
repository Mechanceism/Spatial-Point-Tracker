"""Marker maps: rigid sets of markers (and boards) whose real layout is measured.

A map is a set of *elements*, each a rigid set of points with known local
coordinates and a pose in the map frame:

- ``m<id>``: one ArUco marker, its 4 corners (TL, TR, BR, BL) as a square of known
  side; local frame x right, y up, z out of the marker (towards the viewer).
- ``b:<name>``: a ChArUco board, its chessboard corners in the board's own frame.

Two uses:

- :func:`locate`: the map's pose from whatever elements a camera sees (any faces of a
  prism probe, any of several boards), one PnP over all their points.
- :func:`refine`: bundle adjustment. From frames of a calibrated camera it solves for
  every frame's pose and every element's pose together, starting from a nominal map
  (the design). That measures where glued markers / placed boards really are.

The gauge (which frame the map is in) is fixed by holding one element still during
refinement; afterwards the whole result is moved rigidly onto the starting map (best
fit over all points), so the map stays in the design frame and nominal offsets such
as a probe tip keep their meaning. The scale comes from the known marker / square
sizes, so print them accurately (check the 100 mm bar).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .boards import BoardSpec
from .pose import Pose, _pose_covariance

# Loose markers and prism markers: DICT_5X5_250. Its first 100 markers are exactly
# DICT_5X5_100 (the boards' dictionary), so boards (IDs 0-55) and prisms (60-83) are
# unchanged; loose reference markers use IDs 100-249.
MARKER_DICTIONARY = cv2.aruco.DICT_5X5_250
MARKER_DICTIONARY_NAME = "DICT_5X5_250"
FIRST_LOOSE_ID = 100


def square_corners(size: float) -> np.ndarray:
    """A marker's corners in its own frame: TL, TR, BR, BL (x right, y up)."""
    h = size / 2.0
    return np.array([[-h, h, 0.0], [h, h, 0.0], [h, -h, 0.0], [-h, -h, 0.0]])


def rigid_fit(src: np.ndarray, dst: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """R, t minimising |R src + t - dst| (Kabsch, no scale)."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    cs, cd = src.mean(0), dst.mean(0)
    U, _, Vt = np.linalg.svd((src - cs).T @ (dst - cd))
    D = np.diag([1.0, 1.0, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    return R, cd - R @ cs


@dataclass
class Element:
    key: str
    local: np.ndarray  # (n, 3) points in the element's own frame
    R: np.ndarray  # element -> map
    t: np.ndarray
    kind: str = "marker"  # "marker" or "board"
    size: float | None = None  # marker side (mm)
    board: dict | None = None  # board definition (spt.boards fields) for "board"

    def points(self, idx: np.ndarray | None = None) -> np.ndarray:
        local = self.local if idx is None else self.local[idx]
        return local @ self.R.T + self.t


@dataclass
class MarkerMap:
    name: str = "map"
    elements: dict[str, Element] = field(default_factory=dict)
    extras: dict = field(default_factory=dict)  # e.g. a probe's nominal tip

    # -- building -------------------------------------------------------------
    def add_marker(self, marker_id: int, size: float, corners: np.ndarray | None = None,
                   R: np.ndarray | None = None, t: np.ndarray | None = None) -> Element:
        """A marker placed by its 4 corners in the map frame (TL, TR, BR, BL), or by R, t."""
        local = square_corners(size)
        if corners is not None:
            R, t = rigid_fit(local, corners)
        el = Element(f"m{marker_id}", local, np.eye(3) if R is None else R, np.zeros(3) if t is None else t,
                     "marker", size)
        self.elements[el.key] = el
        return el

    def add_board(self, spec: BoardSpec, R: np.ndarray | None = None, t: np.ndarray | None = None) -> Element:
        n = (spec.squares_x - 1) * (spec.squares_y - 1)
        local = np.array([spec.corner_xyz(i) for i in range(n)], float)
        board = {"name": spec.name, "squares_x": spec.squares_x, "squares_y": spec.squares_y,
                 "square_mm": spec.square_mm, "marker_mm": spec.marker_mm, "first_id": spec.first_id}
        el = Element(f"b:{spec.name}", local, np.eye(3) if R is None else R, np.zeros(3) if t is None else t,
                     "board", board=board)
        self.elements[el.key] = el
        return el

    @classmethod
    def from_prism(cls, design) -> "MarkerMap":
        """The nominal map of a printed prism probe (spt.prism.PrismDesign)."""
        m = cls(design.name, extras={"nominal_tip": design.nominal_tip.tolist(), "design": design.name})
        for mid, corners in design.nominal_markers().items():
            m.add_marker(mid, design.marker_mm, corners)
        return m

    def copy(self) -> "MarkerMap":
        return MarkerMap(self.name, {k: Element(e.key, e.local.copy(), e.R.copy(), e.t.copy(), e.kind, e.size,
                                                 None if e.board is None else dict(e.board))
                                     for k, e in self.elements.items()}, dict(self.extras))

    def transformed(self, R: np.ndarray, t: np.ndarray) -> "MarkerMap":
        """The same map moved rigidly: X' = R X + t."""
        out = self.copy()
        for e in out.elements.values():
            e.R, e.t = R @ e.R, R @ e.t + t
        return out

    def all_points(self, keys=None) -> np.ndarray:
        keys = sorted(self.elements) if keys is None else keys
        return np.vstack([self.elements[k].points() for k in keys])

    # -- files ----------------------------------------------------------------
    def to_json(self) -> dict:
        els = {}
        for k, e in self.elements.items():
            d = {"kind": e.kind, "R": np.round(e.R, 9).tolist(), "t": np.round(e.t, 6).tolist()}
            if e.kind == "marker":
                d["size"] = e.size
            else:
                d["board"] = e.board
            els[k] = d
        return {"format": 1, "name": self.name, "dictionary": MARKER_DICTIONARY_NAME, "elements": els,
                "extras": self.extras}

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=1))

    @classmethod
    def load(cls, path: str | Path) -> "MarkerMap":
        d = json.loads(Path(path).read_text())
        m = cls(d.get("name", "map"), extras=d.get("extras", {}))
        for k, e in d["elements"].items():
            R, t = np.array(e["R"], float), np.array(e["t"], float)
            if e["kind"] == "marker":
                m.add_marker(int(k[1:]), float(e["size"]), R=R, t=t)
            else:
                m.add_board(BoardSpec(**{key: e["board"][key] for key in
                                         ("name", "squares_x", "squares_y", "square_mm", "marker_mm", "first_id")}),
                            R=R, t=t)
        return m


# -- observations ------------------------------------------------------------
# One frame's observation: element key -> (point indices into element.local, (n, 2) pixels)
Observation = dict[str, tuple[np.ndarray, np.ndarray]]


def observe_markers(markers: dict[int, np.ndarray]) -> Observation:
    """Detected markers (id -> 4x2 corners TL, TR, BR, BL) as an observation."""
    return {f"m{int(i)}": (np.arange(4), np.asarray(c, float).reshape(4, 2)) for i, c in markers.items()}


def observe_boards(boards: dict) -> Observation:
    """ChArUco detections (name -> spt.detect.Detection) as an observation."""
    return {f"b:{name}": (np.asarray(d.ids, int), np.asarray(d.corners, float).reshape(-1, 2))
            for name, d in boards.items() if d is not None and len(d) > 0}


class MarkerDetector:
    """ArUco markers (MARKER_DICTIONARY) with sub-pixel corners: id -> (4, 2)."""

    def __init__(self):
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self._det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(MARKER_DICTIONARY), params)

    def detect(self, gray: np.ndarray) -> dict[int, np.ndarray]:
        corners, ids, _ = self._det.detectMarkers(gray)
        if ids is None:
            return {}
        return {int(i): c.reshape(4, 2).astype(np.float64) for i, c in zip(ids.ravel(), corners)}


# -- pose from a map ----------------------------------------------------------
def _gather(mmap: MarkerMap, obs: Observation, keys=None):
    obj, img = [], []
    for k, (idx, px) in obs.items():
        if k in mmap.elements and (keys is None or k in keys):
            obj.append(mmap.elements[k].points(idx))
            img.append(px)
    if not obj:
        return None, None
    return np.vstack(obj), np.vstack(img)


def locate(mmap: MarkerMap, obs: Observation, calib, min_points: int = 4) -> Pose | None:
    """Pose of the map (map -> camera) from every visible element of it."""
    obj, img = _gather(mmap, obs)
    if obj is None or len(obj) < min_points:
        return None
    K, dist = calib.camera_matrix, calib.dist_coeffs
    o, i = obj.astype(np.float64), img.astype(np.float64)
    planar = np.linalg.svd(o - o.mean(0), compute_uv=False)[-1] < 1e-6 * max(1.0, np.ptp(o))
    if planar:
        ok, rvec, tvec = cv2.solvePnP(o, i, K, dist, flags=cv2.SOLVEPNP_IPPE)
    else:
        ok, rvec, tvec = cv2.solvePnP(o, i, K, dist, flags=cv2.SOLVEPNP_SQPNP)
    if not ok:
        return None
    rvec, tvec = cv2.solvePnPRefineLM(o, i, K, dist, rvec, tvec)
    proj, jac = cv2.projectPoints(o, rvec, tvec, K, dist)
    rms = float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - i) ** 2, axis=1))))
    return Pose(cv2.Rodrigues(rvec)[0], tvec.reshape(3).astype(np.float64), rms, len(o),
                _pose_covariance(jac[:, :6], rms, len(o)))


# -- bundle adjustment ----------------------------------------------------------
@dataclass
class RefineReport:
    frames_used: int
    frames_dropped: int
    rms_before: float
    rms_after: float
    iterations: int
    observations: dict[str, int]  # element -> frames it was seen in
    moved_mm: dict[str, float]  # element -> RMS corner change vs the starting map
    frame_rms: list[float]
    element_rms: dict[str, float]  # element -> reprojection RMS over its observations


def refine(start: MarkerMap, frames: list[Observation], calib, fixed: str | None = None,
           iterations: int = 50, min_points: int = 8, min_seen: int = 2) -> tuple[MarkerMap, RefineReport]:
    """Bundle-adjust element poses (and frame poses) from frames of one calibrated camera.

    Elements seen in fewer than ``min_seen`` usable frames keep their starting pose.
    ``fixed`` is the element held still (default: the most observed one).
    """
    K, dist = calib.camera_matrix, calib.dist_coeffs
    # initial frame poses from the starting map
    usable, poses = [], []
    for obs in frames:
        obs = {k: v for k, v in obs.items() if k in start.elements}
        n = sum(len(v[0]) for v in obs.values())
        if n < min_points:
            continue
        p = locate(start, obs, calib)
        if p is None:
            continue
        usable.append(obs)
        poses.append(p)
    if not usable:
        raise ValueError("no frame shows enough of the map")
    seen: dict[str, int] = {}
    for obs in usable:
        for k in obs:
            seen[k] = seen.get(k, 0) + 1
    free_keys = sorted(k for k, n in seen.items() if n >= min_seen)
    if fixed is None:
        fixed = max(free_keys, key=lambda k: (seen[k], k))
    free_keys = [k for k in free_keys if k != fixed]
    el_index = {k: i for i, k in enumerate(free_keys)}
    nf, ne = len(usable), len(free_keys)

    work = start.copy()
    params = np.zeros(6 * nf + 6 * ne)
    for f, p in enumerate(poses):
        params[6 * f:6 * f + 3] = p.rvec
        params[6 * f + 3:6 * f + 6] = p.t
    for k, i in el_index.items():
        e = work.elements[k]
        params[6 * (nf + i):6 * (nf + i) + 3] = cv2.Rodrigues(e.R)[0].ravel()
        params[6 * (nf + i) + 3:6 * (nf + i) + 6] = e.t

    # observation list: (frame, key, idx, pixels) with residual row ranges
    obs_list = []
    row = 0
    for f, obs in enumerate(usable):
        for k, (idx, px) in obs.items():
            obs_list.append((f, k, np.asarray(idx), np.asarray(px, float), row))
            row += 2 * len(idx)
    n_rows = row

    def element_pose(p, k):
        if k in el_index:
            j = 6 * (nf + el_index[k])
            return cv2.Rodrigues(p[j:j + 3])[0], p[j + 3:j + 6]
        e = work.elements[k]
        return e.R, e.t

    def obs_residual(p, f, k, idx, px):
        Rc = cv2.Rodrigues(p[6 * f:6 * f + 3])[0]
        tc = p[6 * f + 3:6 * f + 6]
        Re, te = element_pose(p, k)
        R, t = Rc @ Re, Rc @ te + tc
        proj, _ = cv2.projectPoints(work.elements[k].local[idx], cv2.Rodrigues(R)[0], t, K, dist)
        return (proj.reshape(-1, 2) - px).ravel()

    def residuals(p):
        r = np.empty(n_rows)
        for f, k, idx, px, r0 in obs_list:
            r[r0:r0 + 2 * len(idx)] = obs_residual(p, f, k, idx, px)
        return r

    by_frame: dict[int, list] = {}
    by_elem: dict[str, list] = {}
    for o in obs_list:
        by_frame.setdefault(o[0], []).append(o)
        by_elem.setdefault(o[1], []).append(o)

    def jacobian(p, r):
        J = np.zeros((n_rows, len(p)))
        for j in range(len(p)):
            d = 1e-6 if j % 6 < 3 else 1e-4
            q = p.copy()
            q[j] += d
            if j < 6 * nf:
                group = by_frame.get(j // 6, [])
            else:
                group = by_elem.get(free_keys[j // 6 - nf], [])
            for f, k, idx, px, r0 in group:
                n = 2 * len(idx)
                J[r0:r0 + n, j] = (obs_residual(q, f, k, idx, px) - r[r0:r0 + n]) / d
        return J

    r = residuals(params)
    rms_before = float(np.sqrt(np.mean(r ** 2) * 2))
    cost = float(r @ r)
    lam = 1e-3
    it = 0
    for it in range(1, iterations + 1):
        J = jacobian(params, r)
        A = J.T @ J
        g = J.T @ r
        improved = False
        for _ in range(10):
            try:
                step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), -g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            trial = params + step
            r_try = residuals(trial)
            c_try = float(r_try @ r_try)
            if c_try < cost:
                params, r, improved = trial, r_try, True
                rel = (cost - c_try) / max(cost, 1e-12)
                cost = c_try
                lam = max(lam / 3, 1e-9)
                break
            lam *= 5
        if not improved or rel < 1e-10:
            break

    # write back, then put the map back onto the starting map (best rigid fit)
    for k in free_keys:
        e = work.elements[k]
        e.R, e.t = element_pose(params, k)
    keys = sorted(work.elements)
    Rfit, tfit = rigid_fit(work.all_points(keys), start.all_points(keys))
    out = work.transformed(Rfit, tfit)

    frame_rms, elem_sq = [], {}
    for f in range(nf):
        rr = np.concatenate([r[o[4]:o[4] + 2 * len(o[2])] for o in by_frame[f]])
        frame_rms.append(float(np.sqrt(np.mean(rr ** 2) * 2)))
    for k, group in by_elem.items():
        rr = np.concatenate([r[o[4]:o[4] + 2 * len(o[2])] for o in group])
        elem_sq[k] = float(np.sqrt(np.mean(rr ** 2) * 2))
    moved = {k: float(np.sqrt(np.mean(np.sum((out.elements[k].points() - start.elements[k].points()) ** 2, 1))))
             for k in keys}
    report = RefineReport(nf, len(frames) - nf, rms_before, float(np.sqrt(np.mean(r ** 2) * 2)), it, seen, moved,
                          frame_rms, elem_sq)
    return out, report
