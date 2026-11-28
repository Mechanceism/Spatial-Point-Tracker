"""Collecting views of a printed prism probe for its marker map.

A view is kept when the probe is held still, shows at least two faces (so faces get
linked to each other), and is turned at least ``min_turn`` degrees from every view
kept so far. Coverage: every face needs several views, and every pair of
neighbouring faces must be seen together at least once, or the map can't tie them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .markermap import MarkerMap, Observation, locate, observe_markers
from .pose import Pose


def angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    cos = (np.trace(Ra.T @ Rb) - 1) / 2
    return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


@dataclass
class ProbeStep:
    state: str  # "none", "moving", "holding", "same", "kept"
    pose: Pose | None = None
    faces: list[int] = field(default_factory=list)


@dataclass
class ProbeMapSession:
    design: object  # spt.prism.PrismDesign
    start: MarkerMap
    calib: object
    min_turn: float = 8.0
    still_mm: float = 1.5
    still_deg: float = 1.0
    still_frames: int = 3
    views_per_face: int = 6

    def __post_init__(self):
        self.frames: list[Observation] = []
        self.kept_R: list[np.ndarray] = []
        self.seen: dict[int, int] = {}
        self.pairs: set[tuple[int, int]] = set()
        self._prev: Pose | None = None
        self._still = 0
        self._ids = set(self.design.ids)

    def face_of(self, marker_id: int) -> int:
        return (marker_id - self.design.first_id) // self.design.per_face

    def update(self, markers: dict[int, np.ndarray]) -> ProbeStep:
        markers = {i: c for i, c in markers.items() if i in self._ids}
        obs = observe_markers(markers)
        pose = locate(self.start, obs, self.calib) if len(markers) >= 2 else None
        if pose is None:
            self._prev, self._still = None, 0
            return ProbeStep("none")
        prev = self._prev
        moving = prev is None or np.linalg.norm(pose.t - prev.t) > self.still_mm or angle_deg(pose.R, prev.R) > self.still_deg
        self._prev = pose
        self._still = 0 if moving else self._still + 1
        faces = sorted({self.face_of(i) for i in markers})
        if moving:
            return ProbeStep("moving", pose, faces)
        if self._still < self.still_frames:
            return ProbeStep("holding", pose, faces)
        if len(faces) < 2:
            return ProbeStep("holding", pose, faces)
        if self.kept_R and min(angle_deg(pose.R, R) for R in self.kept_R) < self.min_turn:
            return ProbeStep("same", pose, faces)
        self.frames.append(obs)
        self.kept_R.append(pose.R)
        for i in markers:
            self.seen[i] = self.seen.get(i, 0) + 1
        n = self.design.sides
        for a in faces:
            if (a + 1) % n in faces:
                self.pairs.add((a, (a + 1) % n))
        self._still = 0
        return ProbeStep("kept", pose, faces)

    def coverage(self) -> tuple[list[int], list[tuple[int, int]]]:
        """Faces still short of views, and neighbouring faces never seen together."""
        d = self.design
        weak = [k for k in range(d.sides)
                if min(self.seen.get(d.marker_id(k, j), 0) for j in range(d.per_face)) < self.views_per_face]
        missing = [(k, (k + 1) % d.sides) for k in range(d.sides) if (k, (k + 1) % d.sides) not in self.pairs]
        return weak, missing

    def enough(self, views: int) -> bool:
        weak, missing = self.coverage()
        return not weak and not missing and len(self.frames) >= views
