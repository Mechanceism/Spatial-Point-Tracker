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
