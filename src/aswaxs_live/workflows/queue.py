"""Task queue model for FrameByFrame-ASWAXS GUI runs."""

from __future__ import annotations

import json
import os
import re
import threading
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import h5py

from aswaxs_live.workflows.task import AsaxsPair, TaskSpec, safe_name
from aswaxs_live.paths import PROJECT_DIR
from aswaxs_live.reduction.stitching import (
    StitchedAsaxsSettings,
    read_detector_group_averages,
    update_live_stitched_averages,
    write_stitched_asaxs_outputs,
)
from aswaxs_live.reduction.xanos_export import export_analysis_h5_to_xanos_format


DEFAULT_QUEUE_PATH = PROJECT_DIR / "aswaxs_v6_queue.json"
REDUCED_FRAME_RE = re.compile(r"(?:\[(?P<label>[^\]]+)\]\s*)?Reduced frame\s+(?P<done>\d+)/(?P<total>\d+)")


class _SmoothedFrameRate:
    """Estimate recent aggregate throughput without displaying bursty ETAs."""

    def __init__(self, started_at: float, *, window_seconds: float = 30.0, alpha: float = 0.25) -> None:
        self.started_at = float(started_at)
        self.window_seconds = max(5.0, float(window_seconds))
        self.alpha = max(0.05, min(1.0, float(alpha)))
        self.samples: deque[tuple[float, int]] = deque([(self.started_at, 0)])
        self.last_completed = 0
        self.smoothed_rate = 0.0

    def update(self, completed: int, now: float | None = None) -> float:
        now = time.monotonic() if now is None else float(now)
        completed = max(0, int(completed))
        if completed < self.last_completed:
            self.samples = deque([(now, completed)])
            self.last_completed = completed
            self.smoothed_rate = 0.0
            return 0.0
        if completed > self.last_completed:
            self.samples.append((now, completed))
            self.last_completed = completed
            cutoff = now - self.window_seconds
            while len(self.samples) > 2 and self.samples[1][0] <= cutoff:
                self.samples.popleft()
            first_time, first_count = self.samples[0]
            span = now - first_time
            if span >= 1.0 and completed > first_count:
                recent_rate = (completed - first_count) / span
            else:
                elapsed = now - self.started_at
                recent_rate = completed / elapsed if elapsed > 0 else 0.0
            if recent_rate > 0:
                self.smoothed_rate = (
                    recent_rate
                    if self.smoothed_rate <= 0
                    else self.alpha * recent_rate + (1.0 - self.alpha) * self.smoothed_rate
                )
        return self.smoothed_rate


def scan_detector_files(folder: Path, pattern: str = "*.h5") -> tuple[int, int]:
    pil = folder / "Pil300K"
    eig = folder / "Eig1M"
    pil_count = sum(1 for path in pil.glob(pattern) if path.is_file()) if pil.exists() else 0
    eig_count = sum(1 for path in eig.glob(pattern) if path.is_file()) if eig.exists() else 0
    return pil_count, eig_count


def sort_h5_files(paths: list[str | Path]) -> list[Path]:
    resolved = [Path(path).expanduser() for path in paths if str(path).strip()]
    return sorted(resolved, key=_data_file_sort_key)


def _data_file_sort_key(path: Path) -> tuple[tuple[int, ...], list[object], str]:
    parts = re.split(r"(\d+)", path.name)
    numbers = tuple(int(part) for part in parts if part.isdigit())
    natural = [int(part) if part.isdigit() else part.lower() for part in parts]
    return numbers, natural, path.name.lower()


def task_to_json(task: TaskSpec) -> dict[str, object]:
    return asdict(task)


