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

USB charging warms the battery, so the watchdog may pause between runs to cool down.
