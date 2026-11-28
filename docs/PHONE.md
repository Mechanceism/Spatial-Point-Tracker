# Phone app (SPT Camera)

The Android app in [`android/`](../android) turns the S23 into a smart camera. Step 1
is a **test tool**: it identifies the lenses and measures which lens combinations can
stream at once (1080p / 4K) and at what frame rate, under a temperature watchdog.

## Safety rules (the S23 is the only phone)

- A normal app only: no root, no bootloader unlock, no firmware, no system settings.
- The app's only permission is **CAMERA** (checked in the built APK with `aapt2`):
  no internet, storage, location or contacts; no services, receivers or start-at-boot.
- The camera is released whenever the app leaves the screen.
- Temperature watchdog: camera work stops at thermal status *moderate*, battery
  **41 °C** or thermal headroom 0.85, and only resumes below 38 °C / *light* / 0.70.
- Only the adb commands listed below, each explained before it runs.
- USB debugging is allowed for this PC only and switched off afterwards.

## PC tools (installed once)

Command-line only, no Android Studio, nothing installed system-wide:

| Tool | Where |
|---|---|
| Microsoft OpenJDK 17 (zip) | `%LOCALAPPDATA%\Android\jdk-17` |
| Android SDK: platform 35, build-tools 35/34, platform-tools (adb) | `%LOCALAPPDATA%\Android\Sdk` |
| Gradle 8.11.1 (only used to create the wrapper) | `%LOCALAPPDATA%\Android\gradle` |

Downloads were checked against the vendors' published SHA-256 / SHA-1 sums. The
project uses AGP 8.7.3 + Kotlin 2.0.21 and **no libraries** (platform APIs only).
Android Studio can open `android/` later without changes.

Build (PowerShell, from the repo root):

```powershell
$env:JAVA_HOME = "$env:LOCALAPPDATA\Android\jdk-17"
android\gradlew.bat -p android assembleDebug
# -> android\app\build\outputs\apk\debug\app-debug.apk
```

`android/local.properties` (not in git) holds `sdk.dir=C:/Users/<you>/AppData/Local/Android/Sdk`.

## Phone setup (S23, One UI)

1. **Developer options:** Settings > About phone > Software information > tap
   *Build number* 7 times, enter the PIN.
2. **USB debugging:** Settings > Developer options > *USB debugging* on.
3. **Auto Blocker** (Settings > Security and privacy > Auto Blocker) blocks USB
   commands. Turn it off while testing, and back on afterwards.
4. Plug in the USB cable. The phone asks *Allow USB debugging?* with the PC's key
   fingerprint: tap **Allow**. Leaving *Always allow* unticked means it asks every time.

Afterwards: Developer options > *USB debugging* off, *Revoke USB debugging
authorisations*, then turn Auto Blocker back on (optionally switch Developer options off
at the top of its page).

## adb commands used (and nothing else)

`adb` = `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`

| Command | What it does |
|---|---|
| `adb devices -l` | lists connected phones (shows whether this PC is authorised) |
| `adb install -r android\app\build\outputs\apk\debug\app-debug.apk` | installs / updates SPT Camera |
| `adb shell am start -n io.github.mechanceism.sptcamera/.MainActivity` | opens the app (`--es run report` or `--es run streams` starts a test) |
| `adb logcat -s SPT` | shows the app's own log lines |
| `adb exec-out run-as io.github.mechanceism.sptcamera cat files/lens_report.json` | copies a result file out of the app's private folder |
| `adb shell am force-stop io.github.mechanceism.sptcamera` | closes the app |
| `adb uninstall io.github.mechanceism.sptcamera` | removes the app and its files |
| `adb forward tcp:8765 localabstract:sptcamera` | links PC port 8765 to the app's local socket over USB (done by `python -m spt phone`) |
| `adb forward --remove tcp:8765` | removes that link (done on exit) |
| `adb kill-server` | stops adb on the PC |

## Using the test tool

- **Lens report** (no camera opened): every camera ID including the hidden physical
  lenses, with focal length, sensor size, field of view, fx in pixels at 4K / 1080p,
  focus range, OIS, YUV sizes and their max fps; it guesses ultrawide / main /
  telephoto from the field of view. Saved as `files/lens_report.json`.