def task_from_json(payload: dict[str, object]) -> TaskSpec:
    pairs = [AsaxsPair(**item) for item in payload.get("asaxs_pairs", [])]  # type: ignore[arg-type]
    clean = dict(payload)
    clean["asaxs_pairs"] = pairs
    clean.setdefault("pil300k_files", [])
    clean.setdefault("eig1m_files", [])
    clean.setdefault("detector_mode", "both")
    clean.setdefault("reduction_mode", "asaxs")
    clean.setdefault("pil300k_monitor_key", "SPDS")
    clean.setdefault("eig1m_monitor_key", "WPDS")
    clean.setdefault("last_run_seconds", None)
    clean.setdefault("last_run_finished_at", "")
    saved_cores = int(clean.get("cores", 0) or 0)
    cpu_count = max(1, os.cpu_count() or 1)
    if saved_cores <= 1 and cpu_count > 1:
        clean["cores"] = cpu_count
    return TaskSpec(**clean)


def save_queue(path: Path, tasks: list[TaskSpec]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([task_to_json(task) for task in tasks], indent=2), encoding="utf-8")
    tmp.replace(path)


def load_queue(path: Path) -> list[TaskSpec]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [task_from_json(item) for item in payload]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Queue file is corrupt and could not be loaded: {exc}") from exc


def preflight_task(task: TaskSpec) -> tuple[bool, str]:
    if task.analysis_h5_path:
        return False, "Recovered analysis HDF5 records are preview-only; create or edit a task to run a new reduction"
    expected = task.expected_files_per_detector
    problems: list[str] = []
    output_problem = _output_inside_raw_problem(task)
    if output_problem:
        problems.append(output_problem)
    for detector, count in _active_detector_counts(task):
        if task.has_selected_files(detector):
            if count != expected:
                problems.append(_file_count_problem(detector, count, task, selected=True))
            missing = [path for path in task.detector_files(detector) if not path.exists()]
            if missing:
                problems.append(f"{detector}: {len(missing)} selected files missing")
        elif count == 0:
            problems.append(f"{detector}: no files")
        elif count < expected:
            problems.append(_file_count_problem(detector, count, task, selected=False))
        elif count % expected != 0:
            problems.append(_file_count_problem(detector, count, task, selected=False))
    poni_mask_values = [
        ("Pil300K PONI", task.pil300k_poni),
        ("Pil300K mask", task.pil300k_mask),
        ("Eig1M PONI", task.eig1m_poni),
        ("Eig1M mask", task.eig1m_mask),
    ]
    active = set(task.active_detectors())
    for label, value in poni_mask_values:
        if label.split()[0] not in active:
            continue
        problems.extend(_calibration_file_problems(label, value))
    if task.is_asaxs_mode() and not task.asaxs_pairs:
        problems.append("no ASAXS pairs")
    if not task.is_asaxs_mode() and not task.xanos_output_name():
        problems.append("SAXS XAnos output name missing; enter it in the sample/solvent output table")
    if task.is_asaxs_mode():
        problems.extend(_group_role_problems(task))
    if problems:
        return False, "; ".join(problems)
    return True, "Ready"


def _calibration_file_problems(label: str, value: str) -> list[str]:
    path_text = str(value).strip()
    if not path_text:
        return [f"{label} missing"]
    path = Path(path_text).expanduser()
    if not path.exists():
        return [f"{label} missing: {path}"]
    if path.is_dir():
        return [f"{label} is a folder, choose a file: {path}"]
    if not path.is_file():
        return [f"{label} is not a readable file: {path}"]
    if "PONI" in label and path.suffix.lower() != ".poni":
        return [f"{label} must be a .poni file: {path}"]
    return []


def _active_detector_counts(task: TaskSpec) -> list[tuple[str, int]]:
    counts = {"Pil300K": task.pil300k_count, "Eig1M": task.eig1m_count}
    return [(detector, counts[detector]) for detector in task.active_detectors()]


def _file_count_problem(detector: str, count: int, task: TaskSpec, *, selected: bool) -> str:
    expected = task.expected_files_per_detector
    source = "selected" if selected else "found"
    base = (
        f"{detector}: {source} {count} files, but sequence "
        f"{task.num_energies} energies x {task.num_groups} groups x {task.num_frames} frames expects {expected}"
    )
    per_energy_group = int(task.num_energies) * int(task.num_groups)
    if per_energy_group > 0 and count > 0 and count % per_energy_group == 0:
        inferred_frames = count // per_energy_group
        if inferred_frames != task.num_frames:
            base += f"; set Frames to {inferred_frames}"
    return base


