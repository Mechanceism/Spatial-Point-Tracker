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
```

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
or less. `q` saves the tip into the probe file.
