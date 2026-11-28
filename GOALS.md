# Goals

Spatial Point Tracker captures 3D points on real objects with cameras, fiducial
markers and a touch probe: first suspension hardpoints, later vehicle body lines and
the scaling and alignment of 3D scans. **Target: 1-2 mm per point.** The project is
standalone; other programs (including Suspension-Calculator) take its output.

Design decisions: [docs/PLAN.md](docs/PLAN.md). Phase 0: [docs/PHASE0.md](docs/PHASE0.md).
Cameras: [docs/CAMERAS.md](docs/CAMERAS.md). Probes: [docs/PROBE.md](docs/PROBE.md). Phone: [docs/PHONE.md](docs/PHONE.md).

## Current status and handoff (2026-11-27)

- Branch **`phase-2`** (phase 0 and phase 1 are merged into `main`). 77 tests,
  all passing (`.venv\Scripts\python -m pytest`).
- Working end to end: guided live calibration, live measurement, phone-screen probe,
  pivot calibration, and digitizing points against touch targets (1.55 mm raw,
  ~0.5 mm with a constant offset removed).
- Just finished: printed prism probe designs (`hex30`, `hex60`), folding templates,
  and the probe size simulation, including the S23 as a camera.
- The S23's Camera2 report shows a logical back camera (ID 0, level 3, manual focus)
  with physical lenses **2 (ultrawide, fixed focus), 5 and 6 (main / telephoto,
  which is which still unknown)**. Two lenses streaming at once is guaranteed at
  about 1080p; three lenses, or 4K, needs testing on the phone.
- The PC has **no Android tooling** yet (no Android Studio, SDK, adb or Java).

### Decisions for next session

1. **Next step:** ~~marker maps or the S23 app first?~~ Decided 2026-11-27: the phone app
   (branch `phone-app`).
2. **Android tooling:** decided: command-line only (installed, see docs/PHONE.md).
3. **Phone safety:** agreed approach is a normal app only (no root, no bootloader
   unlock, no system changes), camera permission only, temperature watchdog, USB
   debugging approved for this PC only and turned off afterwards. Back up the phone
   first. The user decides whether to go ahead.

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
- [ ] **Marker maps:** measure the real layout of rigid marker sets from images
      (bundle adjustment), starting from nominal geometry
  - [ ] Prism probe calibration (face-to-face layout) + pivot for the printed tip
  - [ ] Multi-board reference maps for a car corner; grow by adding boards seen
        together with mapped ones; re-solve without losing stored points
- [ ] Poses from any visible markers of a marker map (multi-face probe, several boards)
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
- [ ] Live measurement test with the phone (compare with the NexiGo's 0.27 mm)
- [ ] Calibrate ultrawide and tele; digitize with the phone
- [x] Native Android app: detects markers on the phone and sends corners (raw
      observations) to the PC; the PC stays the hub
- [ ] Per-lens calibration through the same video pipeline; stabilisation off,
      focus locked, explicit physical lens (no auto lens switching)
- [ ] Telephoto for long range (hex30 for body lines at 3-4 m?), ultrawide to keep
      reference markers in view

## Later

- [ ] Multiple cameras / devices fused on the PC (separate viewpoints fix depth error)
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