def _group_role_problems(task: TaskSpec) -> list[str]:
    problems: list[str] = []
    role_groups = {
        "GC": task.gc_group,
        "Air": task.air_group,
        "Empty": task.empty_group,
    }
    seen: dict[int, str] = {}
    for role, group in role_groups.items():
        if group is None:
            continue
        if group < 1 or group > task.num_groups:
            problems.append(f"{role} group {group} is outside 1-{task.num_groups}")
        if group in seen:
            problems.append(f"{role} group {group} duplicates {seen[group]} group")
        else:
            seen[group] = role
    for pair in task.asaxs_pairs:
        for label, group in [("sample", pair.sample_group), ("solvent", pair.solvent_group)]:
            if group < 1 or group > task.num_groups:
                problems.append(f"ASAXS pair {pair.output_name}: {label} group {group} is outside 1-{task.num_groups}")
            if group in seen:
                problems.append(f"ASAXS pair {pair.output_name}: {label} group {group} is also {seen[group]} group")
        if pair.sample_group == pair.solvent_group:
            problems.append(f"ASAXS pair {pair.output_name}: sample and solvent are both group {pair.sample_group}")
    return problems


def run_task(
    task: TaskSpec,
    log: Callable[[str], None],
    should_stop: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> None:
    """Run one queued task using the existing reducer/stitcher pipeline."""
    should_stop = should_stop or (lambda: False)
    progress = progress or (lambda _fraction, _label: None)
    ok, message = preflight_task(task)
    if not ok:
        raise RuntimeError(message)
    task.output_path.mkdir(parents=True, exist_ok=True)
    progress(0.02, "Preparing restart")
    _prepare_restart_outputs(task, log)
    detector_label = task.detector_label
    progress(0.05, f"Reducing {detector_label}")
    log(f"{task.task_name}: reducing {detector_label}")
    _run_detector_reducers_parallel(task, log, should_stop, progress)
    progress(0.83, "Finished detector reductions")
    if should_stop():
        raise RuntimeError("Stopped by user")
    if not task.is_asaxs_mode():
        _finish_saxs_only_task(task, log, progress)
        return
    if len(task.active_detectors()) == 1:
        detector = task.active_detectors()[0]
        progress(0.94, f"Writing final ASAXS from {detector}")
        log(f"{task.task_name}: writing final ASAXS and XAnos outputs from {detector}; stitching skipped")
        _write_single_detector_asaxs_outputs(task, detector)
        progress(1.0, "Complete")
        log(f"{task.task_name}: complete -> {task.combined_h5_path()}")
        return
    progress(0.86, "Stitching detectors")
    log(f"{task.task_name}: stitching detector outputs")
    combined = update_live_stitched_averages(
        task.detector_output_dir("Pil300K"),
        task.detector_output_dir("Eig1M"),
        combined_h5_path=task.combined_h5_path(),
        sample_names=[task.task_name],
    )
    if combined is None and not task.combined_h5_path().exists():
        raise RuntimeError("No stitched output was produced.")
    if should_stop():
        raise RuntimeError("Stopped by user")
    progress(0.94, "Writing final ASAXS")
    log(f"{task.task_name}: writing final ASAXS and XAnos outputs")
    settings = StitchedAsaxsSettings(
        num_groups=task.num_groups,
        sample_group=None,
        air_group=task.air_group,
        empty_group=task.empty_group,
        water_group=None,
        gc_group=task.gc_group,
        gc_reference_file=None,
        gc_q_range=(0.03, 0.20),
        capillary_thickness=task.capillary_thickness,
        gc_thickness=task.gc_thickness,
        subtract_fluorescence=False,
        fluorescence_level=None,
        fluorescence_reference="latest",
        fluorescence_q_range=(0.8, 1.0),
        asaxs_pairs=tuple(pair.cli_value() for pair in task.asaxs_pairs),
    )
    if not write_stitched_asaxs_outputs(task.combined_h5_path(), settings):
        raise RuntimeError("Stitched ASAXS output was not written.")
    progress(1.0, "Complete")
    log(f"{task.task_name}: complete -> {task.combined_h5_path()}")


def record_task_run_timing(task: TaskSpec, elapsed_seconds: float, finished_at: str) -> None:
    """Store the latest full-task wall time in the derived task analysis HDF5."""
    path = task.combined_h5_path()
    if not path.is_file():
        return
    with h5py.File(path, "a") as handle:
        entry = handle.require_group("entry")
        timing = entry.require_group("reduction_timing")
        timing.attrs["NX_class"] = "NXcollection"
        timing.attrs["task_name"] = task.task_name
        timing.attrs["last_run_seconds"] = float(elapsed_seconds)
        timing.attrs["last_run_finished_at"] = finished_at
        timing.attrs["scope"] = "full task: preparation, detector reduction, QC, averaging, finalization, stitching, and export"


def _finish_saxs_only_task(task: TaskSpec, log: Callable[[str], None], progress: Callable[[float, str], None]) -> None:
    active = task.active_detectors()
    if len(active) == 1:
        detector = active[0]
        detector_h5 = task.detector_analysis_h5_path(detector)
        if not detector_h5.is_file():
            raise RuntimeError(f"No {detector} analysis HDF5 was produced.")
        combined_h5 = task.combined_h5_path()
        if combined_h5.exists():
            combined_h5.unlink()
        shutil.copy2(detector_h5, combined_h5)
        written = export_analysis_h5_to_xanos_format(combined_h5, saxs_output_name=task.xanos_output_name(), force_saxs=True)
        if not written:
            raise RuntimeError("No SAXS XAnos files were written.")
        progress(1.0, "Complete")
        log(f"{task.task_name}: SAXS-only {detector} reduction complete; XAnos files written -> {_xanos_written_dir(written)}")
        return

    progress(0.86, "Stitching SAXS/WAXS detector averages")
    log(f"{task.task_name}: stitching detector outputs for SAXS-only reduction")
    combined = update_live_stitched_averages(
        task.detector_output_dir("Pil300K"),
        task.detector_output_dir("Eig1M"),
        combined_h5_path=task.combined_h5_path(),
        sample_names=[task.task_name],
    )
    if combined is None and not task.combined_h5_path().exists():
        raise RuntimeError("No stitched SAXS/WAXS output was produced.")
    written = export_analysis_h5_to_xanos_format(task.combined_h5_path(), saxs_output_name=task.xanos_output_name(), force_saxs=True)
    if not written:
        raise RuntimeError("No stitched SAXS XAnos files were written.")
    progress(1.0, "Complete")
    log(f"{task.task_name}: SAXS-only reduction complete; XAnos files written -> {_xanos_written_dir(written)}")


def _xanos_written_dir(written: list[Path]) -> Path | str:
    for path in written:
        if path.suffix.lower() == ".dat":
            return path.parent
    return written[0].parent if written else "unknown"


def _output_inside_raw_problem(task: TaskSpec) -> str | None:
    try:
        output = task.output_path
    except OSError:
        return "invalid output folder"
    for root in task.raw_source_roots():
        try:
            output.relative_to(root)
        except ValueError:
            continue
        return f"output folder is inside raw data folder: {output}"
    return None


def _prepare_restart_outputs(task: TaskSpec, log: Callable[[str], None]) -> None:
    """Clear queue-level derived outputs before a deliberate GUI restart run."""
    removed_root = _clear_task_output_records(task)
    if removed_root:
        log(f"{task.task_name}: restart removed {removed_root} previous task-level output record(s)")
    for detector in ("Pil300K", "Eig1M"):
        removed = _clear_detector_output_records(task, detector)
        if removed:
            log(f"{task.task_name}: restart removed {removed} previous {detector} output record(s)")


def _clear_detector_output_records(task: TaskSpec, detector: str) -> int:
    """Remove current-task detector outputs so stitching cannot pick up stale HDF5."""
    output_dir = task.detector_output_dir(detector)
    if not output_dir.exists():
        return 0
    removed = 0
    current_task_files = [
        task.detector_analysis_h5_path(detector),
        output_dir / f"{safe_name(task.task_name)}_{detector}_analysis.hdf5",
        output_dir / f"{safe_name(task.task_name)}_{detector}_selected_manifest.csv",
    ]
    for path in current_task_files:
        if path.is_file():
            path.unlink()
            removed += 1
    shared_run_files = [
        "live_events.jsonl",
        "live_replay_manifest.csv",
        "live_sequence_manifest.csv",
        "group_summary.csv",
    ]
    for name in shared_run_files:
        path = output_dir / name
        if path.is_file():
            path.unlink()
            removed += 1
    return removed


def _write_single_detector_asaxs_outputs(task: TaskSpec, detector: str) -> None:
    detector_h5 = task.detector_analysis_h5_path(detector)
    if not detector_h5.is_file():
        raise RuntimeError(f"No {detector} analysis HDF5 was produced.")
    task.output_path.mkdir(parents=True, exist_ok=True)
    combined_h5 = task.combined_h5_path()
    if combined_h5.exists():
        combined_h5.unlink()
    shutil.copy2(detector_h5, combined_h5)
    averages = read_detector_group_averages(combined_h5, detector)
    if not averages:
        raise RuntimeError(f"No {detector} group-average curves were found for final ASAXS output.")

    from aswaxs_live.reduction.aswaxs_sequence import (  # pylint: disable=import-outside-toplevel
        _write_legacy_final_group,
        _write_named_asaxs_outputs,
    )
    from aswaxs_live.reduction.live import build_final_outputs_for_h5, load_reduction_core  # pylint: disable=import-outside-toplevel

    args = SimpleNamespace(
        sample_group=None,
        air_group=task.air_group,
        empty_group=task.empty_group,
        water_group=None,
        gc_group=task.gc_group,
        gc_reference_file=None,
        gc_q_range=(0.03, 0.20),
        capillary_thickness=task.capillary_thickness,
        gc_thickness=task.gc_thickness,
        subtract_fluorescence=False,
        fluorescence_level=None,
        fluorescence_reference="latest",
        fluorescence_q_range=(0.8, 1.0),
        asaxs_pair=[pair.cli_value() for pair in task.asaxs_pairs],
        asaxs_extraction_plan=None,
        asaxs_output_name="sample",
        write_text_output=False,
    )
    outputs = build_final_outputs_for_h5(load_reduction_core(), averages, args, task.output_path)
    if not outputs:
        raise RuntimeError(f"No final ASAXS output was produced for {detector}.")
    _write_named_asaxs_outputs(combined_h5, outputs)
    _write_legacy_final_group(combined_h5, outputs)
    written = export_analysis_h5_to_xanos_format(combined_h5)
    if not written:
        raise RuntimeError("No XAnos files were written.")


def _clear_task_output_records(task: TaskSpec) -> int:
    """Remove top-level derived task records from the task output folder."""
    output_dir = task.output_path
    if not output_dir.exists():
        return 0
    removed = 0
    for path in (task.combined_h5_path(), output_dir / f"{safe_name(task.task_name)}_analysis.hdf5"):
        if path.is_file():
            path.unlink()
            removed += 1
    _ensure_current_pair_xanos_outputs_writable(output_dir, task)
    return removed


def _ensure_current_pair_xanos_outputs_writable(output_dir: Path, task: TaskSpec) -> None:
    """Verify existing XAnos .dat exports can be overwritten on restart."""
    xanos_dir = output_dir / "XAnos format"
    if not xanos_dir.exists() or not xanos_dir.is_dir():
        return
    if task.is_asaxs_mode():
        output_names = {_xanos_output_folder_name(pair.output_name) for pair in task.asaxs_pairs}
    else:
        output_names = {_xanos_output_folder_name(task.xanos_output_name() or task.task_name)}
    if not output_names:
        return
    for output_name in sorted(output_names):
        folder = xanos_dir / output_name
        if folder.exists() and folder.is_dir():
            _check_dat_files_writable(folder)


def _check_dat_files_writable(folder: Path) -> None:
    locked = [path for path in folder.rglob("*.dat") if path.is_file() and not os.access(path, os.W_OK)]
    if locked:
        examples = ", ".join(str(path) for path in locked[:3])
        suffix = "" if len(locked) <= 3 else f", and {len(locked) - 3} more"
        raise PermissionError(f"Cannot overwrite existing XAnos .dat file(s): {examples}{suffix}")


def _xanos_output_folder_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name).strip())
    return cleaned.strip("._") or "sample"


