# Phase 0: feasibility test

Question: can a phone camera and printed markers locate a point to 1-2 mm?

The **reference board** stands in for markers fixed to the car and defines the
coordinate frame. The **target board** stands in for the probe. Each photo showing
both gives one independent measurement of where the target's corner 0 is in the
reference frame:

- the **spread** across photos is the *repeatability*;
- the distance compared with a hand measurement is the *accuracy*.

## 1. Setup

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest            # synthetic end-to-end tests
```

## 2. Print and mount the boards

```
python -m spt make-boards              # or --paper a4
```

This writes `output/boards/boards_letter.pdf` (page 1 reference, page 2 target) as an
exact vector drawing, plus PNG previews.

- Print at **100 % / Actual size**, never "fit to page". A laser printer is best.
- Glue each page **flat** to something rigid: glass from a picture frame, acrylic, or
  aluminium composite. Use spray adhesive, no bubbles. Foam board and cardboard warp.
- **Measure the printed scale.** It is the biggest error source you control: a 0.5 %
  scale error is 2 mm over 400 mm. With calipers, measure between the inner corners
  given on the page (reference: 5 squares across = 150 mm, 3 down = 90 mm), then
  divide by the number of squares. That is the `--square-mm` value.
  If across and down differ by more than ~0.2 %, the printer stretches one axis: try
  another printer or paper setting.

## 3. Camera settings (Samsung S23)

The calibration only holds while the camera is in exactly the same state, so for
**all** calibration and measurement photos:

- Main (1x) lens, no zoom. Each lens is a different camera.
- **Pro mode with manual focus**, set once at the working distance (~60 cm) and left
  there. Autofocus changes the lens and breaks the calibration.
- One resolution for everything (12 MP 4:3 is fine).
- Good, even light, fast enough shutter (1/125 s or faster), low ISO, no motion blur,
  no glare on the boards.
- Holding the phone in portrait or landscape is fine: the tools read the photos in
  the sensor's own orientation.

Risk to watch: the S23 main camera has optical stabilisation, which can shift the
image centre slightly between photos. Per-view errors in the calibration report will
show whether it matters.

## 4. Calibrate the camera

Take **20-30 photos of the reference board** (target board out of view):

- at roughly the distance you will measure from;
- with the board tilted 20-45 degrees in different directions;
- with the board in **every part of the frame, corners included**. This matters more
  than anything else: if the corners of the frame are never covered, the lens
  distortion there is guessed, and points measured there are wrong even though the
  calibration error looks small.

Put them in `captures/calib/` and run:

```
python -m spt calibrate captures/calib --square-mm 29.97 --debug-dir output/calib_debug
```

Good result: **RMS under 0.5 px** and **coverage of 70 % or more**. The `--debug-dir`
overlays show which corners were found. `--max-view-error 1.0` drops bad photos and
recalibrates. The calibration is written to `output/camera.calib.json`.

## 5. Measure

- Lay both boards flat on the same rigid surface, about 250-350 mm apart.
- With a steel rule, measure the distance from the reference's corner 0 to the
  target's corner 0 (the margin ticks on each page point to them).
- Take **15-20 photos** with both boards fully in view, from different positions,
  50-80 cm away, **tilted 20-40 degrees from square-on**, with the same focus and
  resolution as the calibration. Put them in `captures/measure/`.

```
python -m spt measure captures/measure --ref-square-mm 29.97 --tgt-square-mm 24.98 --known-mm 301.5 --csv output/measure.csv --debug-dir output/measure_debug
```

The report lists every photo's point and these summary lines:

- **spread (1 sd)** per axis and in 3D: repeatability. Two sd should be inside the
  target (2 mm by default, `--target-mm`).
- **vs known**: accuracy against the steel rule (which is itself only good to about
  0.5 mm).
- Photos where either board fits its pose worse than 1 px (`--max-rms`) are rejected
  and listed with the reason.

Optional extra: raise the target board on a block of known height to test the depth
(z) direction.

## What the synthetic tests already show

The tests render boards with a known, perfect camera and check what comes back:

- Even on clean images, corners are only located to about 0.1 px. That tilts the
  estimated reference pose by about 0.05-0.2 degrees, and over the 300 mm to the
  target that is up to about 1 mm, almost all **along the board normal (z)**. In-plane
  errors stay near 0.1 mm.
- **Square-on views from about 1 m are the weakest** (3 mm in z in one case).
  Tilted views at 50-70 cm are mostly under 0.5 mm, and averaging photos helps.
- **Calibration coverage is critical.** A calibration from photos with the board only
  near the centre still showed a small error (0.08 px) but had nonsense distortion,
  and points near the frame edges came out several mm off. The tools now warn about
  low coverage and reject badly fitting views.

Real photos will be noisier, which is what phase 0 measures. Implications for the
design so far: reference markers should be large and close to the points measured,
probe markers should be spread as wide as practical, and live capture should average
many frames.

## Other cameras

The same steps work for any camera, each with its own calibration file (`--out`).
The DJI Osmo Action cameras have very wide, strongly distorted lenses; try
`--rational` and expect them to need a fisheye model later. The manual scientific
camera is a useful best-case reference to compare the S23 against.
