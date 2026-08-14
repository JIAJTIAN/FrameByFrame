"""Bridge Bluesky/Kafka messages into v3 measurement reduction jobs.

The reducer consumes a simple JSONL job queue. This module is the beamline-side
adapter: it accepts a Bluesky/Kafka payload, normalizes the fields the reducer
needs, and appends a ``measurement_done`` job. The optional live Kafka consumer
uses ``bluesky-kafka`` only when that package is installed.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Callable

from aswaxs_live.workflows.bluesky_queue import append_measurement_done_message


REQUIRED_FIELDS = ("data_dir",)
DEFAULT_DETECTORS = ("Pil300K", "Eig1M")


def _default_log(message: str) -> None:
    """Print bridge messages immediately when running under the GUI."""
    print(message, flush=True)


def normalize_measurement_done_payload(
    payload: dict[str, Any],
    *,
    data_root: str | Path | None = None,
    output_root: str | Path | None = None,
    detectors: list[str] | tuple[str, ...] = DEFAULT_DETECTORS,
) -> list[dict[str, Any]]:
    """Return a reducer job dict when a payload describes a finished measurement."""
    event = str(payload.get("event") or payload.get("name") or "").strip()
    if event in {"start", "sample_active", "sample_started"} or _looks_like_plan_item(payload):
        return _jobs_from_sample_document(payload, data_root=data_root, output_root=output_root, detectors=detectors)
    if event and event != "measurement_done":
        return []
    if not event and not any(key in payload for key in REQUIRED_FIELDS):
        return []
    data_dir = payload.get("data_dir") or payload.get("directory") or payload.get("path")
    if not data_dir:
        return []
    output_dir = payload.get("output_dir")
    if output_dir is None and output_root is not None:
        output_dir = _output_dir_for_data_dir(
            Path(data_dir).expanduser(),
            data_root=Path(data_root).expanduser() if data_root is not None else None,
            output_root=Path(output_root).expanduser(),
            detector=payload.get("detector"),
        )
    return [
        {
            "event": payload.get("_queue_event") or payload.get("event") or "measurement_done",
            "uid": _first_metadata_value(payload, ("uid", "run_uid", "item_uid")),
            "scan_id": _scan_id_from_payload(payload),
            "sample_name": _sample_name_from_payload(payload),
            "detector": payload.get("detector"),
            "analysis_mode": payload.get("analysis_mode") or "saxs",
                "measurement_type": payload.get("measurement_type") or "normal_saxs",
                "data_dir": data_dir,
                "output_dir": output_dir,
                "num_energies": _num_energies_from_payload(payload),
                "num_groups": _num_groups_from_payload(payload),
                "num_frames": _num_frames_from_payload(payload),
            }
        ]


def append_jobs_from_payload(
    queue_path: str | Path,
    payload: dict[str, Any],
    *,
    data_root: str | Path | None = None,
    output_root: str | Path | None = None,
    detectors: list[str] | tuple[str, ...] = DEFAULT_DETECTORS,
) -> list[Path]:
    """Append normalized measurement jobs; return an empty list for unrelated payloads."""
    paths: list[Path] = []
    for job in normalize_measurement_done_payload(
        payload,
        data_root=data_root,
        output_root=output_root,
        detectors=detectors,
    ):
        paths.append(append_measurement_done_message(queue_path, **job))
    return paths


def append_job_from_payload(queue_path: str | Path, payload: dict[str, Any]) -> Path | None:
    """Backward-compatible single-job helper."""
    paths = append_jobs_from_payload(queue_path, payload)
    return paths[0] if paths else None


def _jobs_from_sample_document(
    payload: dict[str, Any],
    *,
    data_root: str | Path | None,
    output_root: str | Path | None,
    detectors: list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Build detector-directory jobs from a Bluesky start/sample document."""
    # For live Kafka, the beamline start document is the freshest source of the
    # acquisition root. The GUI data root remains a fallback for older/debug
    # messages that do not carry expDir.
    root_value = _exp_dir_from_payload(payload) or data_root
    if root_value is None:
        return []
    sample_name = _sample_name_from_payload(payload)
    root = Path(root_value).expanduser()
    data_root_path = Path(data_root).expanduser() if data_root is not None else None
    output_root_path = Path(output_root).expanduser() if output_root is not None else None
    scan_id = _scan_id_from_payload(payload)
    queue_event = str(payload.get("_queue_event") or "sample_active")
    output_sample_name = sample_name or (f"scan_{scan_id}" if scan_id is not None else root.name)
    sample_root = _resolve_sample_root(root, sample_name, data_root_path=data_root_path)
    output_sample_root = (
        _output_root_for_sample_root(sample_root, data_root=data_root_path or root, output_root=output_root_path)
        if output_root_path is not None
        else None
    )
    jobs: list[dict[str, Any]] = []
    for detector in detectors:
        detector = str(detector).strip()
        if not detector:
            continue
        data_dir = sample_root / detector
        output_dir = output_sample_root / detector if output_sample_root is not None else None
        jobs.append(
            {
                "event": queue_event,
                "uid": _first_metadata_value(payload, ("uid", "run_uid", "item_uid")),
                "scan_id": scan_id,
                "sample_name": output_sample_name,
                "detector": detector,
                "analysis_mode": payload.get("analysis_mode") or "saxs",
                "measurement_type": payload.get("measurement_type") or "sample_active",
                "data_dir": data_dir,
                "output_dir": output_dir,
                "num_energies": _num_energies_from_payload(payload),
                "num_groups": _num_groups_from_payload(payload),
                "num_frames": _num_frames_from_payload(payload),
            }
        )
    return jobs


