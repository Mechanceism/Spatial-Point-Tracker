# Spatial-Point-Tracker

Capture 3D points on real objects with a camera, printed fiducial markers and a touch
probe, for use in other software (suspension hardpoints, vehicle body lines, CAD).

Target accuracy is 1-2 mm. Markers are the measurement source; phone AR and the IMU
only help in between. See [docs/PLAN.md](docs/PLAN.md) for the design decisions and
phases.

## Status

**Phase 0, feasibility.** Python tools to print boards, calibrate a camera and measure
how well a target board can be located relative to a reference board. How to run the
test: [docs/PHASE0.md](docs/PHASE0.md).

## Quick start

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest

python -m spt make-boards
python -m spt calibrate captures/calib --square-mm 29.97
python -m spt measure captures/measure --ref-square-mm 29.97 --tgt-square-mm 24.98 --known-mm 301.5
```

No printer? Show the boards on a flat monitor (`python -m spt show ...`). The true
position is then known exactly; see [docs/PHASE0.md](docs/PHASE0.md#2a-or-show-the-boards-on-a-flat-screen-no-printer).

## Layout

| Path | Contents |
|---|---|
| `spt/boards.py` | Board definitions and exact geometry |
| `spt/pdf.py`, `spt/printable.py` | Vector PDF of the printable boards |
| `spt/detect.py` | Photo loading and ChArUco detection |
| `spt/calibration.py` | Camera calibration and its JSON file |
| `spt/pose.py` | Board pose and rigid transforms |
| `spt/measure.py` | Phase 0 measurement and statistics |
| `spt/display.py` | Boards shown full screen on a monitor, with exact truth |
| `spt/cli.py` | `python -m spt ...` commands |
| `tests/` | Synthetic-image end-to-end tests |

Photos go in `captures/` and results in `output/`. Both are ignored by git.
