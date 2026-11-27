"""Command line: python -m spt {make-boards,calibrate,measure} ..."""

from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .boards import BOARDS, REFERENCE, TARGET, render_raster
from .calibration import CameraCalibration, calibrate, coverage_image
from .cameras import FpsMeter, LiveCamera, find_camera, list_cameras, sharpness
from .detect import BoardDetector, draw_detection, image_size, list_images, load_gray, most_common_size
from .display import LAYOUTS, Scene, list_monitors, make_scene, pitch_from_diagonal, show
from .guided import GuideConfig, GuidedCalibrator
from .live_measure import LiveMeasureSession
from .measure import PointMeasurer, Summary, summarize
from .pdf import write_pdf
from .printable import PAPER_MM, board_page
from .digitize import DigitizeSession
from .pivot import solve_pivot_robust
from .pose import Pose, estimate_pose
from .prism import DESIGNS, template_pages
from .probe import ProbeSpec, screen_image, screen_probe


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


def _put_lines(img: np.ndarray, lines: list[str], scale: float = 0.6) -> None:
    """White status text on a darkened band across the top, readable on any scene."""
    step = round(43 * scale)
    band = img[: round(23 * scale) + step * len(lines)]
    band[:] = (band * 0.35).astype(img.dtype)
    for row, text in enumerate(lines):
        cv2.putText(img, text, (10, round(47 * scale) + step * row), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (255, 255, 255), 1 if scale < 0.8 else 2, cv2.LINE_AA)


def _with_panel(img: np.ndarray, lines: list[str], scale: float = 0.9) -> np.ndarray:
    """The image with a dark text panel added below it (covers nothing)."""
    step = round(43 * scale)
    panel = np.full((round(23 * scale) + step * len(lines), img.shape[1], 3), 30, dtype=np.uint8)
    for row, text in enumerate(lines):
        cv2.putText(panel, text, (12, round(40 * scale) + step * row), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (255, 255, 255), 2 if scale >= 0.8 else 1, cv2.LINE_AA)
    return np.vstack([img, panel])


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


def _beep(freq: int = 1200, ms: int = 80) -> None:
    if sys.platform == "win32":
        import winsound
        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()


