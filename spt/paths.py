"""Where the tools put their files (all under output/, which git ignores).

    output/
      calibrations/        <camera>.calib.json (old/ for superseded ones)
      scenes/              scene_<layout>.json: boards shown on a screen = exact truth
      probes/              probe definitions: *.probe.json, *.prism.json, *.map.json
      print/               print_all_letter.pdf, boards/, markers/, templates/
      measurements/        measure/, measure_live/, measure_multi/, points/, pivot/
      phone/               lens reports, snapshots, phone stream recordings
      debug/               detection overlays (when asked for)
"""

from __future__ import annotations

from pathlib import Path

OUTPUT = Path("output")
CALIBRATIONS = OUTPUT / "calibrations"
SCENES = OUTPUT / "scenes"
PROBES = OUTPUT / "probes"
PRINT = OUTPUT / "print"
BOARDS_PRINT = PRINT / "boards"
MARKERS_PRINT = PRINT / "markers"
TEMPLATES_PRINT = PRINT / "templates"
MEASUREMENTS = OUTPUT / "measurements"
PHONE = OUTPUT / "phone"
DEBUG = OUTPUT / "debug"


def calibration(name: str) -> Path:
    return CALIBRATIONS / f"{name}.calib.json"


def scene(layout: str) -> Path:
    return SCENES / f"scene_{layout}.json"


def measurement(kind: str, filename: str) -> Path:
    """measurements/<kind>/<filename> (kind: measure, measure_live, measure_multi, points, pivot)."""
    return MEASUREMENTS / kind / filename
