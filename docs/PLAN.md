# Plan

Spatial Point Tracker captures 3D points (and later lines) on real objects with a
camera, fiducial markers and a touch probe, for use in other software.
The first use is suspension hardpoints, but the project is standalone: other programs
(including Suspension-Calculator) import its output, not the other way round.

## Goals

- **Accuracy:** 1-2 mm per point (user-settable later). Every point carries an
  estimate of its own uncertainty, and a capture that cannot meet the target is
  blocked or clearly flagged, never silently accepted.
- **Scope:** one corner of a car first, then the whole car.
- **Devices:** Android (Samsung S23) first; iOS is nice to have. Any camera
  connected to the host computer also works.

## Non-goals

This is a **digitizer, not a scanner**: like a touch-probe CMM, it records sparse
points the user chooses, each with a known accuracy.

- Every point comes from the probe tip touching something. Cameras only track
  markers; they never measure surfaces.
- Body lines are traced with the probe as chains of points.
- Surfaces are built in CAD (Blender / Fusion lofts) from exported lines, not here.
- No photogrammetry, depth meshes or dense point clouds. Dense surfaces come from
  existing scanning tools and are **imported and aligned** (see "Scan alignment").

## Decisions so far

| Topic | Decision |
|---|---|
| Measurement source | Fiducial markers (ChArUco / ArUco) are the **only** source of measurements. Phone AR (ARCore) and the IMU may only bridge short marker gaps, with a growing uncertainty estimate. Phone AR alone is accurate to about 1-3 cm, so it is never used to measure points. |
| Cameras | Start with one camera, but design for several from day one. Each camera measures the probe relative to the reference markers independently, so cameras need no calibration to each other, only each its own. Captures happen while the probe is still, so no hardware sync is needed. A **second viewpoint is the main fix for single-view depth error** (phase 0), so it comes early. |
| Calibration profiles | A calibration is valid for one camera + lens + resolution/crop + focus setting only. The app keeps a profile per camera setting. Live video must be calibrated through the same pipeline that measures (phone video is not the same "camera" as Pro-mode photos). |
| Guided calibration | Calibrate from live video. Frames are kept automatically when the camera is still, sharp, and adds new frame coverage or a new tilt. A live coverage map and hints guide the user; calibration stops when coverage and error pass. |
| Phone-screen probe (option) | The phone shows the probe markers on its screen (flat, exact scale, as in phase 0), with the tip on the phone's end. Its IMU measures probe stillness, and its screen is the UI (capture, countdown, accept/reject). Trade-offs: small marker area plus the tip's lever arm; the screen must face a camera. |
| Modes | **B (first):** fixed camera (webcam, phone on tripod, any camera) watches fixed reference markers and a handheld probe that carries markers. **A (later):** the phone itself is the probe, with a tip attachment. Measuring the same points in both modes checks one against the other. |
| Probe tips | Interchangeable ball and sharp tips, each calibrated by **pivot calibration**: rotate the probe around a fixed tip and solve for the tip offset. |
| Derived points | Many hardpoints can't be touched (ball-joint centres, bushing axes). Support midpoints (both ends of a bolt), circle fits (3+ touches on a hole rim) and sphere fits. |
| Capture | Press, then an optional countdown, then the capture waits until the camera or phone is still (IMU) and averages over a short still window. Reject if it never settles. |
| Drift checks | Occasionally touch a known check point. If it is off by more than the target, warn. |
| Architecture | The **host is the brain**: it stores project, camera calibrations, marker map, points and exports. Cameras are clients speaking one protocol, so a native app later only swaps a client. |
| Stack | **Python** (OpenCV, NumPy) for the vision maths on the host: native speed, lowest lag. A browser UI talks to it over a local WebSocket (only small data, no video). Webcams on the host are read by Python directly. |
| Data | **Store raw observations** (detected corners per photo/frame), not only final XYZ, so every point can be recomputed after a better calibration or a re-solved marker map. |
| Marker map | Grows over time: a new board must be photographed together with an already-mapped one; then the map is re-solved (bundle adjustment). |
| Coordinates | Explicit project frame (e.g. car frame defined from measured points), units mm, axis convention converted on export. |
| Export | CSV, OBJ (vertices + line elements; Blender), Fusion 360 (CSV/script, later an add-in). Lines and surfaces are built on the host from points later. |

## Phases

| Phase | What | Platform |
|---|---|---|
| **0** | Feasibility: print boards, calibrate the S23 from photos, measure a target board's position relative to a reference board, compare to calipers / steel rule. See [PHASE0.md](PHASE0.md). | Python scripts |
| 1 | Mode B with a camera on the host: live capture, probe with pivot calibration, point list, stillness-gated capture | Python service + browser UI |
| 2 | Mode B with the S23 on a tripod as the camera | Phone browser client |
| 3 | Mode A, phone as probe, markers only (works on iOS too) | Phone browser client |
| 4 | Mode A plus ARCore bridging of marker gaps, only if gaps are a real problem | Native Android client |

Later: multi-board marker maps for the whole car, body lines, named point lists
supplied by other programs, exports, scan alignment.

## Scan alignment

The user can 3D-scan parts, but scan scale and accuracy are unreliable. Fix this by
aligning an imported scan (mesh) to probe-measured points:

- **Matching points:** probe 4-6 features (holes, corners, or sticker dots placed
  before scanning), pick the same features on the scan, and solve the similarity
  transform (rotation, translation, **uniform scale**) in closed form.
- **Surface touches:** probe ~20 spots anywhere on the part's surface and fit the
  scan to them (scaled ICP) from a rough initial alignment. No picking on the mesh.
- Report the leftover error at each point. If the scan is warped, not only scaled, the
  leftover errors show it; correcting warp is a later, bigger step.

## Open questions

- Phone video to the host: send frames, or detect on the phone and send corners?
  Decide in phase 2 (bandwidth vs. phone CPU vs. accuracy).
- Camera focus locking from the browser on Android (needed for a stable calibration).
- Wide / fisheye lenses (DJI Osmo Action) need a fisheye or rational distortion model.