def _run_detector_reducers_parallel(
    task: TaskSpec,
    log: Callable[[str], None],
    should_stop: Callable[[], bool],
    progress: Callable[[float, str], None],
) -> None:
    all_detector_specs = {
        "Pil300K": ("Pil300K", task.pil300k_poni, task.pil300k_mask),
        "Eig1M": ("Eig1M", task.eig1m_poni, task.eig1m_mask),
    }
    detector_specs = [all_detector_specs[detector] for detector in task.active_detectors()]
    total_jobs = max(1, min(int(task.cores), os.cpu_count() or 1))
    jobs_per_detector = max(1, total_jobs // len(detector_specs))
    processes: dict[str, subprocess.Popen[str]] = {}
    output_lines: dict[str, list[str]] = {}
    reader_threads: dict[str, threading.Thread] = {}
    stopped = False
    try:
        monitors: dict[str, _EventLogFrameMonitor] = {}
        detector_fractions: dict[str, float] = {detector: 0.0 for detector, _poni, _mask in detector_specs}
        reduction_started_at = time.monotonic()
        frame_rate = _SmoothedFrameRate(reduction_started_at)
        last_finalize_progress = 0.0
        for detector, poni, mask in detector_specs:
            if should_stop():
                raise RuntimeError("Stopped by user")
            calibration_problems = [
                *_calibration_file_problems(f"{detector} PONI", poni),
                *_calibration_file_problems(f"{detector} mask", mask),
            ]
            if calibration_problems:
                raise RuntimeError("; ".join(calibration_problems))
            output_dir = task.detector_output_dir(detector)
            output_dir.mkdir(parents=True, exist_ok=True)
            monitors[detector] = _EventLogFrameMonitor(
                output_dir / "live_events.jsonl",
                task.expected_files_per_detector,
                detector,
            )
            cmd = _detector_batch_command(task, detector, poni, mask, output_dir, jobs_per_detector)
            log(f"{task.task_name}: starting {detector} batch reducer with {jobs_per_detector} worker(s)")
            processes[detector] = subprocess.Popen(
                cmd,
                cwd=PROJECT_DIR,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_subprocess_env(),
            )
            output_lines[detector] = []
            reader = threading.Thread(
                target=_collect_subprocess_output,
                args=(processes[detector], output_lines[detector], monitors[detector]),
                daemon=True,
            )
            reader.start()
            reader_threads[detector] = reader
        finished: set[str] = set()
        outputs: dict[str, str] = {}
        while len(finished) < len(processes):
            if should_stop():
                stopped = True
                for process in processes.values():
                    _terminate_process_tree(process)
            for detector, process in list(processes.items()):
                if detector in finished or process.poll() is None:
                    continue
                process.wait()
                if detector in reader_threads:
                    reader_threads[detector].join(timeout=2)
                outputs[detector] = "".join(output_lines.get(detector, []))
                finished.add(detector)
                detector_fractions[detector] = 1.0
                if detector in monitors:
                    monitors[detector].reduced_frames = monitors[detector].expected_frames
                total_fraction = sum(detector_fractions.values()) / max(1, len(detector_fractions))
                label = _frame_progress_label(monitors, frame_rate, prefix=f"Finished {detector}")
                detector_progress = 0.05 + 0.75 * total_fraction
                if all(fraction >= 1.0 for fraction in detector_fractions.values()):
                    detector_progress = max(detector_progress, 0.82)
                progress(detector_progress, label)
            saw_frame_update = False
            if not stopped:
                for detector, monitor in monitors.items():
                    if detector in finished:
                        continue
                    update = monitor.poll()
                    if update is None:
                        continue
                    detector_fraction, _label = update
                    detector_fractions[detector] = detector_fraction
                    saw_frame_update = True
                if saw_frame_update:
                    total_fraction = sum(detector_fractions.values()) / max(1, len(detector_fractions))
                    progress(0.05 + 0.75 * total_fraction, _frame_progress_label(monitors, frame_rate))
            all_frames_reduced = all(fraction >= 1.0 for fraction in detector_fractions.values())
            if all_frames_reduced and len(finished) < len(processes):
                now = time.monotonic()
                if now - last_finalize_progress >= 5.0:
                    last_finalize_progress = now
                    running = [detector for detector in processes if detector not in finished]
                    progress(
                        0.82,
                        _frame_progress_label(
                            monitors,
                            frame_rate,
                            prefix=f"Finalizing detector output ({', '.join(running)})",
                        ),
                    )
            if stopped:
                break
            try:
                time.sleep(0.2)
            except KeyboardInterrupt:
                stopped = True
                for process in processes.values():
                    _terminate_process_tree(process)
        for detector, process in processes.items():
            if detector not in outputs:
                process.wait()
                if detector in reader_threads:
                    reader_threads[detector].join(timeout=2)
                outputs[detector] = "".join(output_lines.get(detector, []))
            for line in outputs[detector].splitlines()[-60:]:
                log(f"  [{detector}] {line}")
            if stopped:
                continue
            if process.returncode != 0:
                raise RuntimeError(_subprocess_failure_message(detector, process.returncode, outputs[detector]))
    except Exception:
        for process in processes.values():
            _terminate_process_tree(process)
        raise
    if stopped:
        raise RuntimeError("Stopped by user")


def _collect_subprocess_output(process: subprocess.Popen[str], lines: list[str], monitor: "_EventLogFrameMonitor") -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        lines.append(line)
        monitor.observe_output_line(line)


def _subprocess_failure_message(detector: str, returncode: int | None, output: str) -> str:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    tail = "\n".join(lines[-120:])
    if not tail:
        tail = "(no reducer output captured)"
    return (
        f"{detector} reducer failed with exit code {returncode}.\n\n"
        f"Last reducer output:\n{tail}"
    )


def _frame_progress_label(
    monitors: dict[str, "_EventLogFrameMonitor"],
    rate_estimator: _SmoothedFrameRate,
    *,
    prefix: str = "Reducing frames",
) -> str:
    reduced = sum(min(monitor.reduced_frames, monitor.expected_frames) for monitor in monitors.values())
    expected = sum(monitor.expected_frames for monitor in monitors.values())
    rate = rate_estimator.update(reduced)
    eta = "calculating"
    if rate > 0 and reduced < expected:
        eta = _format_duration((expected - reduced) / rate)
    elif expected > 0 and reduced >= expected:
        eta = "0s"
    detector_parts = [
        f"{detector} {min(monitor.reduced_frames, monitor.expected_frames)}/{monitor.expected_frames}"
        for detector, monitor in monitors.items()
    ]
    rate_text = f", {rate:.1f} frames/s" if rate > 0 else ""
    detector_text = f" ({'; '.join(detector_parts)})" if detector_parts else ""
    return f"{prefix}: {reduced}/{expected} frames, ETA {eta}{rate_text}{detector_text}"


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _detector_batch_command(
    task: TaskSpec,
    detector: str,
    poni: str,
    mask: str,
    output_dir: Path,
    jobs: int,
) -> list[str]:
    source_args = _detector_batch_source_args(task, detector, output_dir)
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "aswaxs_live.reduction.aswaxs_sequence",
        "--num-energies",
        str(task.num_energies),
        "--num-groups",
        str(task.num_groups),
        "--num-frames",
        str(task.num_frames),
        "--no-prompt",
        "--output-dir",
        str(output_dir),
        "--detector",
        detector,
        "--poni",
        str(poni),
        "--mask",
        str(mask),
        "--dataset-path",
        task.dataset_path,
        "--monitor-key",
        _detector_monitor_key(task, detector),
        "--npt",
        str(task.npt),
        "--jobs",
        str(max(1, int(jobs))),
        "--unit",
        task.unit,
        "--analysis-h5",
        str(task.detector_analysis_h5_path(detector)),
    ]
    cmd[4:4] = source_args
    return cmd