- **Stream test:** opens the back logical camera and runs, for 1080p then 4K: the
  logical camera alone, each lens alone, every pair, all three. Each run warms up
  1.5 s, then counts frames for 6 s (fps, median and max frame gap), with video
  stabilisation and OIS off and auto-exposure fixed at 30 fps. Also records the
  phone's own answer to "is this combination supported?". Saved as
  `files/streams_latest.json` (+ a timestamped copy).
- **Stop** ends a test at any time; leaving the app does too.
- **Snapshot:** one frame per lens from a single 3-lens 4K session (default, then
  "raw": per-lens full-sensor crop + distortion correction off where offered), plus
  the telephoto alone at 2560x1440. Saved as `files/snap_<lens>_<size>.jpg` and
  `files/snapshot_latest.json`; pull them with `run-as ... cat` to `output/phone/`.

USB charging warms the battery, so the watchdog may pause between runs to cool down.

## S23 results (2026-11-27, SPT Camera 0.2)

### Lenses (lens report)

Back logical camera **0** (LEVEL_3, *approximate* sensor sync, realtime timestamps) =
physical **2 + 5 + 6**:

| ID | Lens | f | 35 mm eq. | Sensor | Nominal HFOV | Focus | OIS | Largest 16:9 YUV |
|---|---|---|---|---|---|---|---|---|
| 2 | ultrawide | 2.2 mm | 13.6 mm | 4000x3000, 1.4 µm | 104° | fixed | no | 3840x2160 @30 |
| 5 | main | 5.4 mm | 22.9 mm | 4080x3060, 2.0 µm | 74° | AF / manual | yes | 3840x2160 @30 |
| 6 | telephoto 3x | 7.0 mm | 66 mm | 3648x2736, 1.0 µm | 29° | AF / manual | yes | 2560x1440 @30 |

Front: 1 (25 mm eq., 4K) and 3 (30 mm eq.). No lens publishes intrinsics, distortion
or a minimum focus distance, and none offers a distortion-correction switch.

### What the streams really are (snapshot, SIFT scale vs. the main lens)

| Stream | fx measured (px) | HFOV | Notes |
|---|---|---|---|
| 5 main @ 3840x2160 | 2541 (nominal) | 74° | reference |
| 2 ultrawide @ 3840x2160 | **~2115** | **~84°** | the phone corrects the ultrawide's distortion and crops to ~84°; no switch to turn it off. Straight lines stay straight. |
| 6 tele @ 2560x1440 | ~4900-4985 | 29° | full sensor width, nominal 4912 |
| 6 tele asked for 3840x2160 | ~3700 | 29° | **delivers 1920x1440** (4:3), not 4K |

So the ultrawide is effectively a ~84° rectilinear camera, which is fine: we calibrate
each stream as delivered. The telephoto's best stream is 2560x1440: ~1.9x the main
lens's pixels per mm at 4K.

### Streams at once (stream test, 6 s per run, AE fixed at 30 fps)

All 16 runs configured, the phone answered "supported" for all, and every stream ran
at **~30 fps (29.7-30.0)**: the logical camera, each lens alone, every pair and all
three, at 1080p and at "4K" (telephoto at 1920x1440 inside 4K runs). Worst frame gap
33-48 ms (no dropped streams). Battery 31.9 -> 33.1 °C over the sweep (charging over
USB), no thermal warning, no cool-down needed.

Second sweep (with timestamp offsets): same picture, battery 33.2 -> 33.7 °C. In every
multi-lens run the streams carry **identical timestamps (offset 0.0 ms)**: the phone
stamps all lenses of one capture with the same time, so timestamps can't show how well
the exposures really line up (the declared sync is only *approximate*). That matters
little here: a point is captured while the probe is held still and averaged over ~15
frames. If moving-probe tracking is ever needed, test real sync by filming something
fast with two lenses.

### What this means

- Three lenses at once at 30 fps works and stays cool: main 4K + ultrawide 4K (~84°)
  + telephoto 2560x1440 is the useful combination (the 3-lens session gives the tele
  1920x1440; check whether a mixed-size session gives it 2560x1440).