def _resolve_sample_root(root: Path, sample_name: str | None, *, data_root_path: Path | None = None) -> Path:
    """Find the raw sample folder below the Kafka/GUI main folder.

    Online mode mirrors the GUI sample-list behavior: sample output is based on
    the raw sample folder's relative path from the selected main folder, and
    previously generated ``Extracted`` folders are never treated as raw input.
    """
    if not sample_name:
        return root
    target_fragments = _sample_path_fragments(sample_name)
    search_roots = _unique_paths([root, data_root_path])
    for search_root in search_roots:
        direct = search_root.joinpath(*target_fragments)
        if direct.exists():
            return direct
    target_name = target_fragments[-1]
    matches: list[Path] = []
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for path in search_root.rglob(target_name):
            if not path.is_dir():
                continue
            if _is_inside_excluded_folder(path, search_root):
                continue
            matches.append(path)
    if matches:
        matches.sort(key=_sample_folder_score, reverse=True)
        return matches[0]
    return root.joinpath(*target_fragments)


def _output_root_for_sample_root(sample_root: Path, *, data_root: Path, output_root: Path) -> Path:
    """Mirror a raw sample root under the analysis output root."""
    try:
        relative = sample_root.resolve().relative_to(data_root.resolve())
    except (OSError, ValueError):
        try:
            relative = sample_root.relative_to(data_root)
        except ValueError:
            relative = Path(sample_root.name)
    if _has_excluded_part(relative):
        relative = Path(sample_root.name)
    return output_root / relative


def _output_dir_for_data_dir(
    data_dir: Path,
    *,
    data_root: Path | None,
    output_root: Path,
    detector: Any,
) -> Path:
    """Build an output detector directory for direct measurement_done messages."""
    detector_name = str(detector).strip() if detector is not None else data_dir.name
    sample_root = data_dir.parent if detector_name and data_dir.name == detector_name else data_dir
    output_sample_root = _output_root_for_sample_root(sample_root, data_root=data_root or sample_root.parent, output_root=output_root)
    return output_sample_root / detector_name if detector_name else output_sample_root


def _sample_path_fragments(sample_name: str) -> list[str]:
    parts = [part for part in re.split(r"[\\/]+", sample_name.strip()) if part]
    return [_safe_path_fragment(part) for part in parts] or ["sample"]