def _detector_monitor_key(task: TaskSpec, detector: str) -> str:
    if detector == "Pil300K":
        return str(task.pil300k_monitor_key or "SPDS").strip() or "SPDS"
    if detector == "Eig1M":
        return str(task.eig1m_monitor_key or "WPDS").strip() or "WPDS"
    return "SPDS"


class _EventLogFrameMonitor:
    def __init__(self, event_log_path: Path, expected_frames: int, detector: str) -> None:
        self.event_log_path = event_log_path
        self.expected_frames = max(1, int(expected_frames))
        self.detector = detector
        self.offset = 0
        self.reduced_frames = 0
        self.worker_frames: dict[str, int] = {}
        self._changed = False
        self._lock = threading.Lock()

    def observe_output_line(self, line: str) -> None:
        match = REDUCED_FRAME_RE.search(line)
        if match is None:
            return
        label = match.group("label") or "main"
        done = int(match.group("done"))
        with self._lock:
            if done <= self.worker_frames.get(label, 0):
                return
            self.worker_frames[label] = done
            self.reduced_frames = min(self.expected_frames, sum(self.worker_frames.values()))
            self._changed = True

    def poll(self) -> tuple[float, str] | None:
        with self._lock:
            if self._changed:
                self._changed = False
                fraction = min(1.0, self.reduced_frames / self.expected_frames)
                return fraction, f"Reducing {self.detector}: {self.reduced_frames}/{self.expected_frames} frames"
        if not self.event_log_path.exists():
            return None
        try:
            with self.event_log_path.open("r", encoding="utf-8") as handle:
                handle.seek(self.offset)
                lines = handle.readlines()
                self.offset = handle.tell()
        except OSError:
            return None
        changed = False
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "frame_reduced_1d":
                with self._lock:
                    self.reduced_frames += 1
                changed = True
        if not changed:
            return None
        with self._lock:
            fraction = min(1.0, self.reduced_frames / self.expected_frames)
            reduced = self.reduced_frames
        return fraction, f"Reducing {self.detector}: {reduced}/{self.expected_frames} frames"


