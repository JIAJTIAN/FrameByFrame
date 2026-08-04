from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import TextIO

import numpy as np
import h5py
from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot

from aswaxs_live.reduction.analysis_h5 import file_sha256

from .checkpoints import read_checkpoint_uid, write_experiment_metadata, write_stage_checkpoint
from .config import OnlineConfig
from .identity import ExperimentIdentity, resolve_experiment_identity


DEFAULT_V6_ROOT = Path(__file__).resolve().parents[4]
ONLINE_MAX_MEASUREMENTS = 1_000_000


def ensure_v6_importable(v6_root: Path = DEFAULT_V6_ROOT) -> Path:
    src = v6_root.resolve() / "src"
    engine_path = src / "aswaxs_live" / "reduction" / "live.py"
    if not engine_path.is_file():
        raise FileNotFoundError(f"FrameByFrame-ASWAXS source was not found at {src}")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return src


@dataclass
class DetectorRuntime:
    identity: ExperimentIdentity
    detector: str
    assigner: object
    state: object
    event_log: TextIO
    seen_paths: set[Path]
    checkpoint_parameters: dict[str, object]


class OnlineReductionEngine(QtCore.QObject):
    log = Signal(str)
    progress = Signal(str, int, int, int, int)
    curve_ready = Signal(object)
    image_ready = Signal(str, object, object)
    experiment_discovered = Signal(object)
    output_updated = Signal(str)
    error = Signal(str)
    ready = Signal()
    stopped = Signal()

    def __init__(self, config: OnlineConfig, session_dir: Path, v6_root: Path = DEFAULT_V6_ROOT) -> None:
        super().__init__()
        self.config = config
        self.session_dir = Path(session_dir).expanduser().resolve()
        self.v6_root = v6_root
        self._runtimes: dict[tuple[str, str], DetectorRuntime] = {}
        self._announced_experiments: set[str] = set()
        self._active = False
        self._reducer = None
        self._core = None

    @Slot()
    def initialize(self) -> None:
        try:
            ensure_v6_importable(self.v6_root)
            from aswaxs_live.reduction import live as reducer

            self._reducer = reducer
            self._core = reducer.load_reduction_core()
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self._active = True
            self.log.emit("FrameByFrame online 1-D reduction engine is ready")
            self.ready.emit()
        except Exception as exc:
            self.error.emit(f"Could not initialize V6 engine: {exc}")

    def _make_runtime(self, identity: ExperimentIdentity, detector: str) -> DetectorRuntime:
        reducer = self._reducer
        experiment_dir = self.session_dir / identity.storage_name
        detector_dir = experiment_dir / detector
        detector_dir.mkdir(parents=True, exist_ok=True)
        args = self._runtime_args(detector, detector_dir, identity.safe_title)
        analysis_path = reducer.prepare_output_records_for_run(args, detector_dir)
        event_path = detector_dir / "live_events.jsonl"
        event_log = event_path.open("a" if args.resume and event_path.exists() else "w", encoding="utf-8")
        assigner = reducer.SequenceAssigner(
            ONLINE_MAX_MEASUREMENTS,
            self.config.num_frames,
            1,
        )
        expected_counts = defaultdict(lambda: self.config.num_frames)
        # Online acquisition is open-ended. This sentinel prevents the shared
        # batch state from treating an arbitrary measurement as a finished
        # energy block; every completed measurement is persisted separately.
        expected_groups = {1: {ONLINE_MAX_MEASUREMENTS + 1}}
        state = reducer.LivePipelineState(
            args=args,
            output_dir=detector_dir,
            event_log=event_log,
            core=self._core,
            expected_frame_counts=expected_counts,
            expected_energy_groups=expected_groups,
            monitor_key=args.monitor_key,
            detector=detector,
        )
        state.geometry_metadata.update(identity.metadata())
        state.image_callback = lambda item, image, name=detector, experiment=identity: self._emit_image_preview(
            name, item, image, experiment
        )
        # Restore V6's pending curves, completed averages, and sequence position.
        # Advancing the counter alone would lose unfinished group state.
        resume_items = reducer.compatible_resume_items_or_quarantine(analysis_path, self._core, args)
        seen: set[Path] = set()
        if resume_items:
            seen = reducer.restore_state_from_analysis_h5(state)
            max_sequence = max(item.sequence_index for item in resume_items)
            assigner.advance_to_sequence_index(max_sequence + 1)
            self.log.emit(f"{detector}: restored {len(seen)} frame(s); next sequence is {max_sequence + 1}")
        checkpoint_parameters = {
            "detector": detector,
            "poni_file": str(args.poni),
            "poni_file_hash": file_sha256(args.poni),
            "mask_file": str(args.mask),
            "mask_file_hash": file_sha256(args.mask),
            "dataset_path": str(args.dataset_path),
            "monitor_key": str(args.monitor_key),
            "npt": int(args.npt),
            "unit": str(args.unit),
            "expected_frames_per_group": int(self.config.num_frames),
            "sequence_mode": "open-ended measurements; energy read from raw HDF5 metadata",
        }
        return DetectorRuntime(identity, detector, assigner, state, event_log, set(seen), checkpoint_parameters)

    def _runtime_args(self, detector: str, output_dir: Path, sample_name: str) -> argparse.Namespace:
        cfg = self.config
        poni = cfg.pil300k_poni if detector == "Pil300K" else cfg.eig1m_poni
        mask = cfg.pil300k_mask if detector == "Pil300K" else cfg.eig1m_mask
        monitor = cfg.pil300k_monitor_key if detector == "Pil300K" else cfg.eig1m_monitor_key
        parser = self._reducer.build_parser()
        args = parser.parse_args([
            "--watch-dir", str(output_dir),
            "--sample-name", f"{sample_name}_{detector}",
            "--output-dir", str(output_dir),
            "--analysis-h5", str(output_dir / f"{sample_name}_{detector}_analysis.h5"),
            "--analysis-mode", "saxs",  # dual-detector correction happens after stitching
            "--poni", poni,
            "--mask", mask,
            "--detector", detector,
            "--monitor-key", monitor,
            "--dataset-path", cfg.dataset_path,
            "--num-energies", "1",
            "--num-groups", str(ONLINE_MAX_MEASUREMENTS),
            "--num-frames", str(cfg.num_frames),
            "--npt", str(cfg.npt),
            "--quiet",
        ])
        args.resume = True
        args.write_text_output = False
        args.export_xanos = False
        args.analysis_write_interval_groups = 1
        return args

    @Slot(str, str, object)
    def process_file(self, detector: str, path_text: str, _payload: object = None) -> None:
        if not self._active:
            return
        try:
            path = Path(path_text).expanduser().resolve()
            payload = dict(_payload) if isinstance(_payload, dict) else {}
            identity = resolve_experiment_identity(
                path,
                payload,
                detector=detector,
                fallback_title=self.config.sample_name,
            )
            runtime_key = (identity.experiment_uid, detector)
            runtime = self._runtimes.get(runtime_key)
            if runtime is None:
                runtime = self._make_runtime(identity, detector)
                self._runtimes[runtime_key] = runtime
                if identity.experiment_uid not in self._announced_experiments:
                    self._announced_experiments.add(identity.experiment_uid)
                    self.experiment_discovered.emit(
                        {**identity.metadata(), "storage_name": identity.storage_name, "safe_title": identity.safe_title}
                    )
                self.log.emit(
                    f"Experiment {identity.title} [{identity.experiment_uid[:8]}]: opened {detector} checkpoint"
                )
            if path in runtime.seen_paths:
                self.log.emit(f"{detector}: duplicate ignored: {path.name}")
                return
            ready, reason = self._reducer.file_is_ready(path, self.config.dataset_path, self.config.settle_seconds)
            if not ready:
                raise RuntimeError(f"source file is not ready ({reason}): {path}")
            position = runtime.assigner.next_position()
            item = self._core.ManifestItem(
                sequence_index=position.sequence_index,
                energy_index=position.energy_index,
                group_index=position.group_index,
                frame_index=position.frame_index,
                path=path,
            )
            self.log.emit(
                f"{detector}: E{position.energy_index:03d} G{position.group_index:03d} "
                f"F{position.frame_index:03d} <- {path.name}"
            )
            curves = runtime.state.process_item(item)
            runtime.seen_paths.add(path)
            for curve in curves:
                self.curve_ready.emit(self._curve_payload(detector, curve, identity))
            self.progress.emit(
                detector,
                position.sequence_index,
                position.energy_index,
                position.group_index,
                position.frame_index,
            )
            self._write_detector_checkpoints(runtime)
            self.output_updated.emit(str(runtime.state.analysis_path))
            self._update_stitching(identity)
        except StopIteration:
            self.error.emit(f"{detector}: configured sequence is complete; message ignored")
        except Exception as exc:
            self.error.emit(f"{detector}: reduction failed: {exc}")

    def _emit_image_preview(
        self, detector: str, item: object, image: np.ndarray, identity: ExperimentIdentity
    ) -> None:
        values = np.asarray(image)
        row_step = max(1, int(np.ceil(values.shape[0] / 700)))
        column_step = max(1, int(np.ceil(values.shape[1] / 700)))
        preview = np.asarray(values[::row_step, ::column_step], dtype=np.float32)
        metadata = {
            "sequence_index": int(item.sequence_index),
            "energy_index": int(item.energy_index),
            "group_index": int(item.group_index),
            "frame_index": int(item.frame_index),
            "experiment_title": identity.title,
            "experiment_uid": identity.experiment_uid,
        }
        self.image_ready.emit(detector, preview, metadata)

    @staticmethod
    def _curve_payload(
        detector: str, curve: object, identity: ExperimentIdentity
    ) -> dict[str, object]:
        item = curve.item
        return {
            "detector": detector,
            "sequence_index": int(item.sequence_index),
            "energy_index": int(item.energy_index),
            "group_index": int(item.group_index),
            "frame_index": int(item.frame_index),
            "energy_kev": np.nan if curve.energy_kev is None else float(curve.energy_kev),
            "monitor_value": float(curve.monitor_value),
            "source_path": str(item.path),
            "experiment_title": identity.title,
            "experiment_uid": identity.experiment_uid,
            "q": np.asarray(curve.q, dtype=np.float32),
            "intensity": np.asarray(curve.normalized_intensity, dtype=np.float32),
            "sigma": np.asarray(curve.normalized_error, dtype=np.float32),
        }

    def _write_detector_checkpoints(self, runtime: DetectorRuntime, *, final: bool = False) -> None:
        analysis_path = runtime.state.analysis_path
        write_experiment_metadata(analysis_path, runtime.identity)
        written_frames = len(runtime.state.items)
        expected_frames = written_frames if final else 0
        integration_uid = write_stage_checkpoint(
            analysis_path,
            "detector_integration",
            identity=runtime.identity,
            status="complete" if final else "partial",
            output_group_path="/entry/realtime/process_01_reduction/frames",
            expected_items=expected_frames,
            written_items=written_frames,
            parameters=runtime.checkpoint_parameters,
        )
        written_groups = len(runtime.state.completed_averages)
        pending_groups = sum(bool(curves) for curves in runtime.state.pending_group_curves.values())
        expected_groups = written_groups + pending_groups if final else 0
        averaging_complete = final and pending_groups == 0
        write_stage_checkpoint(
            analysis_path,
            "group_averaging",
            identity=runtime.identity,
            status="complete" if averaging_complete else "partial",
            output_group_path="/entry/process_01_reduction/data",
            expected_items=expected_groups,
            written_items=written_groups,
            parameters={
                "outlier_zmax": float(runtime.state.runtime_args.outlier_zmax),
                "expected_frames_per_group": int(self.config.num_frames),
                "averaging_method": "monitor-normalized mean after total-intensity outlier rejection",
            },
            input_checkpoint_ids=[integration_uid],
            validation_message=(
                "All received measurements contain the configured frame count."
                if averaging_complete
                else f"{pending_groups} measurement(s) do not yet contain {self.config.num_frames} frames."
            ),
        )

    def _update_stitching(self, identity: ExperimentIdentity, *, final: bool = False) -> None:
        from aswaxs_live.reduction.stitching import update_live_stitched_averages

        if any((identity.experiment_uid, detector) not in self._runtimes for detector in ("Pil300K", "Eig1M")):
            return
        root = self.session_dir / identity.storage_name
        combined = root / f"{identity.safe_title}_analysis.h5"
        updated = update_live_stitched_averages(
            root / "Pil300K",
            root / "Eig1M",
            combined_h5_path=combined,
            sample_names=[identity.safe_title],
        )
        if updated is None and (not final or not combined.exists()):
            return
        write_experiment_metadata(combined, identity)
        with h5py.File(combined, "r") as handle:
            curves = handle.get("/entry/stitched_averages/curves")
            written_rows = len(curves) if isinstance(curves, h5py.Group) else 0
        detector_group_counts = [
            len(self._runtimes[(identity.experiment_uid, detector)].state.completed_averages)
            for detector in ("Pil300K", "Eig1M")
        ]
        expected_rows = min(detector_group_counts) if final else 0
        source_ids = [
            read_checkpoint_uid(self._runtimes[(identity.experiment_uid, detector)].state.analysis_path, "group_averaging")
            for detector in ("Pil300K", "Eig1M")
        ]
        write_stage_checkpoint(
            combined,
            "detector_stitching",
            identity=identity,
            status="complete" if final and written_rows == expected_rows else "partial",
            output_group_path="/entry/stitched_averages/curves",
            expected_items=expected_rows,
            written_items=written_rows,
            parameters={
                "detectors": ["Pil300K", "Eig1M"],
                "scaling": "robust detector overlap scale or nearest-edge estimate without synthetic q points",
            },
            input_checkpoint_ids=[value for value in source_ids if value],
        )
        self.log.emit(f"{identity.title}: updated stitched 1-D detector averages -> {combined.name}")
        self.output_updated.emit(str(combined))

    @Slot()
    def shutdown(self) -> None:
        self._active = False
        identities = {runtime.identity.experiment_uid: runtime.identity for runtime in self._runtimes.values()}
        for runtime in self._runtimes.values():
            try:
                runtime.state.write_analysis_h5(force=True)
                self._write_detector_checkpoints(runtime, final=True)
            except Exception as exc:
                self.error.emit(f"{runtime.detector}: final HDF5 write failed: {exc}")
        for identity in identities.values():
            try:
                self._update_stitching(identity, final=True)
            except Exception as exc:
                self.error.emit(f"{identity.title}: final stitching checkpoint failed: {exc}")
        for runtime in self._runtimes.values():
            runtime.event_log.close()
        self._runtimes.clear()
        self.stopped.emit()
