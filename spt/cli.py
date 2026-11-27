"""Command line: python -m spt {make-boards,calibrate,measure} ..."""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import cv2
import numpy as np

from .boards import BOARDS, REFERENCE, TARGET, render_raster
from .calibration import CameraCalibration, calibrate, coverage_image
from .cameras import FpsMeter, LiveCamera, find_camera, list_cameras, sharpness
from .detect import BoardDetector, draw_detection, image_size, list_images, load_gray, most_common_size
from .display import LAYOUTS, Scene, list_monitors, make_scene, pitch_from_diagonal, show
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


def cmd_monitors(args: argparse.Namespace) -> int:
    for m in list_monitors():
        print(f"  {m.index}: {m.width} x {m.height} px at ({m.x}, {m.y}){'  primary' if m.primary else ''}")
    print("Use the number with: python -m spt show --monitor N --diagonal-in SIZE")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    monitors = {m.index: m for m in list_monitors()}
    if args.monitor not in monitors:
        raise SystemExit(f"no monitor {args.monitor}; available: {sorted(monitors)} (see: python -m spt monitors)")
    mon = monitors[args.monitor]
    if args.pitch_mm:
        pitch = args.pitch_mm
    elif args.diagonal_in:
        pitch = pitch_from_diagonal(args.diagonal_in, mon.width, mon.height)
    else:
        raise SystemExit("give the screen size: --diagonal-in (e.g. 30) or --pitch-mm")
    scene = make_scene(mon.width, mon.height, pitch, args.layout, monitor=mon.name)
    out = Path(args.out or f"output/scene_{args.layout}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    scene.save(out)

    print(f"Monitor {mon.index}: {mon.width} x {mon.height} px, pixel pitch {pitch:.4f} mm, layout '{args.layout}'")
    for b in scene.boards:
        w, h = b.spec.size_mm
        print(f"  {b.spec.name:9} square {b.square_px} px = {b.spec.square_mm:.2f} mm, board {w:.0f} x {h:.0f} mm")
    if scene.known_mm() is not None:
        t = scene.truth()
        print(f"  truth: target corner 0 at x {t[0]:.2f}  y {t[1]:.2f}  z 0 mm, {scene.known_mm():.2f} mm from reference corner 0")
    print(f"wrote {out}")
    print("Showing full screen. Esc or q closes it.")
    w, h = show(scene, mon, args.seconds)
    if (w, h) != (mon.width, mon.height):
        print(f"WARNING: the window was {w} x {h}, not {mon.width} x {mon.height}: the boards were "
              "scaled and the scene file does not match what was shown.")
    return 0


def cmd_cameras(args: argparse.Namespace) -> int:
    cameras = list_cameras(with_modes=True, include_virtual=args.all)
    if not cameras:
        print("no cameras found")
        return 1
    for cam in cameras:
        print(f"  {cam.index}: {cam.name}{'  (virtual)' if cam.virtual else ''}")
        by_format: dict[str, list] = {}
        for m in cam.modes:
            by_format.setdefault(m.fourcc, []).append(m)
        for fourcc, modes in by_format.items():
            shown = ", ".join(f"{m.width}x{m.height}@{m.fps:.0f}" for m in modes[:8])
            more = f" (+{len(modes) - 8} more)" if len(modes) > 8 else ""
            print(f"       {fourcc:5} {shown}{more}")
        best = cam.best_mode()
        if best:
            print(f"       default: {best}")
        if args.test and not cam.virtual and best:
            try:
                with LiveCamera(cam.index, best.width, best.height, best.fps, best.fourcc) as live:
                    first = live.read(timeout=8.0)
                    time.sleep(2.0)
                    last = live.read(after=first.number, timeout=5.0)
                    w, h = live.size
                    rate = (last.number - first.number) / (last.time - first.time)
                    print(f"       test:    got {w}x{h} {live.fourcc} at {rate:.1f} fps")
            except (RuntimeError, TimeoutError) as e:
                print(f"       test:    FAILED - {e}")
    print("\nPreview one with: python -m spt preview --camera NAME   (part of the name is enough)")
    return 0


def _put_lines(img: np.ndarray, lines: list[str]) -> None:
    """White status text on a darkened band across the top, readable on any scene."""
    band = img[: 14 + 26 * len(lines)]
    band[:] = (band * 0.35).astype(img.dtype)
    for row, text in enumerate(lines):
        cv2.putText(img, text, (10, 28 + 26 * row), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1, cv2.LINE_AA)


def cmd_preview(args: argparse.Namespace) -> int:
    cam_info = find_camera(args.camera, list_cameras(with_modes=True, include_virtual=True))
    mode = cam_info.best_mode()
    width = height = None
    if args.mode:
        width, height = (int(v) for v in args.mode.lower().split("x"))
    elif mode:
        width, height = mode.width, mode.height
    fps = args.fps or (mode.fps if mode else None)

    specs = []
    if args.detect in ("reference", "both"):
        specs.append(REFERENCE)
    if args.detect in ("target", "both"):
        specs.append(TARGET)
    detectors = [BoardDetector(s) for s in specs]
    save_dir = Path(args.save_dir)

    live = LiveCamera(cam_info.index, width, height, fps, args.fourcc).start()
    window = f"Preview - {cam_info.name}"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    shown = FpsMeter()
    detect_on = bool(detectors)
    last = 0
    stop_at = None if args.seconds is None else time.perf_counter() + args.seconds
    print(f"{cam_info.name}: requested {width}x{height} @ {fps} fps {args.fourcc}")
    print("Keys: q/Esc quit, s save frame, c camera settings dialog, d detection on/off")
    try:
        while True:
            try:
                frame = live.read(after=last, timeout=5.0)
            except TimeoutError as e:
                print(e)
                return 1
            last = frame.number
            started = time.perf_counter()
            gray = cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY)
            dets = [d.detect(gray) for d in detectors] if detect_on else []
            sharp = sharpness(gray)
            work_ms = (time.perf_counter() - started) * 1000
            shown.tick()

            disp = draw_detection(frame.image, *dets, max_side=args.max_side)
            w, h = live.size
            lag_ms = (time.perf_counter() - frame.time) * 1000
            parts = [f"sharpness {sharp:.0f}"]
            for spec, det in zip(specs, dets):
                parts.append(f"{spec.name} {0 if det is None else len(det)}/{spec.corner_count} corners")
            _put_lines(disp, [
                f"{cam_info.name}  {w}x{h} {live.fourcc}  camera {live.fps:.1f} fps  shown {shown.fps:.1f} fps  "
                f"processing {work_ms:.0f} ms  lag {lag_ms:.0f} ms",
                "   ".join(parts),
                "q quit   s save frame   c camera settings   d detection on/off",
            ])
            cv2.imshow(window, disp)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                break
            if stop_at is not None and time.perf_counter() >= stop_at:
                if args.save_last:
                    cv2.imwrite(args.save_last, disp)
                print(f"camera {live.fps:.1f} fps, shown {shown.fps:.1f} fps, processing {work_ms:.0f} ms")
                break
            if key == ord("s"):
                save_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
                safe = "".join(ch if ch.isalnum() else "_" for ch in cam_info.name).strip("_")
                path = save_dir / f"{safe}_{stamp}.png"
                cv2.imwrite(str(path), frame.image)
                print(f"saved {path}")
            elif key == ord("c"):
                live.open_settings()
            elif key == ord("d") and detectors:
                detect_on = not detect_on
    finally:
        live.stop()
        cv2.destroyAllWindows()
    return 0


