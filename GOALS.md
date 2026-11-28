# Goals

Spatial Point Tracker captures 3D points on real objects with cameras, fiducial
markers and a touch probe: first suspension hardpoints, later vehicle body lines and
the scaling and alignment of 3D scans. **Target: 1-2 mm per point.** The project is
standalone; other programs (including Suspension-Calculator) take its output.

Design decisions: [docs/PLAN.md](docs/PLAN.md). Phase 0: [docs/PHASE0.md](docs/PHASE0.md).
Cameras: [docs/CAMERAS.md](docs/CAMERAS.md). Probes: [docs/PROBE.md](docs/PROBE.md). Phone: [docs/PHONE.md](docs/PHONE.md).

## Current status and handoff (2026-11-28)

- Everything is on **`main`** (phase 2 and the phone work merged; old branches
  deleted). 106 tests, all passing (`.venv\Scripts\python -m pytest`).
- **Waiting on the user:** the hex30 / hex60 bodies are printing; the print file
  `output\print\print_all_letter.pdf` (11 letter pages: reference + target boards,
  hex30 + hex60 templates, loose markers 165-25 mm, IDs 100-177) is to be printed at
  100 % and checked against the 100 mm bars.
- **Phone (S23, SPT Camera 0.10 installed; 0.11 built, not installed):** all three back
  lenses calibrated through the app (`output\calibrations\phone_2/5/6`), live
  detection, USB link (`python -m spt phone`), phone works as `--camera phone:main`
  in every live tool. App 0.11 reads DICT_5X5_250 (loose markers 100+): install it
  next time the phone is used. USB debugging is still on: when phone work pauses for
  a while, uninstall the app, turn USB debugging off, revoke authorisations, turn
  Auto Blocker back on (docs/PHONE.md).
- **Fusion:** phone + NexiGo ~40° apart beat both cameras when neither is biased
  (0.18 mm, depth 0.08); the Kamvas cover glass biases a camera viewing at an angle.
  Confirm with printed boards.
- **Marker maps** (`spt/markermap.py`, `map-probe`) are built and tested in
  simulation (glue errors 0.6-0.8 mm -> 0.01 mm, tip 2 mm -> 0.08 mm per frame).
- The NexiGo now offers only YUY2/NV12 1080p (16.5 fps); MJPG vanished on both USB
  ports. Calibration still valid (same sensor and size).

### Next session (when the prints are done)

1. Assemble the hex30: template printed at 100 %, glued flat, rigid tip on the axis.
2. `python -m spt map-probe --design hex30 --calib <camera calibration>` (~40 views).
3. Wire the probe marker map into `pivot` and `digitize` (they still expect the
   phone-screen ChArUco board), then pivot the printed tip.
4. Re-run the touch test with the printed probe; then printed boards for fusion.

## Phase 0: feasibility (done)

- [x] Printable ChArUco reference and target boards (vector PDF, scale checks)
- [x] Camera calibration from photos (coverage map, per-view errors, outlier drop)
- [x] Measurement test: target corner in the reference frame, repeatability and accuracy
- [x] Boards on a flat screen instead of paper, with exact truth (`show`, `--scene`)
- [x] S23 + Kamvas: mean of 29 photos within 0.16 mm of truth, single photo 1.16 mm
- [x] Lessons: frame coverage is critical; depth (z) is the weak direction; keep
      tilts moderate within the depth of field

## Phase 1: live capture (done)

- [x] `cameras` (names, modes, real fps) and `preview` (live detection, lag, sharpness)
- [x] Fix: DirectShow needs the format set after size/fps (UC40M 1 -> 20 fps at 4K)
- [x] Guided live calibration (`calibrate-live`): auto-capture when still, sharp and new
- [x] NexiGo calibrated: RMS 0.18 px, coverage 85 %
- [x] Live measurement test (`measure-live`): 0.27 mm per capture, 0.16 mm over 3 views
- [x] Phone-screen probe board, scale (`probe-image`, `probe-scale --ppi`)
- [x] Pivot calibration (`pivot`) with raw samples saved; tip rescales with the board
- [x] Digitizer (`digitize`): tip in the reference frame, hold-still capture, undo,
      save as you go, touch targets (`show --layout touch`)
- [x] Touch test: 1.55 mm raw; constant +1.45 mm x offset (likely duct-tape flex on
      the pencil), 0.52 mm with it removed

## Phase 2: probes, marker maps, more cameras (in progress)

- [x] Probe size simulation (`scripts/probe_size_sim.py`), including S23 4K / 1080p
- [x] Prism designs: `hex30` (6 x 30 x 60 mm, IDs 60-71, small work) and `hex60`
      (6 x 60 x 120 mm, IDs 72-83, body lines, needs 4K)
- [x] Folding templates (`prism-template`) + nominal geometry (`.prism.json`)
- [ ] **User:** 3D-print the hex bodies (51.96 / 103.92 mm across flats), print and
      glue the templates, fit a rigid tip on the axis
- [ ] **User:** printed (non-emissive) reference boards (print shop / library laser printer)
- [x] **Marker maps:** measure the real layout of rigid marker sets from images
      (bundle adjustment, `spt/markermap.py`), starting from nominal geometry;
      simulation: glue errors 0.6-0.8 mm -> 0.01 mm, tip 2 mm -> 0.08 mm per frame
  - [x] `map-probe`: collect views of a prism probe (still, two+ faces, new angle,
        face / neighbour coverage), refine, save the map
  - [ ] Run `map-probe` on the printed hex30, then pivot for the printed tip
  - [x] Loose marker sheets (`marker-sheets`): 165-25 mm, DICT_5X5_250 IDs 100-177
  - [ ] Multi-board reference maps for a car corner; grow by adding boards seen
        together with mapped ones; re-solve without losing stored points