def _detector_source_args(task: TaskSpec, detector: str, output_dir: Path) -> list[str]:
    selected = sort_h5_files(task.detector_files(detector))
    if selected:
        manifest = _write_selected_file_manifest(task, detector, selected, output_dir)
        return ["--manifest", str(manifest)]
    return [
        "--watch-dir",
        str(task.detector_dir(detector)),
        "--pattern",
        task.pattern,
        "--once",
    ]


def _detector_batch_source_args(task: TaskSpec, detector: str, output_dir: Path) -> list[str]:
    selected = sort_h5_files(task.detector_files(detector))
    if selected:
        manifest = _write_selected_file_manifest(task, detector, selected, output_dir)
        return ["--manifest", str(manifest)]
    return [
        "--data-dir",
        str(task.detector_dir(detector)),
        "--pattern",
        task.pattern,
        "--allow-extra-files",
        "--resume-mode",
        "first",
    ]


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_dir = str(PROJECT_DIR / "src")
    existing = env.get("PYTHONPATH", "")
    paths = [src_dir]
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _write_selected_file_manifest(task: TaskSpec, detector: str, selected: list[Path], output_dir: Path) -> Path:
    from aswaxs_live.reduction.sequence import build_sequence_map, write_manifest  # pylint: disable=import-outside-toplevel

    expected = task.expected_files_per_detector
    if len(selected) != expected:
        raise RuntimeError(f"{detector}: selected {len(selected)} HDF5 files, expected {expected}.")
    manifest_path = output_dir / f"{safe_name(task.task_name)}_{detector}_selected_manifest.csv"
    items = build_sequence_map(selected, task.num_energies, task.num_groups, task.num_frames)
    return write_manifest(items, manifest_path)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    else:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
