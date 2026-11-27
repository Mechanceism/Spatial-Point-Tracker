"""Command line: python -m spt {make-boards,calibrate,measure} ..."""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import cv2
import numpy as np

from .boards import BOARDS, REFERENCE, TARGET, render_raster
from .calibration import CameraCalibration, calibrate
from .detect import BoardDetector, draw_detection, image_size, list_images, load_gray, most_common_size
from .measure import PointMeasurer, Summary, summarize
from .pdf import write_pdf
from .printable import PAPER_MM, board_page


def cmd_make_boards(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = out / f"boards_{args.paper}.pdf"
    write_pdf(pdf_path, [board_page(spec, args.paper) for spec in (REFERENCE, TARGET)])
    print(f"wrote {pdf_path}  (page 1: reference, page 2: target)")
    for spec in (REFERENCE, TARGET):
        png = out / f"{spec.name}_preview.png"
        cv2.imwrite(str(png), render_raster(spec, px_per_mm=6, margin_mm=8))
        w, h = spec.size_mm
        print(f"wrote {png}  ({spec.name}: {w:g} x {h:g} mm, square {spec.square_mm:g} mm)")
    print("Print the PDF at 100% / Actual size, then measure the squares (see docs/PHASE0.md).")
    return 0


def _load_views(folder: str):
    """Yield (path, gray) for every image in the folder."""
    paths = list_images(folder)
    if not paths:
        raise SystemExit(f"no images found in {folder}")
    for path in paths:
        yield path, load_gray(path)


def cmd_calibrate(args: argparse.Namespace) -> int:
    spec = BOARDS[args.board].scaled(args.square_mm)
    detector = BoardDetector(spec)
    debug = Path(args.debug_dir) if args.debug_dir else None
    if debug:
        debug.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    found, sizes, skipped = [], [], []
    for path, gray in _load_views(args.photos):
        det = detector.detect(gray)
        if debug:
            cv2.imwrite(str(debug / f"{path.stem}_det.jpg"), draw_detection(gray, det))
        if det is None or len(det) < 8:
            skipped.append((path.name, f"{0 if det is None else len(det)} corners"))
            continue
        found.append((path.name, det, image_size(gray)))
        sizes.append(image_size(gray))
    if not found:
        raise SystemExit("the board was not detected in any photo")

    size = most_common_size(sizes)
    views = []
    for name, det, s in found:
        if s != size:
            skipped.append((name, f"size {s[0]}x{s[1]} differs from {size[0]}x{size[1]}"))
        else:
            views.append((name, det))

    calib = calibrate(views, spec, size, rational=args.rational)
    if args.max_view_error is not None:
        worst = {v["file"] for v in calib.views if v["error_px"] > args.max_view_error}
        if worst and len(views) - len(worst) >= 4:
            for v in calib.views:
                if v["file"] in worst:
                    skipped.append((v["file"], f"view error {v['error_px']:.2f} px > {args.max_view_error}"))
            views = [(n, d) for n, d in views if n not in worst]
            calib = calibrate(views, spec, size, rational=args.rational)
    calib.save(args.out)
    elapsed = time.perf_counter() - started

    K = calib.camera_matrix
    std = calib.std_intrinsics
    print(f"\nCalibration ({spec.name} board, square {spec.square_mm:g} mm), {elapsed:.1f} s")
    print(f"  image size      {size[0]} x {size[1]}")
    print(f"  views used      {len(calib.views)}   skipped {len(skipped)}")
    for name, why in skipped:
        print(f"    - {name}: {why}")
    print(f"  RMS error       {calib.rms_px:.3f} px   {_rms_verdict(calib.rms_px)}")
    print(f"  fx, fy          {K[0, 0]:.1f} +/- {std.get('fx', 0):.1f},  {K[1, 1]:.1f} +/- {std.get('fy', 0):.1f} px")
    print(f"  cx, cy          {K[0, 2]:.1f} +/- {std.get('cx', 0):.1f},  {K[1, 2]:.1f} +/- {std.get('cy', 0):.1f} px")
    print(f"  distortion      {np.array2string(calib.dist_coeffs, precision=4, suppress_small=True)}")
    print(f"  frame coverage  {calib.coverage:.0%}   {'(good)' if calib.coverage >= 0.7 else '(add photos with the board near the edges/corners of the frame)'}")
    worst = sorted(calib.views, key=lambda v: -v["error_px"])[:5]
    print("  worst views     " + ", ".join(f"{v['file']} {v['error_px']:.2f}px" for v in worst))
    if calib.coverage < 0.5:
        print("\n  WARNING: corners cover under half of the frame, so the lens distortion is")
        print("  unreliable near the edges (low RMS does not mean a good calibration).")
        print("  Retake with the board in every part of the frame, especially the corners.")
    print(f"\nwrote {args.out}")
    return 0


def _rms_verdict(rms: float) -> str:
    if rms < 0.5:
        return "(good)"
    if rms < 1.0:
        return "(usable; check focus, flatness, motion blur)"
    return "(poor: focus changed, board not flat, or blurry photos?)"


def cmd_measure(args: argparse.Namespace) -> int:
    calib = CameraCalibration.load(args.calib)
    ref_spec = REFERENCE.scaled(args.ref_square_mm)
    tgt_spec = TARGET.scaled(args.tgt_square_mm)
    measurer = PointMeasurer(calib, ref_spec, tgt_spec, args.ref_corner, args.tgt_corner, args.max_rms)
    debug = Path(args.debug_dir) if args.debug_dir else None
    if debug:
        debug.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    results, skipped = [], []
    for path, gray in _load_views(args.photos):
        if image_size(gray) != tuple(calib.image_size):
            w, h = image_size(gray)
            skipped.append((path.name, f"size {w}x{h} differs from calibration {calib.image_size[0]}x{calib.image_size[1]}"))
            continue
        result, why = measurer.measure(path.name, gray)
        if debug:
            overlay = draw_detection(gray, measurer.ref.detect(gray), measurer.tgt.detect(gray))
            cv2.imwrite(str(debug / f"{path.stem}_det.jpg"), overlay)
        if result is None:
            skipped.append((path.name, why))
        else:
            results.append(result)
    elapsed = time.perf_counter() - started
    if not results:
        for name, why in skipped:
            print(f"  - {name}: {why}")
        raise SystemExit("no photo showed both boards")

    s = summarize(results, args.known_mm)
    print(f"\nMeasurement: target corner {args.tgt_corner} relative to reference corner {args.ref_corner}")
    print(f"  squares used    reference {ref_spec.square_mm:g} mm, target {tgt_spec.square_mm:g} mm")
    print(f"  views used      {s.views}   skipped {len(skipped)}   ({elapsed / max(1, s.views + len(skipped)):.2f} s per photo)")
    for name, why in skipped:
        print(f"    - {name}: {why}")
    print("\n  view                       x mm      y mm      z mm   dist mm   cam mm  ref px  tgt px")
    for r in results:
        x, y, z = r.point
        print(f"  {r.name[:24]:24} {x:9.2f} {y:9.2f} {z:9.2f} {r.distance_mm:9.2f} {r.camera_distance_mm:8.0f}  {r.ref.rms_px:6.2f}  {r.tgt.rms_px:6.2f}")
    x, y, z = s.mean_point
    sx, sy, sz = s.std_xyz
    print(f"\n  mean point      x {x:.2f}  y {y:.2f}  z {z:.2f} mm (reference-board frame)")
    print(f"  spread (1 sd)   x {sx:.2f}  y {sy:.2f}  z {sz:.2f} mm   3D {s.std_3d:.2f} mm, worst view {s.max_dev_3d:.2f} mm")
    print(f"  distance        mean {s.mean_distance:.2f} mm, sd {s.std_distance:.2f}, range {s.min_distance:.2f} - {s.max_distance:.2f}")
    if s.error_mm is not None:
        print(f"  vs known        {s.known_mm:.2f} mm -> error {s.error_mm:+.2f} mm")
    print("\n  " + _measure_verdict(s, args.target_mm))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["view", "x_mm", "y_mm", "z_mm", "distance_mm", "camera_distance_mm", "ref_rms_px", "tgt_rms_px", "ref_corners", "tgt_corners"])
            for r in results:
                w.writerow([r.name, *np.round(r.point, 4), round(r.distance_mm, 4), round(r.camera_distance_mm, 1),
                            round(r.ref.rms_px, 4), round(r.tgt.rms_px, 4), r.ref.corners, r.tgt.corners])
        print(f"\nwrote {args.csv}")
    return 0


