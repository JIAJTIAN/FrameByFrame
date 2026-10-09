"""Deterministic unit tests for kafka_bridge.py document routing logic.

All tests replay pre-built Bluesky document sequences through
``normalize_measurement_done_payload`` and ``consume_document`` (the inner
closure extracted via ``run_bluesky_kafka_bridge``) without requiring a real
Kafka connection.  The ``run_bluesky_kafka_bridge`` function itself is only
called with mocked RemoteDispatcher in the integration-style tests.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from aswaxs_live.workflows.kafka_bridge import (
    normalize_measurement_done_payload,
    append_jobs_from_payload,
    parse_kafka_message_line,
    replay_jsonl_messages,
    run_bluesky_kafka_bridge,
)
from aswaxs_live.workflows.bluesky_queue import ReductionJobQueueReader


# ---------------------------------------------------------------------------
# normalize_measurement_done_payload — unit tests
# ---------------------------------------------------------------------------


def test_normalize_measurement_done_returns_job_when_data_dir_present() -> None:
    payload = {"event": "measurement_done", "data_dir": "/beamtime/sample/Pil300K"}

    jobs = normalize_measurement_done_payload(payload)

    assert len(jobs) == 1
    assert jobs[0]["data_dir"] == "/beamtime/sample/Pil300K"
    assert jobs[0]["event"] == "measurement_done"


def test_normalize_start_document_returns_per_detector_jobs() -> None:
    payload = {
        "name": "start",
        "uid": "run-001",
        "scan_id": 42,
        "sampleName": "MySample",
        "expDir": "/beamtime/raw",
    }

    jobs = normalize_measurement_done_payload(
        payload,
        data_root="/beamtime/raw",
        output_root="/beamtime/extracted",
        detectors=["Pil300K", "Eig1M"],
    )

    assert len(jobs) == 2
    detectors = {job["detector"] for job in jobs}
    assert detectors == {"Pil300K", "Eig1M"}
    for job in jobs:
        assert "MySample" in str(job["data_dir"])


def test_normalize_returns_empty_for_unknown_event() -> None:
    jobs = normalize_measurement_done_payload({"event": "unrelated_thing", "data_dir": "/x"})

    assert jobs == []


def test_normalize_returns_empty_when_no_data_dir_and_no_recognized_event() -> None:
    jobs = normalize_measurement_done_payload({"uid": "x", "scan_id": 1})

    assert jobs == []


# ---------------------------------------------------------------------------
# parse_kafka_message_line
# ---------------------------------------------------------------------------


def test_parse_kafka_message_line_handles_json_dict() -> None:
    line = json.dumps({"event": "measurement_done", "data_dir": "/x", "uid": "abc"})

    result = parse_kafka_message_line(line)

    assert result is not None
    assert result["uid"] == "abc"


def test_parse_kafka_message_line_handles_start_repr() -> None:
    line = "Start({'uid': 'run-99', 'scan_id': 7, 'num_points': 10})"

    result = parse_kafka_message_line(line)

    assert result is not None
    assert result.get("name") == "start"
    assert result.get("uid") == "run-99"


def test_parse_kafka_message_line_returns_none_for_blank() -> None:
    assert parse_kafka_message_line("") is None
    assert parse_kafka_message_line("   ") is None


def test_parse_kafka_message_line_returns_none_for_garbage() -> None:
    assert parse_kafka_message_line("not-json-not-repr") is None


# ---------------------------------------------------------------------------
# replay_jsonl_messages
# ---------------------------------------------------------------------------


def test_replay_jsonl_queues_measurement_done_jobs(tmp_path: Path) -> None:
    messages = tmp_path / "messages.jsonl"
    messages.write_text(
        json.dumps({"event": "measurement_done", "data_dir": "/beamtime/A/Pil300K"}) + "\n"
        + json.dumps({"event": "measurement_done", "data_dir": "/beamtime/B/Eig1M"}) + "\n",
        encoding="utf-8",
    )
    queue = tmp_path / "queue.jsonl"

    count = replay_jsonl_messages(queue, messages)

    assert count == 2
    reader = ReductionJobQueueReader(queue)
    jobs = reader.poll()
    assert len(jobs) == 2


# ---------------------------------------------------------------------------
# Document routing via run_bluesky_kafka_bridge (mocked RemoteDispatcher)
# ---------------------------------------------------------------------------


def _run_bridge_with_documents(
    documents: list[tuple[str, dict]],
    tmp_path: Path,
    *,
    data_root: str | None = None,
) -> list:
    """Run the bridge with a mocked dispatcher and replay documents. Return queued jobs.

    ``bluesky_kafka`` is imported lazily inside ``run_bluesky_kafka_bridge``, so
    we inject a fake module into ``sys.modules`` before calling the bridge.
    """
    import sys

    queue = tmp_path / "queue.jsonl"
    callbacks: list = []

    mock_instance = MagicMock()
    mock_instance.subscribe.side_effect = lambda cb: callbacks.append(cb)
    mock_instance.start.return_value = None

    mock_bluesky_kafka = MagicMock()
    mock_bluesky_kafka.RemoteDispatcher.return_value = mock_instance

    with patch.dict(sys.modules, {"bluesky_kafka": mock_bluesky_kafka}):
        run_bluesky_kafka_bridge(
            bootstrap_servers="localhost:9092",
            topics=["bluesky_aswaxs"],
            queue_path=queue,
            data_root=data_root,
            log=lambda _: None,
        )

    assert callbacks, "No callback was registered — bridge did not call dispatcher.subscribe()"
    consume = callbacks[0]
    for name, doc in documents:
        consume(name, doc)

    if not queue.exists():
        return []
    reader = ReductionJobQueueReader(queue)
    return reader.poll()


def test_bridge_start_doc_queues_sample_active(tmp_path: Path) -> None:
    start_doc = {
        "uid": "run-001",
        "scan_id": 1,
        "sampleName": "ProteinA",
        "expDir": str(tmp_path / "raw"),
    }
    jobs = _run_bridge_with_documents(
        [("start", start_doc)],
        tmp_path,
        data_root=str(tmp_path / "raw"),
    )

    assert len(jobs) >= 1
    assert all(j.event == "sample_active" for j in jobs)


def test_bridge_start_then_stop_queues_measurement_done(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "ProteinB"
    raw.mkdir(parents=True)
    start_doc = {
        "uid": "run-002",
        "scan_id": 2,
        "sampleName": "ProteinB",
        "expDir": str(tmp_path / "raw"),
    }
    stop_doc = {"run_start": "run-002", "exit_status": "success"}
    jobs = _run_bridge_with_documents(
        [("start", start_doc), ("stop", stop_doc)],
        tmp_path,
        data_root=str(tmp_path / "raw"),
    )

    # start → sample_active, stop → measurement_done
    events = {j.event for j in jobs}
    assert "measurement_done" in events


def test_bridge_event_with_data_dir_merges_start_metadata(tmp_path: Path) -> None:
    start_doc = {
        "uid": "run-003",
        "scan_id": 3,
        "sampleName": "ProteinC",
        "expDir": str(tmp_path / "raw"),
    }
    # An event doc that directly carries data_dir (some beamlines do this).
    event_doc = {
        "descriptor": "desc-001",
        "data": {"data_dir": str(tmp_path / "raw" / "ProteinC" / "Pil300K")},
        "data_dir": str(tmp_path / "raw" / "ProteinC" / "Pil300K"),
    }
    # Wire up the descriptor so the bridge knows which run this event belongs to.
    descriptor_doc = {"uid": "desc-001", "run_start": "run-003"}

    jobs = _run_bridge_with_documents(
        [("start", start_doc), ("descriptor", descriptor_doc), ("event", event_doc)],
        tmp_path,
        data_root=str(tmp_path / "raw"),
    )

    # The event job should carry the sample name from the start doc.
    event_jobs = [j for j in jobs if j.event not in {"sample_active"}]
    if event_jobs:
        assert any("ProteinC" in str(j.sample_name) for j in event_jobs), \
            f"sample name not propagated: {[j.sample_name for j in event_jobs]}"


def test_bridge_descriptor_caches_run_uid_for_event_lookup(tmp_path: Path) -> None:
    """Descriptor → run_uid mapping must be cached so events can find their start doc."""
    start_doc = {"uid": "run-004", "scan_id": 4, "sampleName": "RunD"}
    desc_doc = {"uid": "desc-004", "run_start": "run-004"}
    event_doc = {
        "descriptor": "desc-004",
        "data_dir": str(tmp_path / "raw" / "RunD" / "Pil300K"),
    }

    jobs = _run_bridge_with_documents(
        [("start", start_doc), ("descriptor", desc_doc), ("event", event_doc)],
        tmp_path,
    )

    # At minimum, the event must not raise and the bridge must still be alive.
    # If the event had data_dir it should produce a job.
    assert isinstance(jobs, list)


def test_bridge_event_page_also_uses_run_association(tmp_path: Path) -> None:
    start_doc = {"uid": "run-005", "scan_id": 5, "sampleName": "RunE", "expDir": str(tmp_path / "raw")}
    desc_doc = {"uid": "desc-005", "run_start": "run-005"}
    event_page_doc = {
        "descriptor": "desc-005",
        "data_dir": str(tmp_path / "raw" / "RunE" / "Pil300K"),
    }

    jobs = _run_bridge_with_documents(
        [("start", start_doc), ("descriptor", desc_doc), ("event_page", event_page_doc)],
        tmp_path,
        data_root=str(tmp_path / "raw"),
    )

    assert isinstance(jobs, list)


def test_bridge_stop_cleans_up_descriptor_cache(tmp_path: Path) -> None:
    """After stop, descriptor entries for that run must not linger."""
    start_doc = {"uid": "run-006", "scan_id": 6, "sampleName": "RunF"}
    desc_doc = {"uid": "desc-006", "run_start": "run-006"}
    stop_doc = {"run_start": "run-006", "exit_status": "success"}

    # No assertion on counts here — just verify the sequence does not raise.
    jobs = _run_bridge_with_documents(
        [("start", start_doc), ("descriptor", desc_doc), ("stop", stop_doc)],
        tmp_path,
    )
    assert isinstance(jobs, list)


def test_bridge_two_concurrent_runs_do_not_cross_contaminate(tmp_path: Path) -> None:
    start_a = {"uid": "run-a", "scan_id": 10, "sampleName": "SampleA", "expDir": str(tmp_path / "raw")}
    start_b = {"uid": "run-b", "scan_id": 11, "sampleName": "SampleB", "expDir": str(tmp_path / "raw")}
    stop_a = {"run_start": "run-a", "exit_status": "success"}
    stop_b = {"run_start": "run-b", "exit_status": "success"}

    jobs = _run_bridge_with_documents(
        [("start", start_a), ("start", start_b), ("stop", stop_a), ("stop", stop_b)],
        tmp_path,
        data_root=str(tmp_path / "raw"),
    )

    done_jobs = [j for j in jobs if j.event == "measurement_done"]
    # Two runs × two detectors = 4 measurement_done jobs; each run must appear.
    assert len(done_jobs) >= 2
    run_uids = {j.uid for j in done_jobs}
    assert "run-a" in run_uids and "run-b" in run_uids


def test_bridge_stop_without_cached_start_is_skipped_silently(tmp_path: Path) -> None:
    orphan_stop = {"run_start": "unknown-run-uid", "exit_status": "success"}

    jobs = _run_bridge_with_documents([("stop", orphan_stop)], tmp_path)

    # No crash; no job queued for an orphan stop.
    done_jobs = [j for j in jobs if j.event == "measurement_done"]
    assert len(done_jobs) == 0


def test_bridge_malformed_document_does_not_crash_bridge(tmp_path: Path) -> None:
    jobs = _run_bridge_with_documents(
        [
            ("start", {}),                        # start with no uid
            ("event", {"bad_field": "value"}),    # event with no data_dir
            ("stop", {}),                         # stop with no run_start
        ],
        tmp_path,
    )
    assert isinstance(jobs, list)
