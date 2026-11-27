"""Monte Carlo comparison of probe marker designs: tip error per frame.

Simulates a camera viewing a probe (flat board or regular prism with markers on its
side faces) with noisy corner detections, solves the pose and measures how far the
estimated tip lands from the true tip. Use it to compare designs, not as absolute
accuracy: real errors run higher (blur, glare, flex, imperfect calibration). The S23
phone board measured ~1.5 mm pivot residual where this predicts ~0.2 mm.

    python scripts/probe_size_sim.py            # both tables (small work, body lines)

Markers are assumed to be 5x5-bit ArUco (7 cells across including the border); a
marker whose cells come out under MIN_CELL_PX pixels is treated as not detected.
"""

from __future__ import annotations

import cv2
import numpy as np

MIN_CELL_PX = 4.0  # below this ArUco decoding becomes unreliable
CELLS = 7  # 5x5 data bits + 1-cell border each side
ARUCO_SIGMA = 0.25  # px, corner noise for single ArUco markers
CHARUCO_SIGMA = 0.15  # px, ChArUco chessboard corners are more precise

CAMERAS = {
    # name: (fx, width, height)
    "NexiGo 1080p": (1471.0, 1920, 1080),  # calibrated
    "4K camera": (3400.0, 3840, 2160),  # e.g. UC40M at the wide end of its zoom (estimate)
}


def rot(axis, deg):
    r, _ = cv2.Rodrigues(np.asarray(axis, float) * np.radians(deg))
    return r


def face_marker(center, normal, up, size):
    n = normal / np.linalg.norm(normal)
    u = up / np.linalg.norm(up)
    r = np.cross(u, n)
    h = size / 2
    return [center + h * (-r + u), center + h * (r + u), center + h * (r - u), center + h * (-r - u)]


def prism(sides: int, face_w: float, length: float, per_face: int = 1, marker_frac: float = 0.8):
    """Regular prism along z, centred at the origin; `per_face` markers stacked along
    the axis on each side face. Returns [(corners, normal, marker_size)] per marker."""
    apothem = face_w / (2 * np.tan(np.pi / sides))
    seg = length / per_face
    size = marker_frac * min(face_w, seg)
    markers = []
    for k in range(sides):
        a = 2 * np.pi * k / sides
        n = np.array([np.cos(a), np.sin(a), 0.0])
        for j in range(per_face):
            zc = -length / 2 + seg * (j + 0.5)
            markers.append((face_marker(apothem * n + np.array([0, 0, zc]), n, np.array([0, 0, 1.0]), size), n, size))
    return markers


def flat_charuco(w, h, nx, ny, square):
    """Planar grid of ChArUco inner corners facing +x; `square` sets detectability."""
    ys, zs = np.linspace(-w / 2, w / 2, nx), np.linspace(-h / 2, h / 2, ny)
    pts = [np.array([0.0, y, z]) for y in ys for z in zs]
    return [(pts, np.array([1.0, 0, 0]), 0.7 * square)]


def simulate(markers, tip, distance, view_deg, camera="NexiGo 1080p", sigma=ARUCO_SIGMA, trials=400, seed=0):
    """RMS tip error (mm) and the fraction of frames where the probe was detected."""
    rng = np.random.default_rng(seed)
    fx, w, h = CAMERAS[camera]
    K = np.array([[fx, 0, w / 2], [0, fx, h / 2], [0, 0, 1]])
    base = np.array([[0, 1, 0], [0, 0, -1], [-1, 0, 0]], float)  # probe x -> towards camera, z -> up
    errs, seen = [], 0
    for _ in range(trials):
        R = base @ rot([0, 0, 1], view_deg + rng.normal(0, 3)) @ rot([0, 1, 0], rng.normal(0, 5))
        t = np.array([rng.normal(0, 0.05 * distance), rng.normal(0, 0.03 * distance), distance])
        obj = []
        for corners, n, size in markers:
            nc = R @ n
            pc = R @ np.mean(corners, axis=0) + t
            cos_view = float(np.dot(nc, -pc / np.linalg.norm(pc)))
            if cos_view < np.cos(np.radians(75)):
                continue
            cell_px = fx * size / CELLS / pc[2] * np.sqrt(cos_view)  # rough foreshortening
            if cell_px < MIN_CELL_PX:
                continue
            obj.extend(corners)
        if len(obj) < 4:
            continue
        seen += 1
        obj = np.array(obj)
        proj, _ = cv2.projectPoints(obj, cv2.Rodrigues(R)[0], t, K, None)
        proj = proj.reshape(-1, 2) + rng.normal(0, sigma, (len(obj), 2))
        normal = np.linalg.svd(obj - obj.mean(0))[2][2]
        planar = np.ptp(obj @ normal) < 1e-6
        _, rv, tv = cv2.solvePnP(obj, proj, K, None, flags=cv2.SOLVEPNP_IPPE if planar else cv2.SOLVEPNP_SQPNP)
        rv, tv = cv2.solvePnPRefineLM(obj, proj, K, None, rv, tv)
        Re, _ = cv2.Rodrigues(rv)
        errs.append(np.linalg.norm((Re @ tip + tv.ravel()) - (R @ tip + t)))
    if not errs:
        return float("nan"), 0.0
    return float(np.sqrt(np.mean(np.square(errs)))), seen / trials


def table(title, designs, tips, distances, views, camera):
    print(f"\n{title}  [{camera}]")
    cols = [f"{d / 1000:g} m {v:>2} deg" for d in distances for v in views]
    print(f"{'design':48} {'tip':>5}  " + "  ".join(f"{c:>12}" for c in cols))
    for name, (markers, sigma) in designs.items():
        for tip_mm in tips:
            tip = np.array([0, 0, -float(tip_mm)])
            cells = []
            for d in distances:
                for v in views:
                    err, seen = simulate(markers, tip, d, v, camera, sigma)
                    cells.append("not seen" if seen < 0.5 else f"{err:6.2f}" + ("*" if seen < 0.95 else " "))
            print(f"{name:48} {tip_mm:>5}  " + "  ".join(f"{c:>12}" for c in cells))
    print("  mm RMS per frame (before averaging); * = probe missed in some frames")


def main():
    small = {
        "phone board 60x120 (ChArUco, 15 mm squares)": (flat_charuco(60, 120, 3, 7, 15), CHARUCO_SIGMA),
        "triangular prism, 30 mm faces": (prism(3, 30, 30), ARUCO_SIGMA),
        "square prism, 40 mm faces": (prism(4, 40, 40), ARUCO_SIGMA),
        "square prism 40w x 80 long, 2 markers/face": (prism(4, 40, 80, 2), ARUCO_SIGMA),
        "hex prism 30w x 60 long, 2 markers/face": (prism(6, 30, 60, 2), ARUCO_SIGMA),
    }
    table("Small work (0.4-0.8 m)", small, [50, 80, 120], [400, 800], [0, 45], "NexiGo 1080p")

    big = {
        "hex prism 30w x 60 long, 2 markers/face": (prism(6, 30, 60, 2), ARUCO_SIGMA),
        "hex prism 50w x 100 long, 2 markers/face": (prism(6, 50, 100, 2), ARUCO_SIGMA),
        "hex prism 60w x 120 long, 2 markers/face": (prism(6, 60, 120, 2), ARUCO_SIGMA),
        "hex prism 80w x 160 long, 2 markers/face": (prism(6, 80, 160, 2), ARUCO_SIGMA),
    }
    for cam in CAMERAS:
        table("Body lines (1.5-3.5 m)", big, [150, 300], [1500, 2500, 3500], [0, 30], cam)


if __name__ == "__main__":
    main()