def _load_views(folder: str):
    """Yield (path, gray) for every image in the folder."""
    paths = list_images(folder)
    if not paths:
        raise SystemExit(f"no images found in {folder}")
    for path in paths:
        yield path, load_gray(path)


def cmd_calibrate(args: argparse.Namespace) -> int:
    square_mm = args.square_mm
    if args.scene and square_mm is None:
        placed = Scene.load(args.scene).board(args.board)
        if placed is None:
            raise SystemExit(f"the scene file has no {args.board} board")
        square_mm = placed.spec.square_mm
    spec = BOARDS[args.board].scaled(square_mm)
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
    if debug:
        used = {v["file"] for v in calib.views}
        cv2.imwrite(str(debug / "coverage.png"),
                    coverage_image([d.corners for n, d in views if n in used], size))

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
    ref_square, tgt_square, known, truth = args.ref_square_mm, args.tgt_square_mm, args.known_mm, None
    if args.scene:
        scene = Scene.load(args.scene)
        ref_b, tgt_b = scene.board("reference"), scene.board("target")
        if ref_b is None or tgt_b is None:
            raise SystemExit("the scene file must show both boards (layout 'measure')")
        ref_square = ref_square or ref_b.spec.square_mm
        tgt_square = tgt_square or tgt_b.spec.square_mm
        truth = scene.truth(args.ref_corner, args.tgt_corner)
        known = known or scene.known_mm(args.ref_corner, args.tgt_corner)
    ref_spec = REFERENCE.scaled(ref_square)
    tgt_spec = TARGET.scaled(tgt_square)
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

    s = summarize(results, known, truth)
    print(f"\nMeasurement: target corner {args.tgt_corner} relative to reference corner {args.ref_corner}")
    print(f"  squares used    reference {ref_spec.square_mm:.3f} mm, target {tgt_spec.square_mm:.3f} mm")
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
    if s.truth is not None:
        tx, ty, tz = s.truth
        ex, ey, ez = s.mean_error
        print(f"  truth           x {tx:.2f}  y {ty:.2f}  z {tz:.2f} mm (exact, from the screen layout)")
        print(f"  mean - truth    x {ex:+.2f}  y {ey:+.2f}  z {ez:+.2f} mm")
        print(f"  per-view error  RMS {s.rms_error_3d:.2f} mm, worst {s.max_error_3d:.2f} mm")
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
    if s.rms_error_3d is not None:
        parts.append(f"Single-photo 3D error (RMS) {s.rms_error_3d:.2f} mm: "
                     + ("within target." if s.rms_error_3d <= target_mm else "OUTSIDE target."))
    return "\n  ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m spt", description="Spatial Point Tracker phase 0 tools")
    sub = p.add_subparsers(dest="command", required=True)

    mb = sub.add_parser("make-boards", help="write the printable reference + target boards (vector PDF)")
    mb.add_argument("--paper", choices=sorted(PAPER_MM), default="letter")
    mb.add_argument("--out", default="output/boards")
    mb.set_defaults(func=cmd_make_boards)

    ca = sub.add_parser("cameras", help="list cameras and their modes")
    ca.add_argument("--all", action="store_true", help="include virtual cameras")
    ca.add_argument("--test", action="store_true", help="open each camera and measure its real frame rate")
    ca.set_defaults(func=cmd_cameras)

    pv = sub.add_parser("preview", help="live camera view with optional board detection")
    pv.add_argument("--camera", required=True, help="index or part of the name (see 'cameras')")
    pv.add_argument("--mode", help="WIDTHxHEIGHT (default: largest MJPG mode at 15+ fps)")
    pv.add_argument("--fps", type=float)
    pv.add_argument("--fourcc", default="MJPG")
    pv.add_argument("--detect", choices=["none", "reference", "target", "both"], default="both")
    pv.add_argument("--save-dir", default="captures/preview")
    pv.add_argument("--max-side", type=int, default=1600, help="display size limit (px)")
    pv.add_argument("--seconds", type=float, help="close automatically after this long")
    pv.add_argument("--save-last", help="with --seconds: save the last displayed view here")
    pv.set_defaults(func=cmd_preview)

    mo = sub.add_parser("monitors", help="list monitors (for show)")
    mo.set_defaults(func=cmd_monitors)

    sh = sub.add_parser("show", help="show boards full screen on a monitor instead of printing")
    sh.add_argument("--monitor", type=int, required=True, help="monitor number from 'monitors'")
    sh.add_argument("--layout", choices=LAYOUTS, default="measure",
                    help="measure: both boards (exact truth); calib: large reference; reference/target: one board")
    size = sh.add_mutually_exclusive_group()
    size.add_argument("--diagonal-in", type=float, help="visible screen diagonal in inches")
    size.add_argument("--pitch-mm", type=float, help="pixel pitch in mm")
    sh.add_argument("--out", help="scene file (default output/scene_<layout>.json)")
    sh.add_argument("--seconds", type=float, help="close automatically after this long")
    sh.set_defaults(func=cmd_show)

    cal = sub.add_parser("calibrate", help="calibrate a camera from photos of one board")
    cal.add_argument("photos", help="folder of calibration photos")
    cal.add_argument("--board", choices=sorted(BOARDS), default="reference")
    cal.add_argument("--square-mm", type=float, help="measured printed square size (default: nominal)")
    cal.add_argument("--scene", help="scene file from 'show' (sets the square size)")
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
    me.add_argument("--scene", help="scene file from 'show --layout measure': exact truth and square sizes")
    me.add_argument("--target-mm", type=float, default=2.0, help="accuracy goal (default 2 mm)")
    me.add_argument("--max-rms", type=float, default=1.0, help="reject views whose board fit is worse (px)")
    me.add_argument("--csv", help="write per-view results here")
    me.add_argument("--debug-dir", help="save detection overlays here")
    me.set_defaults(func=cmd_measure)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
