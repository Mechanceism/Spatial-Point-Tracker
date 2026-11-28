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
