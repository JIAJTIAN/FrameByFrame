from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from aswaxs_live.reduction.frame_qc import FrameSeries


@dataclass(frozen=True)
class OnlineCurveRecord:
    experiment_title: str
    experiment_uid: str
    detector: str
    sequence_index: int
    energy_index: int
    group_index: int
    frame_index: int
    energy_kev: float
    monitor_value: float
    source_path: str
    q: np.ndarray
    intensity: np.ndarray
    sigma: np.ndarray

    @property
    def label(self) -> str:
        energy = f" | {self.energy_kev:.4f} keV" if np.isfinite(self.energy_kev) else ""
        return (
            f"{self.experiment_title} | {self.detector} | "
            f"M{self.group_index:04d} F{self.frame_index:03d}{energy}"
        )


class OnlineCurveStore:
    """RAM catalog of reduced 1-D frames with shared q-grid storage."""

    def __init__(self) -> None:
        self.records: list[OnlineCurveRecord] = []
        self._q_grids: dict[tuple[str, int, int], list[np.ndarray]] = {}

    def clear(self) -> None:
        self.records.clear()
        self._q_grids.clear()

    def add_payload(self, payload: dict[str, object]) -> OnlineCurveRecord:
        detector = str(payload["detector"])
        energy_index = int(payload["energy_index"])
        q = self._shared_q(detector, energy_index, np.asarray(payload["q"], dtype=np.float32))
        record = OnlineCurveRecord(
            experiment_title=str(payload.get("experiment_title", "Online experiment")),
            experiment_uid=str(payload.get("experiment_uid", "")),
            detector=detector,
            sequence_index=int(payload["sequence_index"]),
            energy_index=energy_index,
            group_index=int(payload["group_index"]),
            frame_index=int(payload["frame_index"]),
            energy_kev=float(payload.get("energy_kev", np.nan)),
            monitor_value=float(payload.get("monitor_value", np.nan)),
            source_path=str(payload.get("source_path", "")),
            q=q,
            intensity=np.asarray(payload["intensity"], dtype=np.float32),
            sigma=np.asarray(payload["sigma"], dtype=np.float32),
        )
        self.records.append(record)
        return record

    def _shared_q(self, detector: str, energy_index: int, q: np.ndarray) -> np.ndarray:
        key = (detector, energy_index, q.size)
        candidates = self._q_grids.setdefault(key, [])
        for existing in candidates:
            if np.allclose(existing, q, rtol=1e-6, atol=1e-12, equal_nan=True):
                return existing
        q.setflags(write=False)
        candidates.append(q)
        return q

    def frame_series(self, indices: list[int]) -> tuple[str, FrameSeries]:
        if len(indices) < 2:
            raise ValueError("Select at least two reduced curves for frame-stability QC.")
        records = [self.records[index] for index in indices]
        detector_keys = {record.detector for record in records}
        if len(detector_keys) != 1:
            raise ValueError("QC curves must come from the same detector (cannot mix SAXS and WAXS).")
        records.sort(key=lambda record: record.sequence_index)
        detector = next(iter(detector_keys))
        label = f"{records[0].experiment_title} | {detector} | online selection ({len(records)} frames)"
        # Per-group frame_index is always 1 for online data (num_frames=1).
        # Use sequential position in this selection so QC axes are meaningful.
        sequential_frame_index = np.arange(1, len(records) + 1, dtype=int)
        series = FrameSeries(
            q=np.stack([record.q for record in records]),
            intensity=np.stack([record.intensity for record in records]),
            sigma=np.stack([record.sigma for record in records]),
            frame_index=sequential_frame_index,
            sequence_index=np.asarray([record.sequence_index for record in records], dtype=int),
            energy_index=np.asarray([record.energy_index for record in records], dtype=int),
            group_index=np.asarray([record.group_index for record in records], dtype=int),
            energy_kev=np.asarray([record.energy_kev for record in records], dtype=float),
            monitor_value=np.asarray([record.monitor_value for record in records], dtype=float),
            source_path=[record.source_path for record in records],
            existing_status=["online_reduced"] * len(records),
        )
        return label, series