def _draw_guide(image: np.ndarray, guide: GuidedCalibrator, det, step, mirror: bool, max_side: int) -> np.ndarray:
    """Camera frame with the coverage grid (red = still needed), corners and a state
    border, mirrored like a selfie when the user faces the camera."""
    out = image.copy()
    h, w = out.shape[:2]
    gx, gy = guide.cfg.grid
    covered = guide.covered_cells
    tint = out.copy()
    for cx in range(gx):
        for cy in range(gy):
            if (cx, cy) not in covered:
                p0 = (round(cx * w / gx), round(cy * h / gy))
                p1 = (round((cx + 1) * w / gx), round((cy + 1) * h / gy))
                cv2.rectangle(tint, p0, p1, (0, 0, 255), -1)
    out = cv2.addWeighted(tint, 0.25, out, 0.75, 0)
    for cx in range(1, gx):
        cv2.line(out, (round(cx * w / gx), 0), (round(cx * w / gx), h), (200, 200, 200), 1)
    for cy in range(1, gy):
        cv2.line(out, (0, round(cy * h / gy)), (w, round(cy * h / gy)), (200, 200, 200), 1)
    if det is not None:  # plain dots: id labels would come out mirrored
        r = max(3, w // 300)
        for x, y in det.corners:
            cv2.circle(out, (round(x), round(y)), r, (0, 255, 0), -1, cv2.LINE_AA)
    if mirror:
        out = cv2.flip(out, 1)
    colors = {"captured": (0, 220, 0), "holding": (0, 220, 255), "rejected": (0, 0, 255),
              "duplicate": (255, 160, 0), "none": (0, 0, 200), "few": (0, 0, 200)}
    color = colors.get(step.state)
    if color:
        thick = max(6, w // 100)
        cv2.rectangle(out, (0, 0), (w - 1, h - 1), color, thick)
        if step.state == "holding":  # hold-still progress bar along the bottom
            cv2.rectangle(out, (0, h - thick * 2), (round(w * step.still), h), color, -1)
    scale = max_side / max(w, h)
    if scale < 1:
        out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return out


def cmd_calibrate_live(args: argparse.Namespace) -> int:
    cam_info = find_camera(args.camera, list_cameras(with_modes=True, include_virtual=True))
    mode = cam_info.best_mode()
    if args.mode:
        width, height = (int(v) for v in args.mode.lower().split("x"))
    else:
        width, height = (mode.width, mode.height) if mode else (None, None)
    square_mm = None
    if args.scene:
        placed = Scene.load(args.scene).board("reference")
        square_mm = placed.spec.square_mm if placed else None
    spec = REFERENCE.scaled(square_mm)
    safe = "".join(ch if ch.isalnum() else "_" for ch in cam_info.name).strip("_").lower()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    frames_dir = Path(args.frames_dir or f"captures/calib_live/{safe}_{stamp}")
    out = Path(args.out or f"output/{safe}.calib.json")

    live = LiveCamera(cam_info.index, width, height, args.fps or (mode.fps if mode else None), args.fourcc).start()
    first = live.read(timeout=8.0)
    size = (first.image.shape[1], first.image.shape[0])
    cfg = GuideConfig(min_views=args.min_views, target_coverage=args.coverage, max_rms=args.max_rms)
    guide = GuidedCalibrator(spec, size, cfg)
    frames: list[np.ndarray] = []

    window = f"Guided calibration - {cam_info.name}"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    if args.monitor:
        mon = {m.index: m for m in list_monitors()}.get(args.monitor)
        if mon:
            cv2.moveWindow(window, mon.x + 20, mon.y + 20)
            cv2.resizeWindow(window, min(mon.width - 40, args.max_side), min(mon.height - 80, round(args.max_side * size[1] / size[0])))
    print(f"{cam_info.name}: {size[0]}x{size[1]} {live.fourcc}. Hold the board in front of the camera; q saves and quits.")
    message, message_until, was_done = "", 0.0, False
    last = 0
    try:
        while True:
            frame = live.read(after=last, timeout=5.0)
            last = frame.number
            det = guide.detector.detect(cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY))
            step = guide.update(det, frame.time)
            if step.state == "captured":
                frames.append(frame.image.copy())
                _beep(1400, 70)
            elif step.state == "rejected":
                _beep(400, 200)
            if step.message and step.state in ("captured", "rejected", "duplicate"):
                message, message_until = step.message, time.perf_counter() + 1.5
            if guide.done() and not was_done:
                _beep(1800, 300)
                was_done = True

            disp = _draw_guide(frame.image, guide, det, step, not args.no_mirror, args.max_side)
            rms = "-" if guide.rms is None else f"{guide.rms:.2f} px"
            tx, ty = guide.tilt_range
            status = (f"views {len(guide.views)}/{cfg.min_views}   coverage {guide.coverage:.0%} (need {cfg.target_coverage:.0%})   "
                      f"tilt L/R {tx:.0f} F/B {ty:.0f} deg (need {cfg.min_tilt_deg:.0f})   RMS {rms}")
            now_msg = message if time.perf_counter() < message_until else (step.message or "")
            disp = _with_panel(disp, [status, f"> {guide.hint()}", now_msg, "q save and quit   Esc quit without saving"])
            cv2.imshow(window, disp)
            key = cv2.waitKey(1) & 0xFF
            if key == 27 or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                print("cancelled, nothing saved")
                return 1
            if key == ord("q"):
                break
    finally:
        live.stop()
        cv2.destroyAllWindows()

    if guide.calibration is None:
        print(f"only {len(guide.views)} views captured; need at least {cfg.calibrate_after}. Nothing saved.")
        return 1
    # Raw observations: the accepted frames, so the calibration can be redone offline.
    # A view is only ever dropped in the step that captured it (and its frame is
    # never stored), so frames and views correspond one to one.
    assert len(frames) == len(guide.views)
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i, image in enumerate(frames):
        cv2.imwrite(str(frames_dir / f"view{i:02d}.png"), image)
    n = len(frames)
    calib = guide.calibration
    calib.camera = {"name": cam_info.name, "width": size[0], "height": size[1], "fourcc": live.fourcc}
    out.parent.mkdir(parents=True, exist_ok=True)
    calib.save(out)
    _print_calibration(calib, spec, size, [], 0.0)
    print(f"  done criteria   {'met' if guide.done() else 'NOT met (saved anyway)'}")
    print(f"\nwrote {out}\nsaved {n} frames to {frames_dir} (python -m spt calibrate {frames_dir} redoes it offline)")
    return 0


def cmd_measure_live(args: argparse.Namespace) -> int:
    calib = CameraCalibration.load(args.calib)
    scene = Scene.load(args.scene)
    ref_b, tgt_b = scene.board("reference"), scene.board("target")
    if ref_b is None or tgt_b is None:
        raise SystemExit("the scene must show both boards (show --layout measure)")
    query = args.camera or calib.camera.get("name")
    if not query:
        raise SystemExit("give --camera (the calibration file doesn't name one)")
    cam_info = find_camera(query, list_cameras(with_modes=True, include_virtual=True))
    width, height = calib.image_size
    mode = cam_info.best_mode()
    measurer = PointMeasurer(calib, ref_b.spec, tgt_b.spec, max_rms_px=args.max_rms)
    session = LiveMeasureSession(measurer, scene.truth(), frames_per_capture=args.frames)
    safe = "".join(ch if ch.isalnum() else "_" for ch in cam_info.name).strip("_").lower()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    frames_dir = Path(f"captures/measure_live/{safe}_{stamp}")
    csv_path = Path(args.csv or f"output/measure_live_{safe}_{stamp}.csv")

    live = LiveCamera(cam_info.index, width, height, args.fps or (mode.fps if mode else None), args.fourcc).start()
    first = live.read(timeout=8.0)
    if (first.image.shape[1], first.image.shape[0]) != tuple(calib.image_size):
        live.stop()
        raise SystemExit(f"camera gives {first.image.shape[1]}x{first.image.shape[0]}, calibration is for "
                         f"{calib.image_size[0]}x{calib.image_size[1]}")
    window = f"Live measurement - {cam_info.name}"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    if args.monitor:
        mon = {m.index: m for m in list_monitors()}.get(args.monitor)
        if mon:
            cv2.moveWindow(window, mon.x + 20, mon.y + 20)
            cv2.resizeWindow(window, min(mon.width - 40, args.max_side), min(mon.height - 80, round(args.max_side * height / width)))
    truth_mm = scene.known_mm()
    print(f"{cam_info.name}: {width}x{height} {live.fourcc}. Truth: {truth_mm:.2f} mm. q finishes, Esc quits.")
    last, message, message_until = 0, "", 0.0
    colors = {"captured": (0, 220, 0), "collecting": (0, 220, 255), "none": (0, 0, 200), "move": (255, 160, 0)}
    try:
        while True:
            frame = live.read(after=last, timeout=5.0)
            last = frame.number
            gray = cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY)
            step = session.update(f"f{frame.number}", gray)
            if step.state == "captured":
                c = step.capture
                err = np.linalg.norm(c.mean - session.truth)
                frames_dir.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(frames_dir / f"capture{c.number:02d}.png"), frame.image)
                message, message_until = f"capture {c.number}: error {err:.2f} mm - now move or turn the screen", time.perf_counter() + 2.5
                _beep(1400, 70)

            out = frame.image.copy()
            r = max(3, out.shape[1] // 300)
            for det, color in zip(measurer.last_detections, ((0, 255, 0), (255, 200, 0))):
                if det is not None:
                    for x, y in det.corners:
                        cv2.circle(out, (round(x), round(y)), r, color, -1, cv2.LINE_AA)
            if not args.no_mirror:
                out = cv2.flip(out, 1)
            h, w = out.shape[:2]
            color = colors.get(step.state)
            if color:
                thick = max(6, w // 100)
                cv2.rectangle(out, (0, 0), (w - 1, h - 1), color, thick)
                if step.state == "collecting":
                    cv2.rectangle(out, (0, h - thick * 2), (round(w * step.progress), h), color, -1)
            scale = args.max_side / max(w, h)
            if scale < 1:
                out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

            if step.result is not None:
                e = step.result.point - session.truth
                line1 = (f"this frame: error x {e[0]:+.2f}  y {e[1]:+.2f}  z {e[2]:+.2f}  = {np.linalg.norm(e):.2f} mm   "
                         f"(camera {step.result.camera_distance_mm:.0f} mm away)")
            else:
                line1 = f"this frame: {step.reason}"
            st = session.stats()
            line2 = f"captures {st['captures']}   frames {st['frames']}"
            if "frame_rms" in st:
                line2 += f"   frame error RMS {st['frame_rms']:.2f} mm"
            if "capture_rms" in st:
                line2 += (f"   capture error RMS {st['capture_rms']:.2f} mm   "
                          f"mean of captures {np.linalg.norm(st['all_mean_error']):.2f} mm")
            hint = message if time.perf_counter() < message_until else {
                "none": "show both boards to the camera",
                "moving": "hold still to capture",
                "collecting": "hold still...",
                "move": "move or turn the screen for the next capture",
            }.get(step.state, "")
            out = _with_panel(out, [line1, line2, f"> {hint}", "q finish and save   Esc quit without saving"], scale=0.8)
            cv2.imshow(window, out)
            key = cv2.waitKey(1) & 0xFF
            if key == 27 or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                print("cancelled, nothing saved")
                return 1
            if key == ord("q"):
                break
    finally:
        live.stop()
        cv2.destroyAllWindows()

    # Raw results: every measured frame, tagged with its capture (0 = not in one).
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["capture", "frame", "x_mm", "y_mm", "z_mm", "err_x", "err_y", "err_z", "err_3d",
                     "camera_distance_mm", "ref_rms_px", "tgt_rms_px", "ref_corners", "tgt_corners"])
        for cap_no, res in session.frames:
            e = res.point - session.truth
            wr.writerow([cap_no, res.name, *np.round(res.point, 4), *np.round(e, 4), round(float(np.linalg.norm(e)), 4),
                         round(res.camera_distance_mm, 1), round(res.ref.rms_px, 3), round(res.tgt.rms_px, 3),
                         res.ref.corners, res.tgt.corners])
    _print_live_summary(session, truth_mm)
    print(f"\nwrote {csv_path}" + (f"\nsaved capture frames to {frames_dir}" if session.captures else ""))
    return 0


def _print_live_summary(session: LiveMeasureSession, truth_mm: float) -> None:
    st = session.stats()
    print(f"\nLive measurement vs exact truth (corner distance {truth_mm:.2f} mm)")
    print(f"  frames measured       {st['frames']}")
    if "frame_rms" in st:
        x, y, z = st["frame_rms_xyz"]
        print(f"  single frame          RMS {st['frame_rms']:.2f} mm   (x {x:.2f}  y {y:.2f}  z {z:.2f})")
    if "capture_rms" in st:
        x, y, z = st["capture_rms_xyz"]
        m = st["all_mean_error"]
        print(f"  frame noise           {st['frame_noise']:.2f} mm   (spread of frames within one capture)")
        print(f"  one capture ({session.frames_per_capture} fr)   RMS {st['capture_rms']:.2f} mm   (x {x:.2f}  y {y:.2f}  z {z:.2f}), "
              f"worst {st['capture_worst']:.2f}")
        print(f"  mean of {st['captures']} captures    error {np.linalg.norm(m):.2f} mm   (x {m[0]:+.2f}  y {m[1]:+.2f}  z {m[2]:+.2f})")
        print("\n  capture   error mm   frame noise mm   camera mm")
        for c in session.captures:
            err = np.linalg.norm(c.mean - session.truth)
            dist = np.mean([r.camera_distance_mm for r in c.results])
            print(f"  {c.number:7d}   {err:8.2f}   {c.frame_spread:14.2f}   {dist:9.0f}")


def cmd_probe_image(args: argparse.Namespace) -> int:
    probe = screen_probe(args.name, args.width, args.height, args.pitch_mm)
    sq_mm = probe.board.square_mm
    oy = probe.screen["origin_px"][1] * args.pitch_mm
    # Rough tip position from the hand measurements (probe frame: x right, y down, z
    # into the screen): centred, above the top edge of the screen, behind its surface.
    probe.tip.guess = [round(probe.board.squares_x * sq_mm / 2, 2), round(-(oy + args.tip_above_mm), 2), args.tip_back_mm]
    probe.tip.kind = args.tip
    probe.tip.radius_mm = args.ball_radius_mm if args.tip == "ball" else 0.0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    png, spec_path = out.with_suffix(".png"), out.with_suffix(".probe.json")
    cv2.imwrite(str(png), screen_image(probe))
    probe.save(spec_path)
    b = probe.board
    print(f"wrote {png} ({args.width} x {args.height}) and {spec_path}")
    print(f"  board {b.squares_x} x {b.squares_y} squares of {probe.screen['square_px']} px "
          f"= {sq_mm:.3f} mm nominal, marker IDs {b.ids[0]}-{b.ids[-1]}")
    print(f"  tip guess (probe frame) {probe.tip.guess} mm")
    print(f"Show it full screen on the phone, then measure {b.squares_y - 2} squares down between inner corners and run:")
    print(f"  python -m spt probe-scale --probe {spec_path} --measured-mm <mm> --squares {b.squares_y - 2}")
    return 0


def cmd_prism_template(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = list(DESIGNS) if args.design == "all" else [args.design]
    for name in names:
        d = DESIGNS[name]
        pages = template_pages(d)
        pdf, spec = out / f"{d.name}_template.pdf", out / f"{d.name}.prism.json"
        write_pdf(pdf, pages)
        d.save(spec)
        print(f"{d.name}: {d.sides} faces {d.face_w:g} x {d.length:g} mm ({d.across_flats:.2f} mm across flats), "
              f"markers {d.marker_mm:g} mm, IDs {d.ids[0]}-{d.ids[-1]}, nominal tip {d.tip_mm:g} mm below centre")
        print(f"  wrote {pdf} ({len(pages)} page{'s' if len(pages) > 1 else ''}) and {spec}")
    print("Print at 100% / Actual size and check the 100 mm bar.")
    return 0


def cmd_probe_scale(args: argparse.Namespace) -> int:
    probe = ProbeSpec.load(args.probe)
    old, old_tip = probe.board.square_mm, probe.tip.offset
    if args.ppi:
        if not probe.screen:
            raise SystemExit("--ppi needs a probe shown on a screen")
        square = probe.screen["square_px"] * 25.4 / args.ppi
    elif args.measured_mm and args.squares:
        square = args.measured_mm / args.squares
    else:
        raise SystemExit("give --ppi, or --measured-mm with --squares")
    probe = probe.with_square_mm(square)
    probe.save(args.probe)
    change = (probe.board.square_mm / old - 1) * 100
    print(f"square {old:.4f} -> {probe.board.square_mm:.4f} mm ({change:+.2f} %), saved {args.probe}")
    if old_tip is not None:
        print(f"  tip offset rescaled with it: {old_tip} -> {probe.tip.offset}")
    if abs(change) > 3:
        print("  WARNING: more than 3 % off the nominal size - check the measurement and that the image is shown full screen")
    return 0


def cmd_pivot(args: argparse.Namespace) -> int:
    probe = ProbeSpec.load(args.probe)
    calib = CameraCalibration.load(args.calib)
    query = args.camera or calib.camera.get("name")
    cam_info = find_camera(query, list_cameras(with_modes=True, include_virtual=True))
    width, height = calib.image_size
    mode = cam_info.best_mode()
    detector = BoardDetector(probe.board)
    K, dist = calib.camera_matrix, calib.dist_coeffs

    live = LiveCamera(cam_info.index, width, height, mode.fps if mode else None, "MJPG").start()
    window = f"Pivot calibration - {cam_info.name}"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    if args.monitor:
        mon = {m.index: m for m in list_monitors()}.get(args.monitor)
        if mon:
            cv2.moveWindow(window, mon.x + 20, mon.y + 20)
            cv2.resizeWindow(window, min(mon.width - 40, args.max_side), min(mon.height - 80, round(args.max_side * height / width)))
    print(f"{cam_info.name}: put the tip in a small dent and rotate the probe around it. q saves, Esc cancels.")
    poses: list[Pose] = []
    prev: Pose | None = None
    result, keep, was_done = None, [], False
    last = 0
    try:
        while True:
            frame = live.read(after=last, timeout=5.0)
            last = frame.number
            det = detector.detect(cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY))
            pose = estimate_pose(detector, det, calib, min_corners=args.min_corners)
            state = "none"
            if pose is not None and pose.rms_px <= args.max_rms:
                turn = 999.0 if prev is None else _angle_deg(prev.R, pose.R)
                state = "fast" if turn > args.max_turn else "ok"
                if state == "ok" and (not poses or min(_angle_deg(p.R, pose.R) for p in poses[-5:]) >= args.min_step):
                    poses.append(pose)
                    state = "sample"
                    if len(poses) >= 6 and len(poses) % 3 == 0:
                        result, keep = solve_pivot_robust(poses)
            prev = pose
            done = (result is not None and len(keep) >= args.samples and result.spread_deg >= args.spread
                    and result.rms_mm <= args.max_residual)
            if done and not was_done:
                _beep(1800, 300)
                was_done = True

            out = frame.image.copy()
            if det is not None:
                for x, y in det.corners:
                    cv2.circle(out, (round(x), round(y)), 4, (0, 255, 0), -1, cv2.LINE_AA)
            if result is not None and pose is not None:
                tip_cam = pose.apply(result.tip)
                piv, _ = cv2.projectPoints(result.pivot.reshape(1, 3), np.zeros(3), np.zeros(3), K, dist)
                tip, _ = cv2.projectPoints(tip_cam.reshape(1, 3), np.zeros(3), np.zeros(3), K, dist)
                cv2.drawMarker(out, tuple(np.round(piv.ravel()).astype(int)), (255, 0, 255), cv2.MARKER_CROSS, 40, 2)
                cv2.circle(out, tuple(np.round(tip.ravel()).astype(int)), 10, (0, 0, 255), 3, cv2.LINE_AA)
            if not args.no_mirror:
                out = cv2.flip(out, 1)
            h, w = out.shape[:2]
            color = {"sample": (0, 220, 0), "ok": (0, 220, 255), "fast": (0, 140, 255), "none": (0, 0, 200)}[state]
            cv2.rectangle(out, (0, 0), (w - 1, h - 1), color, max(6, w // 100))
            scale = args.max_side / max(w, h)
            if scale < 1:
                out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if result is None:
                line1 = f"samples {len(poses)} - need 6 to start"
            else:
                t = result.tip
                line1 = (f"samples {len(keep)}/{args.samples}   spread {result.spread_deg:.0f}/{args.spread:.0f} deg   "
                         f"residual RMS {result.rms_mm:.2f} mm (goal {args.max_residual:g})   tip x {t[0]:.1f} y {t[1]:.1f} z {t[2]:.1f}")
            hint = {
                "none": "show the probe board to the camera" if pose is None else "board fit poor - check glare / focus",
                "fast": "slower",
                "ok": "keep rotating around the tip: tilt left, right, forward, back",
                "sample": "keep rotating around the tip: tilt left, right, forward, back",
            }[state]
            if done:
                hint = "DONE - press q to save (red circle should sit on the tip)"
            out = _with_panel(out, [line1, f"> {hint}", "red circle = estimated tip   magenta cross = pivot   q save   Esc cancel"], scale=0.8)
            cv2.imshow(window, out)
            key = cv2.waitKey(1) & 0xFF
            if key == 27 or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                print("cancelled, nothing saved")
                return 1
            if key == ord("q"):
                break
    finally:
        live.stop()
        cv2.destroyAllWindows()

    if result is None:
        print("not enough samples, nothing saved")
        return 1
    result, keep = solve_pivot_robust(poses)
    # Raw observations: every sampled probe pose, and whether the fit kept it.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    raw = Path(f"output/pivot_{probe.name}_{stamp}.csv")
    raw.parent.mkdir(parents=True, exist_ok=True)
    kept = set(keep)
    with open(raw, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["sample", "rx", "ry", "rz", "tx", "ty", "tz", "rms_px", "corners", "kept"])
        for i, p in enumerate(poses):
            wr.writerow([i, *_pose_row(p), int(i in kept)])
    probe.tip.offset = [round(float(v), 4) for v in result.tip]
    probe.tip.rms_mm = round(result.rms_mm, 4)
    probe.tip.samples = len(keep)
    probe.save(args.probe)
    print(f"\nPivot calibration: {len(keep)} of {len(poses)} samples used, rotation spread {result.spread_deg:.0f} deg")
    print(f"  tip (probe frame)  x {result.tip[0]:.2f}  y {result.tip[1]:.2f}  z {result.tip[2]:.2f} mm")
    print(f"  residual           RMS {result.rms_mm:.2f} mm, worst {result.residuals.max():.2f} mm")
    if probe.tip.guess:
        d = result.tip - np.array(probe.tip.guess)
        print(f"  vs hand guess      dx {d[0]:+.1f}  dy {d[1]:+.1f}  dz {d[2]:+.1f} mm")
    print(f"saved {args.probe}\nraw samples: {raw}")
    return 0


def _pose_row(p: Pose) -> list[float]:
    rvec, _ = cv2.Rodrigues(p.R)
    return [*np.round(rvec.ravel(), 6), *np.round(p.t, 4), round(p.rms_px, 3), p.corners]


def _open_window(title: str, monitor: int | None, width: int, height: int, max_side: int) -> None:
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    if monitor:
        mon = {m.index: m for m in list_monitors()}.get(monitor)
        if mon:
            cv2.moveWindow(title, mon.x + 20, mon.y + 20)
            cv2.resizeWindow(title, min(mon.width - 40, max_side), min(mon.height - 80, round(max_side * height / width)))


def cmd_digitize(args: argparse.Namespace) -> int:
    probe = ProbeSpec.load(args.probe)
    calib = CameraCalibration.load(args.calib)
    targets: dict[str, np.ndarray] = {}
    ref_square = args.ref_square_mm
    if args.scene:
        scene = Scene.load(args.scene)
        ref_b = scene.board("reference")
        if ref_b is None:
            raise SystemExit("the scene has no reference board")
        ref_square = ref_square or ref_b.spec.square_mm
        targets = scene.target_points()
    session = DigitizeSession(calib, REFERENCE.scaled(ref_square), probe, targets,
                              frames_per_point=args.frames, max_rms_px=args.max_rms)
    query = args.camera or calib.camera.get("name")
    cam_info = find_camera(query, list_cameras(with_modes=True, include_virtual=True))
    width, height = calib.image_size
    mode = cam_info.best_mode()
    K, dist = calib.camera_matrix, calib.dist_coeffs
    stamp = time.strftime("%Y%m%d_%H%M%S")
    points_csv = Path(args.out or f"output/points_{stamp}.csv")
    frames_csv = points_csv.with_name(points_csv.stem + "_frames.csv")

    live = LiveCamera(cam_info.index, width, height, mode.fps if mode else None, "MJPG").start()
    window = f"Digitize - {cam_info.name}"
    _open_window(window, args.monitor, width, height, args.max_side)
    print(f"{cam_info.name}: touch a point and hold still to capture it. Points save as you go; q or Esc finishes.")
    message, message_until, last = "", 0.0, 0
    colors = {"captured": (0, 220, 0), "holding": (0, 220, 255), "none": (0, 0, 200), "move": (255, 160, 0)}
    try:
        while True:
            frame = live.read(after=last, timeout=5.0)
            last = frame.number
            step = session.update(f"f{frame.number}", cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY))
            if step.state == "captured":
                p = step.point
                text = f"captured {p.name}: x {p.xyz[0]:.1f}  y {p.xyz[1]:.1f}  z {p.xyz[2]:.1f} mm (spread {p.spread:.2f})"
                if p.error is not None:
                    text += f"   error xy {np.linalg.norm(p.error[:2]):.2f}  z {p.error[2]:+.2f} mm"
                message, message_until = text, time.perf_counter() + 3.0
                print("  " + text)
                _beep(1400, 70)
                _save_digitized(session, points_csv, frames_csv)  # saved as you go

            out = frame.image.copy()
            for det, color in zip(session.last_detections, ((0, 255, 0), (255, 200, 0))):
                if det is not None:
                    for x, y in det.corners:
                        cv2.circle(out, (round(x), round(y)), 4, color, -1, cv2.LINE_AA)
            if step.sample is not None:
                ref = step.sample.ref
                if targets:  # targets projected through the reference pose: should sit on the crosshairs
                    pts = np.array([ref.apply(t) for t in targets.values()])
                    img_pts, _ = cv2.projectPoints(pts, np.zeros(3), np.zeros(3), K, dist)
                    for q in img_pts.reshape(-1, 2):
                        cv2.drawMarker(out, tuple(np.round(q).astype(int)), (255, 0, 255), cv2.MARKER_CROSS, 24, 2)
                tip_img, _ = cv2.projectPoints(ref.apply(step.sample.tip).reshape(1, 3), np.zeros(3), np.zeros(3), K, dist)
                cv2.circle(out, tuple(np.round(tip_img.ravel()).astype(int)), 10, (0, 0, 255), 3, cv2.LINE_AA)
            if not args.no_mirror:
                out = cv2.flip(out, 1)
            h, w = out.shape[:2]
            color = colors.get(step.state)
            if color:
                thick = max(6, w // 100)
                cv2.rectangle(out, (0, 0), (w - 1, h - 1), color, thick)
                if step.state == "holding":
                    cv2.rectangle(out, (0, h - thick * 2), (round(w * step.progress), h), color, -1)
            scale = args.max_side / max(w, h)
            if scale < 1:
                out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if step.sample is not None:
                t = step.sample.tip
                line1 = f"tip  x {t[0]:7.1f}  y {t[1]:7.1f}  z {t[2]:6.1f} mm"
                if step.nearest:
                    line1 += f"     nearest {step.nearest[0]}: {step.nearest[1]:.1f} mm"
            else:
                line1 = f"tip: {step.reason}"
            s = session.summary()
            line2 = f"points {s['points']}"
            if "xy_rms" in s:
                line2 += f"   on targets {s['matched']}: error xy RMS {s['xy_rms']:.2f} mm, z mean {s['z_mean']:+.2f} sd {s['z_std']:.2f}"
            hint = message if time.perf_counter() < message_until else {
                "none": "show the reference board and the probe to the camera",
                "moving": "touch a point and hold still",
                "holding": step.reason or "hold still...",
                "move": "move to the next point",
            }.get(step.state, "")
            out = _with_panel(out, [line1, line2, f"> {hint}",
                                    "red circle = tip   magenta = targets   u undo last point   q / Esc finish (saved as you go)"], scale=0.8)
            cv2.imshow(window, out)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key == ord("u"):
                dropped = session.undo()
                if dropped is not None:
                    message, message_until = f"removed {dropped.name} (point {dropped.number})", time.perf_counter() + 2.5
                    print(f"  undo: removed {dropped.name}")
                    _beep(500, 150)
                    _save_digitized(session, points_csv, frames_csv)
    finally:
        live.stop()
        cv2.destroyAllWindows()
        _save_digitized(session, points_csv, frames_csv)  # also on errors: never lose points

    s = session.summary()
    print(f"\nDigitized {s['points']} points (tip from pivot: residual {probe.tip.rms_mm} mm)")
    if "xy_rms" in s:
        print(f"  on targets        {s['matched']}")
        print(f"  error in plane    RMS {s['xy_rms']:.2f} mm, worst {s['worst_xy']:.2f} mm")
        print(f"  error in depth    mean {s['z_mean']:+.2f} mm (glass above the pixels + bias), sd {s['z_std']:.2f} mm")
        print(f"  3D RMS            {s['rms_3d']:.2f} mm ({s['rms_3d_offset_removed']:.2f} mm with the mean depth offset removed)")
        print("\n  point   err x   err y   err z   spread")
        for p in session.points:
            if p.error is not None:
                print(f"  {p.name:5}  {p.error[0]:+6.2f}  {p.error[1]:+6.2f}  {p.error[2]:+6.2f}   {p.spread:5.2f}")
    print(f"\nwrote {points_csv} and {frames_csv}")
    return 0


def _save_digitized(session: DigitizeSession, points_csv: Path, frames_csv: Path) -> None:
    points_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(points_csv, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["name", "x_mm", "y_mm", "z_mm", "spread_mm", "frames", "target", "err_x", "err_y", "err_z"])
        for p in session.points:
            e = p.error if p.error is not None else [None] * 3
            wr.writerow([p.name, *np.round(p.xyz, 4), round(p.spread, 4), len(p.samples), p.target or "",
                         *[None if v is None else round(float(v), 4) for v in e]])
    with open(frames_csv, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["point", "frame", "tip_x", "tip_y", "tip_z",
                     "ref_rx", "ref_ry", "ref_rz", "ref_tx", "ref_ty", "ref_tz", "ref_rms_px", "ref_corners",
                     "probe_rx", "probe_ry", "probe_rz", "probe_tx", "probe_ty", "probe_tz", "probe_rms_px", "probe_corners"])
        for n, smp in session.samples:
            wr.writerow([n, smp.frame, *np.round(smp.tip, 4), *_pose_row(smp.ref), *_pose_row(smp.probe)])


def _angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(Ra.T @ Rb) - 1) / 2, -1.0, 1.0))))


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

    _print_calibration(calib, spec, size, skipped, elapsed)
    print(f"\nwrote {args.out}")
    return 0