- [x] Poses from any visible markers of a marker map (`markermap.locate`)
- [ ] pivot / digitize using a probe marker map instead of the phone-screen board
- [ ] Re-test the touch offset with the printed probe and printed boards (parked until then)
- [ ] Automatic per-camera "max range" and expected error from calibration + probe design

## Phone as a smart camera

- [x] Android tooling: command-line JDK 17 + SDK + adb, no Android Studio ([docs/PHONE.md](docs/PHONE.md))
- [x] Test app `android/` (SPT Camera): lens report + multi-lens stream test, temperature watchdog, CAMERA permission only
- [x] Run the test app on the S23 and record the results
- [x] S23 test app: lenses identified (2 ultrawide, 5 main, 6 telephoto 3x); all
      1/2/3-lens combinations stream at 30 fps at 1080p and 4K, battery +1.8 °C max
      ([docs/PHONE.md](docs/PHONE.md#s23-results-2026-11-27-spt-camera-02))
- [x] Findings: ultrawide stream is corrected and cropped to ~84° (no off switch); the
      tele's largest stream is 2560x1440 (1920x1440 when asked for 4K); lens
      timestamps are identical, so true exposure sync is unverified
- [x] Mixed-size session: main 4K + tele 2560x1440 kills the camera device; use all-4K
      (tele delivers 1920x1440)
- [x] Focus: per-lens manual focus and lock work (0 D is ignored: use 0.01 D)
- [x] Live view with on-phone detection (OpenCV 5.1.0, same as the PC), all lenses
- [x] PC link over USB (`adb forward` to a local socket, no network permission):
      `python -m spt phone`, `spt/phone.py`
- [x] Detection load: ArUco3 + 10 fps cap (15-45 ms per 4K frame); still warms slowly
- [x] Heat: PC picks the lenses (one lens levels off), 2 fps search rate for lenses
      that see nothing
- [x] Phone as a camera source for calibrate-live / measure-live / pivot / digitize
      (`--camera phone:main`); focus locked on the board and stored in the calibration
- [x] Main lens calibrated through the phone: 0.244 px RMS at 4K, 100 % coverage
- [x] Live measurement test with the phone, hand-held: 0.39 mm per frame, 0.31 mm per
      capture (NexiGo fixed: 0.48 / 0.27); x/y 0.03-0.04 mm, z dominates
- [x] Zoom 0.6 for the full ultrawide view (zoom 1.0 crops it to 72°); calibrations
      record zoom and session lenses
- [x] Ultrawide (0.245 px, rational model) and tele (0.273 px) calibrated
- [x] Several lenses at once (`measure-multi`): fused 0.32 vs main 0.55 / tele 0.41 in
      one run, but 0.63 vs main 0.30 in the next: the tele's partial-board bias and
      shared viewpoint limit co-located fusion. Use the right lens per distance.
- [ ] Digitize with the phone (needs the printed probe; the S23 can't be camera and probe)
- [x] Native Android app: detects markers on the phone and sends corners (raw
      observations) to the PC; the PC stays the hub
- [ ] Per-lens calibration through the same video pipeline; stabilisation off,
      focus locked, explicit physical lens (no auto lens switching)
- [ ] Telephoto for long range (hex30 for body lines at 3-4 m?), ultrawide to keep
      reference markers in view

## Later

- [x] Two devices fused (`measure-multi` with phone + webcam calibrations): per-camera 3D
      uncertainty from the board fits, inverse-covariance fusion (simulation: 3x better
      than the best camera at 40° apart)
- [x] Real two-device result (phone + NexiGo ~40° apart, Kamvas): fused 0.18 mm vs
      phone 0.27 / NexiGo 0.37 on joint captures; depth 0.18-0.25 -> 0.08 mm
- [ ] Confirm with printed boards (no cover glass) and more joint captures
- [ ] Fusion weights that include systematic error per camera (now a fixed 0.25 mm floor)
- [ ] Project file: named points, point lists from other programs, car coordinate frame
- [ ] Derived points: midpoint, circle fit (hole rim), sphere fit (ball joints)
- [ ] Exports: CSV (done for points), OBJ (Blender), Fusion 360; explicit axes and units
- [ ] Body lines: traced chains of points; surfaces are built in CAD, not here
- [ ] Scan alignment: scale / align imported 3D scans to probed points (similarity
      transform, or scaled ICP from surface touches), with leftover-error report
- [ ] Browser UI talking to the Python service
- [ ] Ball tip support (radius offset along the surface normal)

## Ideas (not decided)

- Phone tracked by a camera while the phone tracks the wand (a chain): likely worse
  than every camera seeing the reference markers directly, because errors multiply
  along a chain.
- A handheld (moving) camera works if every frame shows both the reference markers
  and the probe.
- Bigger reference boards for long range; 80 mm hex faces would reach ~3.5 m with a 4K camera.
- A standalone phone-only mode (phone + wand + reference markers, no PC).
- iOS app later.

## Non-goals

A digitizer, not a scanner: every point comes from the probe tip touching something.
No photogrammetry, depth meshes or dense point clouds. Dense scans are imported and
aligned, not made here.