def _unique_paths(paths: list[Path | None]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _is_inside_excluded_folder(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return _has_excluded_part(relative)


def _has_excluded_part(path: Path) -> bool:
    excluded = {"extracted", "_live_status"}
    return any(part.lower() in excluded for part in path.parts)


def _sample_folder_score(path: Path) -> tuple[int, int]:
    detector_names = {"pil300k", "eig1m", "saxs", "waxs", "spds", "wpds"}
    try:
        child_names = {child.name.lower() for child in path.iterdir() if child.is_dir()}
    except OSError:
        child_names = set()
    detector_score = len(child_names & detector_names)
    try:
        h5_score = sum(1 for _ in path.rglob("*.h5"))
    except OSError:
        h5_score = 0
    return detector_score, h5_score


def _sample_name_from_payload(payload: dict[str, Any]) -> str | None:
    return _first_metadata_value(
        payload,
        ("sampleName", "sample_name", "sample", "sample_id", "sampleDescription", "sample_description"),
    ) or _sample_name_from_args_path(payload)


def _scan_id_from_payload(payload: dict[str, Any]) -> Any:
    return _first_raw_metadata_value(payload, ("scan_id", "scanNum", "scan_num"))


def _exp_dir_from_payload(payload: dict[str, Any]) -> str | None:
    return _first_metadata_value(payload, ("expDir", "exp_dir", "experiment_dir", "data_root"))


def _num_groups_from_payload(payload: dict[str, Any]) -> int | None:
    """Infer groups per energy from Bluesky list_scan metadata."""
    return _int_from_payload(payload, ("num_groups", "groups_per_energy", "group_count", "num_points"))


def _num_frames_from_payload(payload: dict[str, Any]) -> int | None:
    """Read frames per group/point from common Bluesky or detector metadata keys."""
    return _int_from_payload(
        payload,
        (
            "num_frames",
            "frames_per_group",
            "frames_per_point",
            "num_frames_per_point",
            "frame_count",
            "numImages",
            "num_images",
            "images_per_set",
            "frames",
        ),
    )


def _num_energies_from_payload(payload: dict[str, Any]) -> int | None:
    """Read energy-batch count from common metadata keys."""
    return _int_from_payload(payload, ("num_energies", "num_energy", "energies", "energy_count", "num_energy_points"))


def _int_from_payload(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    value = _first_raw_metadata_value(payload, keys)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_metadata_value(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value = _first_raw_metadata_value(payload, keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_raw_metadata_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for source in _metadata_sources(payload):
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip():
                return value
    return None


def _metadata_sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [payload]
    for container_key in ("md", "metadata", "plan_args", "kwargs"):
        value = payload.get(container_key)
        if isinstance(value, dict):
            sources.append(value)
            nested_md = value.get("md")
            if isinstance(nested_md, dict):
                sources.append(nested_md)
    return sources


def _sample_name_from_args_path(payload: dict[str, Any]) -> str | None:
    """Infer the sample folder from a plan args path such as .../2026Jun/SAXS_J/positions.csv."""
    arg_lists: list[list[Any]] = []
    for source in (payload, payload.get("plan_args"), payload.get("plan_pattern_args"), payload.get("kwargs")):
        if isinstance(source, dict) and isinstance(source.get("args"), list):
            arg_lists.append(source["args"])
    for args in arg_lists:
        for value in reversed(args):
            if not isinstance(value, str):
                continue
            path = Path(value)
            if path.suffix.lower() in {".csv", ".txt", ".xlsx", ".xls"} and path.parent.name:
                return path.parent.name
    return None


def _looks_like_plan_item(payload: dict[str, Any]) -> bool:
    """Return True for Bluesky Queue Server plan-item metadata messages."""
    if str(payload.get("item_type") or "").strip() == "plan":
        return True
    kwargs = payload.get("kwargs")
    return isinstance(kwargs, dict) and isinstance(kwargs.get("md"), dict)


def _safe_path_fragment(text: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(text).strip())
    cleaned = cleaned.strip("._")
    return cleaned or "unknown_sample"


def parse_kafka_message_line(line: str) -> dict[str, Any] | None:
    """Parse saved Kafka/debug output lines for local bridge tests.

    The beamline printer may save either JSON dictionaries or Bluesky reprs
    like ``Start({...})``.  The live Kafka bridge receives ``name, doc`` pairs
    directly, but this parser lets us replay pasted debug text into the same
    queue-normalization path.
    """
    text = line.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    for prefix, name in (("Start(", "start"), ("Descriptor(", "descriptor"), ("Event(", "event"), ("Stop(", "stop")):
        if text.startswith(prefix) and text.endswith(")"):
            try:
                value = ast.literal_eval(text[len(prefix) : -1])
            except (SyntaxError, ValueError):
                return _parse_start_repr_fallback(text) if prefix == "Start(" else None
            if isinstance(value, dict):
                value.setdefault("name", name)
                return value
    return None


def _parse_start_repr_fallback(text: str) -> dict[str, Any] | None:
    """Extract reducer-relevant fields from non-literal Bluesky Start repr text."""
    if not text.startswith("Start("):
        return None
    payload: dict[str, Any] = {"name": "start"}
    patterns = {
        "uid": r"'uid':\s*'([^']+)'",
        "scan_id": r"'scan_id':\s*([0-9]+)",
        "num_points": r"'num_points':\s*([0-9]+)",
        "num_intervals": r"'num_intervals':\s*([0-9]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if not match:
            continue
        value: str | int = match.group(1)
        if key != "uid":
            value = int(value)
        payload[key] = value
    csv_matches = re.findall(r"'([^']+\.(?:csv|txt|xlsx|xls))'", text, flags=re.IGNORECASE)
    if csv_matches:
        csv_path = csv_matches[-1]
        payload["args"] = [csv_path]
    detector_match = re.search(r"'detectors':\s*\[([^\]]+)\]", text)
    if detector_match:
        payload["detectors"] = re.findall(r"'([^']+)'", detector_match.group(1))
    return payload if len(payload) > 1 else None


def run_bluesky_kafka_bridge(
    *,
    bootstrap_servers: str,
    topics: list[str],
    queue_path: str | Path,
    data_root: str | Path | None = None,
    output_root: str | Path | None = None,
    detectors: list[str] | tuple[str, ...] = DEFAULT_DETECTORS,
    group_id: str = "aswaxs-v3-reduction-bridge",
    log: Callable[[str], None] = _default_log,
) -> None:
    """Consume Bluesky Kafka documents and append measurement jobs.

    This accepts two beamline patterns:

    - a direct ``measurement_done`` payload with ``data_dir``
    - a normal Bluesky ``start`` document with ``sampleName``/``sample_name``;
      in this case ``data_root / sample_name / detector`` is queued. When
      ``output_root`` is supplied, ``output_root / sample_name / detector`` is
      also written into the queue job.
    """
    try:
        from bluesky_kafka import RemoteDispatcher
    except ImportError as exc:
        raise RuntimeError(
            "bluesky-kafka is required for live Kafka consumption. Install it in "
            "the active Python environment with: python -m pip install bluesky-kafka. "
            "You can still test locally without Kafka using scripts/write_measurement_done.py."
        ) from exc

    try:
        dispatcher = RemoteDispatcher(
            topics,
            bootstrap_servers,
            group_id,
            {"auto.offset.reset": "latest"},
        )
    except TypeError:
        dispatcher = RemoteDispatcher(
            topics=topics,
            bootstrap_servers=bootstrap_servers,
            group_id=group_id,
        )

    start_docs_by_uid: dict[str, dict[str, Any]] = {}
    # Maps descriptor uid → run_start uid so events can be associated with
    # their parent start document even when the run uid is not in the event.
    descriptor_uid_to_run_uid: dict[str, str] = {}

    def consume_document(name: str, doc: dict[str, Any]) -> None:
        payload = dict(doc)
        payload.setdefault("name", name)
        document_name = str(name or payload.get("name") or "").strip()
        sample_name = _sample_name_from_payload(payload) or "unknown"
        scan_id = _scan_id_from_payload(payload)
        if document_name == "start":
            uid = _first_metadata_value(payload, ("uid", "run_uid", "item_uid"))
            if uid:
                start_docs_by_uid[uid] = payload
            log(f"Kafka start cached: sample={sample_name}, scan_id={scan_id}, uid={uid or 'unknown'}")
            start_payload = dict(payload)
            start_payload["_queue_event"] = "sample_active"
            paths = append_jobs_from_payload(
                queue_path,
                start_payload,
                data_root=data_root,
                output_root=output_root,
                detectors=detectors,
            )
            for path in paths:
                log(f"Queued active reduction watch from Kafka start: {path}")
            return
        if document_name == "descriptor":
            desc_uid = _first_metadata_value(payload, ("uid",))
            run_start_uid = _first_metadata_value(payload, ("run_start",))
            if desc_uid and run_start_uid:
                descriptor_uid_to_run_uid[desc_uid] = run_start_uid
            if not any(key in payload for key in REQUIRED_FIELDS):
                return
        elif document_name in {"event", "event_page"}:
            # Merge parent start-document fields so sample name, uid, and
            # scan_id are available even when the event only carries data_dir.
            desc_uid = _first_metadata_value(payload, ("descriptor",))
            run_uid = descriptor_uid_to_run_uid.get(desc_uid or "")
            if run_uid is None:
                run_uid = _first_metadata_value(payload, ("run_start",))
            start_payload = start_docs_by_uid.get(run_uid or "")
            if start_payload is not None:
                # Start-doc fields fill gaps; event-level fields take precedence.
                merged = dict(start_payload)
                merged.update(payload)
                payload = merged
                sample_name = _sample_name_from_payload(payload) or sample_name
                scan_id = _scan_id_from_payload(payload) or scan_id
            if not any(key in payload for key in REQUIRED_FIELDS):
                return
            log(
                f"Kafka {document_name}: run_uid={run_uid or 'unknown'}, "
                f"sample={sample_name}, scan_id={scan_id}"
            )
        elif document_name == "stop":
            run_uid = _first_metadata_value(payload, ("run_start", "uid", "run_uid"))
            start_payload = start_docs_by_uid.pop(run_uid, None) if run_uid else None
            # Clean up descriptor cache entries that belonged to this run.
            if run_uid:
                stale = [d for d, r in descriptor_uid_to_run_uid.items() if r == run_uid]
                for d in stale:
                    descriptor_uid_to_run_uid.pop(d, None)
            if start_payload is None:
                log(f"Kafka stop skipped: no cached start document for run_start={run_uid or 'unknown'}")
                return
            queue_payload = dict(start_payload)
            queue_payload["name"] = "sample_active"
            queue_payload["_queue_event"] = "measurement_done"
            queue_payload["measurement_type"] = payload.get("exit_status") or payload.get("reason") or "bluesky_stop"
            if payload.get("num_events") is not None:
                queue_payload["num_events"] = payload.get("num_events")
            sample_name = _sample_name_from_payload(queue_payload) or "unknown"
            scan_id = _scan_id_from_payload(queue_payload)
            log(f"Kafka stop received; queueing reduction: sample={sample_name}, scan_id={scan_id}, uid={run_uid or 'unknown'}")
            payload = queue_payload
        elif document_name in {"datum", "resource"}:
            if not any(key in payload for key in REQUIRED_FIELDS):
                return
            log(f"Kafka data document contains direct job fields: name={name}, sample={sample_name}, scan_id={scan_id}")
        else:
            log(f"Kafka document received: name={name}, sample={sample_name}, scan_id={scan_id}")
        paths = append_jobs_from_payload(
            queue_path,
            payload,
            data_root=data_root,
            output_root=output_root,
            detectors=detectors,
        )
        if not paths:
            keys = ", ".join(sorted(str(key) for key in payload.keys())[:12])
            log(f"Kafka document skipped: name={name}, sample={sample_name}, keys={keys}")
            return
        for path in paths:
            log(f"Queued reduction job from Kafka topic document {name}: {path}")

    dispatcher.subscribe(consume_document)
    log(f"Listening to Kafka topics {topics} on {bootstrap_servers}")
    dispatcher.start()


def replay_jsonl_messages(
    queue_path: str | Path,
    messages_path: str | Path,
    *,
    data_root: str | Path | None = None,
    output_root: str | Path | None = None,
    detectors: list[str] | tuple[str, ...] = DEFAULT_DETECTORS,
) -> int:
    """Convert saved JSONL Kafka-like messages into reducer jobs for testing."""
    count = 0
    with Path(messages_path).expanduser().resolve().open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = parse_kafka_message_line(line)
            if payload is None:
                continue
            count += len(
                append_jobs_from_payload(
                    queue_path,
                    payload,
                    data_root=data_root,
                    output_root=output_root,
                    detectors=detectors,
                )
            )
    return count