- Each stream needs its own calibration through the video pipeline (as delivered,
  including the ultrawide's built-in correction), with focus locked on main and tele.
- Next for the app: live preview + on-phone marker detection, then sending raw
  observations to the PC.

## Live view and the PC link (SPT Camera 0.4-0.6)

- **Live** (on the phone): streams all three back lenses (each asked for 4K: main and
  ultrawide 3840x2160, tele 1920x1440), detects DICT_5X5_100 markers (sub-pixel) and
  the ChArUco boards of `spt/boards.py` with OpenCV 5.1.0 (same as the PC), and shows
  one lens with the detections. **Lock focus** freezes main and tele at their current
  autofocus distance. The camera stops when the screen is left and pauses when warm.
- **PC link:** while Live is open, `python -m spt phone` forwards a port over USB and
  receives every detected frame (`spt/phone.py`: `PhoneLink`, frames become the same
  `Detection` objects the PC tools use). Commands: lock / unlock / set focus per lens,
  board definitions (the PC sends `spt.boards`), detection settings, preview lens.

```powershell
.venv\Scripts\python -m spt phone --lock-focus 4 --record          # until Ctrl+C
.venv\Scripts\python -m spt phone --max-fps 5 --min-marker-px 30   # tune the phone's detector
```

### Results (S23, Kamvas boards at ~0.4 m)

- All three lenses find every reference (24) and target (12) corner; the tele sees
  part of the boards (narrow view).
- Per-lens manual focus works (both lenses report exactly the distance set). Focus
  distance 0 (infinity) is ignored by the S23, so the app uses 0.01 D (100 m) instead.
  The tele appears not to focus closer than ~0.6 m.
- **Main 4K + tele 2560x1440 in one session kills the camera device** (although the
  phone reports it supported); the app uses the proven all-4K request instead.
- Detection load (three lenses, 4K):

| Setting | 4K lenses | Tele 1920x1440 | Heat |
|---|---|---|---|
| Full-res, every frame (0.5) | 5-9 fps, 70-200 ms | 15-19 fps, 20-50 ms | thermal headroom 0.83 -> 0.85 in 13 s: paused |
| ArUco3 + 10 fps cap (0.6 default) | ~8-9 fps, 15-45 ms | ~9 fps, 5-20 ms | headroom 0.76 -> 0.81 in 60 s, battery 34.7 -> 36.7 °C (charging) |

Still warming slowly at 3 x 10 fps: run only the lenses needed, lower `--max-fps`
when idle, or detect at a lower rate until the probe is held still.

## The phone as a camera for the live tools (0.7)

`--camera phone:main` (or `phone:ultrawide`, `phone:tele`, or a lens ID) works with
`calibrate-live`, `measure-live`, `pivot` and `digitize`. Only that lens streams on
the phone; the tools measure with the corners the phone detected at full resolution
and draw on a small preview picture from the phone.

- `calibrate-live` waits until the board has been in view for 2 s, then locks focus
  and stores the distance (`focus_d`) in the calibration; the other tools restore it.
  The raw observations are the detected corners (`views.json`), not images.
- Steadiness is scaled to the stream (`GuideConfig.for_stream`): at 4K and 15
  detections/s a hand-held phone counts as still at ~4 px per frame, the same
  steadiness as 1 px at 1080p / 30 fps.
- Heat with one lens (main, 4K, 10 detections/s): thermal headroom 0.58 -> 0.63 in
  the first 20 s, then level; battery 30.9 -> 32.2 °C over 60 s.

### Main lens calibration (2026-11-27)

`python -m spt calibrate-live --camera phone:main --scene output/scene_calib.json`,
phone hand-held around the Kamvas board, focus locked at 2.77 D (0.36 m):

| | S23 main (4K) | NexiGo (1080p) |
|---|---|---|
| RMS | **0.244 px** | 0.18 px |
| fx | 2579.7 ± 3.8 px (nominal 2541) | 1471.2 ± 1.2 px |
| coverage | 100 % | 85 % |
| views | 21 | |

0.24 px at 4K is ~0.12 px at webcam scale, with 1.75x the pixels per mm.

### Live measurement test, main lens (2026-11-27)

`python -m spt measure-live --calib output/phone_5.calib.json --scene output/scene_measure.json`:
phone **hand-held** 0.41-0.52 m from the Kamvas (focus restored to 0.36 m), 12
captures of 15 frames, truth 175.49 mm.

| | S23 main, hand-held | NexiGo, fixed (docs/CAMERAS.md) |
|---|---|---|
| single frame | **0.39 mm** (x 0.06, y 0.05, z 0.38) | 0.48 mm (x 0.17, y 0.16, z 0.42) |
| frame noise within a capture | 0.13 mm | |
| one capture (15 frames) | 0.31 mm, worst 0.62 | 0.27 mm, worst 0.58 |
| mean of all captures | 0.19 mm (12 captures) | 0.05 mm (45 captures) |

In-plane error is a third of the webcam's (x/y 0.03-0.04 mm per capture); depth (z)
is again almost all of it. Hand-held at 0.4-0.5 m the phone already matches the fixed
webcam per capture, well inside the 1-2 mm target.

### Zoom crops the physical streams (snapshots, SIFT scale vs. the main lens)

The logical camera's zoom (`CONTROL_ZOOM_RATIO`, 0.6-10) crops physical streams:

| Ultrawide stream | fx (4K) | HFOV |
|---|---|---|
| alone, default zoom 1.0 | ~2624 | 72° (= main lens: ultrawide sensor cropped ~58 % and upscaled; a sixth of the main lens's fine detail) |
| in the 3-lens session, zoom 1.0 | ~2187 | 83° |
| alone or 3-lens, **zoom 0.6** | ~1570 | **102°** (its full view) |

Zoom 0.6 leaves the main (scale 1.000) and tele (fx ~3712) unchanged. The Live rig
always runs at the minimum zoom, and phone calibrations store `zoom` and
`session_lenses`: a calibration holds only for the same session. (The first ultrawide
calibration, fx 2638 at zoom 1.0, was of the cropped stream:
`output/phone_2_zoom1_cropped.calib.json`, not used.)

### All three lenses calibrated (2026-11-27)

Hand-held around the Kamvas board (`calibrate-live --camera phone:<lens>`):

| Lens | Stream | RMS | fx | Focus locked | Model | Views / coverage |
|---|---|---|---|---|---|---|
| main (5) | 3840x2160 | 0.244 px | 2579.7 ± 3.8 | 2.77 D (0.36 m) | 5-term | 21 / 100 % |
| ultrawide (2) | 3840x2160, zoom 0.6 | 0.245 px | 1579.6 ± 2.7 | fixed focus | 8-term rational (`--rational`) | 23 / 100 % |
| tele (6) | 1920x1440 | 0.273 px | 3806.3 ± 6.2 | 2.49 D (0.40 m) | 5-term | 15 / 100 % |

- The tele focused at 0.40 m (closer than guessed); its fx is 2.5 % above the
  far-scene snapshot (lens breathing at close focus).
- The tele's large radial terms (k2 38, k3 -290) are real for this lens: refitting the
  saved corners with k3 fixed doubles the error (0.61 px), with k2 and k3 fixed 1.22 px.
- Pixels per mm at 1 m: ultrawide 1.58, main 2.58, tele 3.81 (tele at 1920 wide).

## Several lenses together (`measure-multi`, 2026-11-27)

`python -m spt measure-multi` streams all three lenses in one session (zoom 0.6, main
and tele focused at their calibrated distances), pairs frames by timestamp (the app
picks the same frames on every lens), lets each lens measure the target in the
reference frame with its own calibration, and fuses the points (fx² weights). It
reports each lens alone, equal weights and the fusion against the exact truth.

Two runs, phone hand-held ~0.45 m from the Kamvas measure layout:

| Capture RMS | main | tele | ultrawide | fused (fx²) | fused (1/frame variance) |
|---|---|---|---|---|---|
| run 1 (12 captures; 8 with main + tele) | 0.55 | 0.41 | - | **0.32** | |
| run 2 (5 captures) | **0.30** | 0.86 | - | 0.63 | 0.54 |

What decides it:

- **The tele sees only part of the boards at 0.45 m** (29° view): few corners, often
  near its edge. Its frames were steady (0.15-0.4 mm spread) but up to 1.4 mm off:
  a *bias*, which neither averaging nor noise-based weights can detect. Weighted by
  fx², it then pulls the fused point off.
- **The ultrawide found nothing**: the measure-layout markers (~10 mm on the Kamvas)
  are ~35 px in it at 0.45 m, under the phone detector's 40 px minimum
  (`--min-marker-px`); it then idles at the search rate.
- **The main lens alone was the most consistent** (0.30-0.55 mm; 0.55 when held beyond
  its locked focus distance).
- With lenses 1-2 cm apart, the views are nearly the same, so depth errors are
  largely shared: fusion can average noise but not the geometry.

Conclusion: on one phone, **choose the lens for the distance** rather than fusing:
main for close work, tele only where it sees whole boards (farther away), ultrawide
for keeping reference markers in view. Fusion pays off with different *viewpoints*
(separate devices), and needs per-lens uncertainty from geometry (corner count,
board size in view), not focal length alone.

## Two devices: phone (main) + NexiGo (2026-11-28)

`measure-multi` takes webcam calibrations too; each camera measures the target in the
reference frame and gets a **3x3 uncertainty** from its two board fits
(`pose.cov6` = sigma² (JᵀJ)⁻¹ per fit, `measure.point_covariance` propagates both).
It is long along the camera's line of sight, so cameras looking from different
directions cover each other's depth: `--weighting cov` fuses by inverse covariance.

Simulation (`tests/test_fusion_geometry.py`, 0.3 px corner noise, per frame):

| cameras apart | front alone | side alone | equal mean | covariance-fused |
|---|---|---|---|---|
| 10° | 0.99 | 0.83 | 0.65 | 0.56 mm |
| 20° | 1.07 | 0.58 | 0.59 | 0.33 mm |
| 40° | 1.01 | 0.62 | 0.61 | **0.20 mm** |
| 60° | 1.01 | 0.58 | 0.58 | 0.16 mm |

(The phone's own lenses are ~2° apart at 0.45 m: no gain, as measured.)

First real run (phone fixed ~0.36 m, NexiGo ~40° to the side, Kamvas turned between
captures; the NexiGo now only offers YUY2 1080p at 16.5 fps, MJPG gone on both USB
ports - same sensor and size, so its calibration holds):

| 3 captures with both cameras | RMS | x | y | z |
|---|---|---|---|---|
| phone main | 0.58 | 0.29 | 0.23 | 0.44 |
| NexiGo | **0.10** | 0.05 | 0.03 | 0.08 |
| equal mean | 0.31 | 0.12 | 0.11 | 0.26 |
| covariance-fused (floor 0.05 mm) | 0.42 | 0.22 | 0.15 | 0.32 |

- Too few joint captures: the phone missed the target board in 497 of 816 frame
  groups, the NexiGo in 329 - both boards must stay in both views.
- The phone's error was a consistent **bias** (x ≈ -0.3, z +0.3..0.6 mm), likely the
  Kamvas cover glass seen at an angle (cf. the touch test's -1.57 mm z).
- The board-fit covariance only knows *noise*; it trusted the closer, higher-resolution
  phone more than its bias deserved. Every camera now gets a systematic floor
  (`--floor-mm`, default 0.25 mm) on top of its fit.
- A fair test of fusion needs printed (non-emissive) boards: no glass, no refraction.

Second run (2026-11-28, systematic floor 0.25 mm, phone at 6 detections/s to stay
cool, 16 captures, smaller Kamvas turns):

| 6 captures with both cameras (>= 5 of 15 frames each) | RMS | x | y | z |
|---|---|---|---|---|
| phone main | 0.27 | 0.19 | 0.08 | 0.18 |
| NexiGo | 0.37 | 0.26 | 0.08 | 0.25 |
| equal mean | 0.24 | 0.20 | 0.04 | 0.12 |
| **covariance-fused** | **0.18** | 0.16 | 0.03 | **0.08** |

All 16 captures (whichever cameras saw both boards): phone 0.30 (11), NexiGo 0.34
(14), fused 0.27 mm (16). Error correlation phone-NexiGo across captures: x +0.58,
y -0.70, **z -0.63**: from two directions the depth errors oppose each other, and the
covariance weighting cancels them (depth 0.18 / 0.25 -> 0.08 mm). Neither camera had
a notable mean bias this time (|mean| <= 0.08 mm), so the first run's phone offset
depended on its viewing angle. Both cameras still lost the target board in about half
the frame groups: keep the boards inside both views.