def _measure_verdict(s: Summary, target_mm: float) -> str:
    # Rough guide: ~95 % of single captures fall within 2 sd of the mean.
    spread = 2 * s.std_3d
    parts = [f"Repeatability (2 sd) {spread:.2f} mm vs target {target_mm:g} mm: "
             + ("within target." if spread <= target_mm else "OUTSIDE target.")]
    if s.error_mm is not None:
        parts.append(f"Distance error {abs(s.error_mm):.2f} mm: "
                     + ("within target." if abs(s.error_mm) <= target_mm else "OUTSIDE target (check print scale / --square-mm first)."))
    return "\n  ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m spt", description="Spatial Point Tracker phase 0 tools")
    sub = p.add_subparsers(dest="command", required=True)

    mb = sub.add_parser("make-boards", help="write the printable reference + target boards (vector PDF)")
    mb.add_argument("--paper", choices=sorted(PAPER_MM), default="letter")
    mb.add_argument("--out", default="output/boards")
    mb.set_defaults(func=cmd_make_boards)

    cal = sub.add_parser("calibrate", help="calibrate a camera from photos of one board")
    cal.add_argument("photos", help="folder of calibration photos")
    cal.add_argument("--board", choices=sorted(BOARDS), default="reference")
    cal.add_argument("--square-mm", type=float, help="measured printed square size (default: nominal)")
    cal.add_argument("--out", default="output/camera.calib.json")
    cal.add_argument("--rational", action="store_true", help="8-coefficient distortion model (wide lenses)")
    cal.add_argument("--max-view-error", type=float, help="drop views above this error (px) and recalibrate once")
    cal.add_argument("--debug-dir", help="save detection overlays here")
    cal.set_defaults(func=cmd_calibrate)

    me = sub.add_parser("measure", help="locate the target corner in the reference frame across photos")
    me.add_argument("photos", help="folder of photos showing both boards")
    me.add_argument("--calib", default="output/camera.calib.json")
    me.add_argument("--ref-square-mm", type=float, help="measured reference square size")
    me.add_argument("--tgt-square-mm", type=float, help="measured target square size")
    me.add_argument("--ref-corner", type=int, default=0)
    me.add_argument("--tgt-corner", type=int, default=0)
    me.add_argument("--known-mm", type=float, help="hand-measured corner-to-corner distance")
    me.add_argument("--target-mm", type=float, default=2.0, help="accuracy goal (default 2 mm)")
    me.add_argument("--max-rms", type=float, default=1.0, help="reject views whose board fit is worse (px)")
    me.add_argument("--csv", help="write per-view results here")
    me.add_argument("--debug-dir", help="save detection overlays here")
    me.set_defaults(func=cmd_measure)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