def _print_calibration(calib: CameraCalibration, spec, size, skipped: list, elapsed: float) -> None:
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

    cl = sub.add_parser("calibrate-live", help="guided calibration: auto-captures while you move the board")
    cl.add_argument("--camera", required=True, help="index or part of the name (see 'cameras')")
    cl.add_argument("--mode", help="WIDTHxHEIGHT (default: largest MJPG mode at 15+ fps)")
    cl.add_argument("--fps", type=float)
    cl.add_argument("--fourcc", default="MJPG")
    cl.add_argument("--scene", help="scene file from 'show --layout calib' (board square size)")
    cl.add_argument("--out", help="calibration file (default output/<camera>.calib.json)")
    cl.add_argument("--frames-dir", help="where accepted frames are saved (default captures/calib_live/...)")
    cl.add_argument("--min-views", type=int, default=15)
    cl.add_argument("--coverage", type=float, default=0.8, help="frame coverage goal (0-1)")
    cl.add_argument("--max-rms", type=float, default=0.6, help="error goal (px)")
    cl.add_argument("--monitor", type=int, help="monitor to show the camera view on")
    cl.add_argument("--max-side", type=int, default=1800, help="display size limit (px)")
    cl.add_argument("--no-mirror", action="store_true", help="don't mirror the view (camera not facing you)")
    cl.set_defaults(func=cmd_calibrate_live)

    ml = sub.add_parser("measure-live", help="live measurement test against the exact screen truth")
    ml.add_argument("--calib", required=True, help="calibration file (its camera is used unless --camera)")
    ml.add_argument("--scene", default="output/scene_measure.json", help="scene from 'show --layout measure'")
    ml.add_argument("--camera", help="index or part of the name (default: from the calibration)")
    ml.add_argument("--fps", type=float)
    ml.add_argument("--fourcc", default="MJPG")
    ml.add_argument("--frames", type=int, default=15, help="frames averaged per capture")
    ml.add_argument("--max-rms", type=float, default=1.0, help="reject frames whose board fit is worse (px)")
    ml.add_argument("--csv", help="per-frame results (default output/measure_live_<camera>_<time>.csv)")
    ml.add_argument("--monitor", type=int, help="monitor to show the camera view on")
    ml.add_argument("--max-side", type=int, default=1800)
    ml.add_argument("--no-mirror", action="store_true")
    ml.set_defaults(func=cmd_measure_live)

    pi = sub.add_parser("probe-image", help="probe board image for a phone screen + probe file")
    pi.add_argument("--name", default="s23")
    pi.add_argument("--width", type=int, default=1080, help="phone screen width (portrait) px")
    pi.add_argument("--height", type=int, default=2340, help="phone screen height (portrait) px")
    pi.add_argument("--pitch-mm", type=float, default=25.4 / 425, help="pixel pitch (S23: 425 ppi)")
    pi.add_argument("--tip-above-mm", type=float, default=50.0, help="tip distance above the top edge of the screen")
    pi.add_argument("--tip-back-mm", type=float, default=15.0, help="tip distance behind the screen surface")
    pi.add_argument("--tip", choices=["sharp", "ball"], default="sharp")
    pi.add_argument("--ball-radius-mm", type=float, default=0.0)
    pi.add_argument("--out", default="output/probe_s23", help="output path without extension")
    pi.set_defaults(func=cmd_probe_image)

    pt = sub.add_parser("prism-template", help="folding template (PDF) + nominal geometry for a prism probe")
    pt.add_argument("--design", choices=["all", *DESIGNS], default="all")
    pt.add_argument("--out", default="output/prisms")
    pt.set_defaults(func=cmd_prism_template)

    ps = sub.add_parser("probe-scale", help="set the probe board's measured square size")
    ps.add_argument("--probe", required=True)
    ps.add_argument("--measured-mm", type=float, help="caliper distance between inner corners")
    ps.add_argument("--squares", type=int, help="number of squares that distance spans")
    ps.add_argument("--ppi", type=float, help="instead: the screen's pixels per inch (S23: 425)")
    ps.set_defaults(func=cmd_probe_scale)

    pv2 = sub.add_parser("pivot", help="pivot calibration: find the probe tip by rotating around it")
    pv2.add_argument("--probe", required=True, help="probe file (from probe-image)")
    pv2.add_argument("--calib", required=True, help="camera calibration (its camera is used unless --camera)")
    pv2.add_argument("--camera")
    pv2.add_argument("--samples", type=int, default=60, help="samples needed")
    pv2.add_argument("--spread", type=float, default=25.0, help="rotation spread needed (deg)")
    pv2.add_argument("--max-residual", type=float, default=1.0, help="residual goal (mm)")
    pv2.add_argument("--min-step", type=float, default=1.0, help="min rotation between samples (deg)")
    pv2.add_argument("--max-turn", type=float, default=3.0, help="max rotation per frame (deg), against blur")
    pv2.add_argument("--min-corners", type=int, default=8)
    pv2.add_argument("--max-rms", type=float, default=1.0, help="reject poses whose board fit is worse (px)")
    pv2.add_argument("--monitor", type=int)
    pv2.add_argument("--max-side", type=int, default=1800)
    pv2.add_argument("--no-mirror", action="store_true")
    pv2.set_defaults(func=cmd_pivot)

    dg = sub.add_parser("digitize", help="capture probe-tip points in the reference frame")
    dg.add_argument("--probe", required=True, help="calibrated probe file")
    dg.add_argument("--calib", required=True, help="camera calibration (its camera is used unless --camera)")
    dg.add_argument("--camera")
    dg.add_argument("--scene", help="scene from 'show' (reference square size; touch targets to check against)")
    dg.add_argument("--ref-square-mm", type=float, help="reference square size (printed board)")
    dg.add_argument("--frames", type=int, default=15, help="frames averaged per point")
    dg.add_argument("--max-rms", type=float, default=1.0)
    dg.add_argument("--out", help="points CSV (default output/points_<time>.csv)")
    dg.add_argument("--monitor", type=int)
    dg.add_argument("--max-side", type=int, default=1800)
    dg.add_argument("--no-mirror", action="store_true")
    dg.set_defaults(func=cmd_digitize)

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
