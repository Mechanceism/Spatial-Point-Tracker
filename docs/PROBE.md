# Probe

The probe carries a marker board and a tip. The first version is the **S23 showing a
ChArUco board on its screen**, with a pencil attached as a sharp tip. Later versions:
a 3D-printed triangular prism with markers on its faces and a wand through the
middle, with interchangeable sharp and ball tips.

## Probe frame

The probe board's frame, in mm: origin at the board's top-left outer corner, **x**
right, **y** down (towards the bottom of the phone), **z** into the board (behind
the screen). The tip is a point in this frame. For the S23 + pencil it is roughly
centred, about 50 mm above the top edge of the screen and 15 mm behind it:
`(29.9, -60.2, 15)` mm.

A probe file (`*.probe.json`) holds the board (with its true square size), where it
sits on the screen, and the tip: its type (`sharp` / `ball` + radius), the calibrated
offset, the hand-measured guess, and the pivot residual.

## 1. Board image for the phone

```
python -m spt probe-image                       # S23 defaults: 1080 x 2340, 425 ppi
```

This writes `output/probe_s23.png` and `output/probe_s23.probe.json`. The board is
4 x 8 squares of 250 px (about 15 mm), marker IDs 40-55, so it never clashes with
the reference (0-16) or target (20-29) boards.

Show the PNG **full screen** in the phone's gallery, in portrait with the controls
hidden. Turn brightness up, auto-rotate off, and use a long screen timeout.

## 2. Measure the displayed size

The gallery may scale the image slightly, and a scale error becomes a distance error
(2 % is about 7 mm at 35 cm). With calipers (plastic, or tape on the jaws), measure
between inner corners **6 squares down** (~90 mm), and 2 across (~30 mm) as a check:

```
python -m spt probe-scale --probe output/probe_s23.probe.json --measured-mm 89.65 --squares 6
python -m spt probe-scale --probe output/probe_s23.probe.json --ppi 425     # or from the spec sheet
```

Rescaling also rescales an already calibrated tip by the same factor (exact for a
pivot done with a fixed camera), so the order of the steps doesn't matter. The S23
uses the spec-sheet 425 ppi (14.941 mm squares); a caliper reading of "15 mm" was
rounded.

## 3. Pivot calibration

```
python -m spt pivot --probe output/probe_s23.probe.json --calib output/nexigo_n930e_fhd_webcam.calib.json --monitor 1
```

Put the tip in a small **dent** that won't move: a centre-punch mark, a small hole
in wood, the centre of a hex socket, or a dimple pressed into a pencil eraser. With
the screen facing the camera, **rotate the probe around the tip**, tilting left,
right, forward and back (up to about 30 degrees) without the tip moving.

Each pose satisfies `R_i @ tip + t_i = P` (P = the dent). Least squares over all poses
gives the tip and P, with poses that don't fit dropped (tip slipped, bad detection).
The view shows the **estimated tip as a red circle**, which should sit on the pencil
point and stay there while you rotate, and the pivot as a magenta cross.

Done (long beep) at 60 samples, 25 degrees of rotation spread, and a residual of 1 mm
or less. `q` saves the tip into the probe file, and every sampled pose goes to
`output/pivot_<probe>_<time>.csv`.

First result (S23 + pencil, NexiGo, 2026-11-27): 266 samples, 37 degrees spread, tip at
(28.4, -70.8, 16.2) mm after rescaling to the spec-sheet 425 ppi (the pencil point was
~61 mm above the screen edge, not the estimated 50), **residual 1.46 mm RMS**. The residual is mostly the geometry: the tip
is ~130 mm from the centre of a 60 x 120 mm board, so a 0.5 degree orientation error
alone moves it ~1.1 mm. Design rule for the printed probe: keep the tip close to the
markers, and make the marker area at least as large as the tip's distance from it.

## 4. Digitize (and the touch test)

```
python -m spt show --monitor 3 --pitch-mm 0.153 --layout touch
python -m spt digitize --probe output/probe_s23.probe.json --calib output/nexigo_n930e_fhd_webcam.calib.json --scene output/scene_touch.json --monitor 1
```

Every frame with both the reference board and the probe visible gives the tip in the
reference frame (`tip_ref = inverse(ref) * probe * tip`), shown live as a red circle.
When the tip has stayed within 1.5 mm RMS for 15 frames, those frames are averaged
into a point (beep). The tip must then move 10 mm away before the next one.

The `touch` layout shows a smaller reference board and 8 crosshair targets (T1-T8) at
exact positions, 20-260 mm from the reference. Points within 15 mm of a target are
matched to it and its error is reported. The error is split into in-plane (x, y) and
depth (z). Depth has a constant offset, because the tip touches the cover glass a
little in front of the pixels.

Output: `output/points_<time>.csv` (name, x, y, z, spread, frames, target, error) and
`..._frames.csv` with every frame's tip and both raw poses.

### First touch test (S23 + pencil, NexiGo, Kamvas, 2026-11-27)

24 touches, 3 on each of T1-T8, 20-260 mm from the reference board:

- In-plane error **1.55 mm RMS** (worst 2.31). Almost all of it is a **constant
  +1.45 mm in x** at every target, near or far. y averages -0.25 mm.
- Depth: mean -1.57 mm with only 0.35 mm spread. That is the cover glass the tip
  rests on (above the pixels).
- With a constant offset removed, the in-plane error is **0.52 mm** (each target
  scored using an offset fitted to the other targets only). A corrected tip position
  fitted the same way gives 0.57 mm.

The two explanations for the constant offset fit equally well, because the phone's
orientation only varied by a median of 12 degrees:

1. **Tip calibration error** (the fit puts the tip ~1.1 mm further in x and ~1 mm
   in z than the pivot did). This rotates with the probe.
2. **Aiming parallax:** the crosshair is ~1.5 mm under the glass, so viewing it
   from an angle shifts where the tip gets placed. This stays fixed in the screen's
   frame.

To tell them apart: touch while looking straight down at each crosshair, and with the
phone turned well differently (sideways, tilted the other way).
