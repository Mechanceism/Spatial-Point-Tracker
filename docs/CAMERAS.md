# Cameras

## Commands

```
python -m spt cameras            # names, indices and supported modes
python -m spt cameras --test     # also open each one and measure the real frame rate
python -m spt preview --camera nexigo
```

`--camera` takes an index or any part of the name (`nexigo`, `osmo`, `uc40m`).
Virtual cameras (NVIDIA Broadcast, OBS) are hidden from the list (`--all` shows them).

The preview opens each camera at its largest MJPG mode that runs at 15 fps or more
(`--mode 1280x720` overrides) and detects both phase 0 boards live. It shows:

- the camera's real frame rate, the displayed rate, processing time per frame, and
  **lag** (time from frame arrival to display);
- **sharpness** (variance of the Laplacian over the centre of the image), useful for
  checking focus;
- corners found on each board.

Keys: `q`/Esc quit, `s` save the full-resolution frame to `captures/preview/`,
`c` open the driver's settings dialog (focus, exposure, white balance), `d` toggle
detection.

## Guided live calibration

```
python -m spt show --monitor 3 --pitch-mm 0.153 --layout calib      # board on the Kamvas
python -m spt calibrate-live --camera nexigo --scene output/scene_calib.json --monitor 1
```

Hold the board (Kamvas, phone screen or print) in front of the camera and move it
around. The camera view is shown mirrored like a selfie, with:

- **red cells:** parts of the frame the board still has to visit;
- **border colour:** yellow while holding still (with a progress bar along the
  bottom), green flash and a beep on capture, red when no board is seen;
- the status line (views, coverage, tilt range, error) and a hint for what to do next.

A frame is captured when the board has been **still** for about 0.2 s, at least half
of its corners are visible, and the view is **new**: it covers a cell not yet covered,
or has a tilt more than 10 degrees from views already taken in that part of the frame.
After 6 views the camera is recalibrated on every capture, and a view fitting much
worse than the rest (blur, glare) is dropped with a low beep.

It's **done** (long beep, hint says DONE) at 15 views, 80 % coverage, tilts of at
least 20 degrees left/right and forward/back, and an error of 0.6 px or less. Press `q`
to save. Esc quits without saving. The calibration goes to `output/<camera>.calib.json`
and records the camera and mode. The accepted frames are saved to
`captures/calib_live/...` so `python -m spt calibrate` can redo it offline.

First run (2026-11-27), NexiGo N930E at 1920x1080 with the board on the Kamvas
held at 29-42 cm: 17 views captured, **RMS 0.18 px**, coverage 85 %, fx 1471.2 +/- 1.2 px
(66 degree horizontal field of view). Turn tilts reached 35 degrees, but forward/back
only 18 (goal 20), so the "done" criteria weren't met; with fx already determined to
0.08 %, this is a minor gap. For comparison, the S23 photos had fx 2705 px over a 73
degree view, so the S23 resolves about 1.8x finer detail per degree than the NexiGo.

## Live measurement test

```
python -m spt show --monitor 3 --pitch-mm 0.153 --layout measure    # both boards on the Kamvas
python -m spt measure-live --calib output/nexigo_n930e_fhd_webcam.calib.json --monitor 1
```

Hold the screen with both boards in front of the camera. Every frame measures the
target corner in the reference frame against the exact truth. Holding still for 15
frames (`--frames`) makes a **capture** (the average of those frames); then move or
turn the screen (20 mm or 5 degrees) for the next one. `q` finishes and prints:

- **single frame:** error of individual frames;
- **frame noise:** spread of frames within one capture;
- **one capture:** error after averaging frames from one viewpoint;
- **mean of captures:** error after also averaging over viewpoints.

Every frame is written to `output/measure_live_<camera>_<time>.csv`, and one image
per capture goes to `captures/measure_live/`.

### Result: NexiGo + Kamvas (2026-11-27)

45 captures (2645 frames) at 245-410 mm, against the exact 175.49 mm truth:

| Level | Error |
|---|---|
| Single frame | 0.48 mm RMS (x 0.17, y 0.16, **z 0.42**) |
| Frame noise within a capture | 0.32 mm |
| One capture (15 frames, ~0.5 s) | **0.27 mm RMS**, worst 0.58 mm |
| Average of 3 captures from different viewpoints | 0.16 mm RMS, 95 % within 0.28 mm |
| Mean of all 45 captures | 0.05 mm |

- About 2.4x better than the S23 photos of phase 0 (1.16 mm per photo), despite
  half the resolution. Likely reasons: a better calibration (0.18 vs 0.54 px, made
  live through the same pipeline), a closer distance, and no phone image processing
  or stabilization in between.
- **Averaging frames does help** with a webcam: frame noise (0.32 mm) is a real part
  of the single-frame error. After 15 frames, what's left is almost all viewpoint
  error (0.25 of the 0.27 mm), which only averaging different viewpoints reduces.
- Error grew with distance even within this range (0.23 mm at 240-320 mm, 0.35 mm at
  370-420 mm).
- Caveat: best case. The screen is perfectly flat, the boards are large in the frame,
  and the distances are short.

## How frames are read

`LiveCamera` reads the camera on a background thread and keeps only the newest frame,
so processing never works through a queue of old frames. Lag stays at about one frame.

**DirectShow gotcha:** the format (MJPG) must be set **after** the frame rate and
size. Setting size or frame rate later silently resets the camera to uncompressed
YUY2, which runs at 1-5 fps at high resolutions (the UC40M dropped to 1 fps at 4K).
`LiveCamera` sets them in the right order and a test guards it.

## Measured (2026-11-27, dim room)

| Camera | Mode | Frame rate | Notes |
|---|---|---|---|
| UC40M | 3840x2160 MJPG | 20 fps camera, ~15 fps shown | Full-frame board detection takes ~56 ms at 4K. IR 5-50 mm varifocal CS lens: set zoom and focus once, lock the rings, one calibration per setting. |
| NexiGo N930E | 1920x1080 MJPG | 25 fps | Fixed focus, clean image. Development camera for now. |
| Osmo Action 4 | 1920x1080 MJPG | 30 fps | Clearest image. **Stabilization must be off** (it warps each frame differently). Keep one lens mode per calibration; ultrawide needs the fisheye model. |

Frame rates below 30 are most likely auto-exposure lengthening frames in dim light.
