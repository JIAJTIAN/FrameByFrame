"""Deterministic unit tests for ZmqReceiver and parse_zmq_message.

All tests run without a real ZMQ server by either testing the parser directly
or driving a live ZMQ PUB/SUB pair bound to localhost (127.0.0.1) on a random
ephemeral port.  No beamline network access is needed.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import zmq

from aswaxs_live.tools.online_reducer.zmq_receiver import (
    ZmqReceiver,
    _event_hdf5_path,
    _resource_hdf5_path,
    parse_zmq_message,
)


# ---------------------------------------------------------------------------
# parse_zmq_message — pure parser tests (no network, no Qt)
# ---------------------------------------------------------------------------


def test_parse_legacy_json_bytes_returns_image_path_doc() -> None:
    result = parse_zmq_message(b'{"image_path": "/data/Pil300K/frame_001.h5"}')
    assert result is not None
    doc_type, doc_body = result
    assert doc_type == "image_path"
    assert doc_body["image_path"] == "/data/Pil300K/frame_001.h5"


def test_parse_legacy_json_string_works() -> None:
    result = parse_zmq_message('{"image_path": "/run/Eig1M/frame.h5", "uid": "abc"}')
    assert result is not None
    doc_type, doc_body = result
    assert doc_type == "image_path"
    assert Path(doc_body["image_path"]).name == "frame.h5"
    assert doc_body["uid"] == "abc"


def test_parse_legacy_missing_image_path_raises() -> None:
    with pytest.raises(ValueError, match="image_path"):
        parse_zmq_message(b'{"other_key": "value"}')


def test_parse_legacy_empty_image_path_raises() -> None:
    with pytest.raises(ValueError, match="image_path"):
        parse_zmq_message(b'{"image_path": "   "}')


def test_parse_malformed_json_raises_exception() -> None:
    with pytest.raises(Exception):
        parse_zmq_message(b"not-json-at-all")


def test_parse_unexpected_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_zmq_message(b'"just-a-string"')


# ---------------------------------------------------------------------------
# Bluesky RunEngine document format
# ---------------------------------------------------------------------------


def test_parse_bluesky_start_returns_start_doc() -> None:
    msg = json.dumps(["start", {"scan_id": 5, "plan_name": "count"}]).encode()
    result = parse_zmq_message(msg)
    assert result is not None
    doc_type, doc_body = result
    assert doc_type == "start"
    assert doc_body["scan_id"] == 5


def test_parse_bluesky_stop_returns_stop_doc() -> None:
    msg = json.dumps(["stop", {"exit_status": "success", "num_events": {}}]).encode()
    result = parse_zmq_message(msg)
    assert result is not None
    doc_type, _ = result
    assert doc_type == "stop"


def test_parse_bluesky_event_returns_event_doc() -> None:
    # Events are no longer silently dropped — the receiver loop handles them.
    msg = json.dumps(["event", {"seq_num": 1, "data": {}}]).encode()
    result = parse_zmq_message(msg)
    assert result is not None
    doc_type, doc_body = result
    assert doc_type == "event"
    assert doc_body["seq_num"] == 1


def test_parse_bluesky_descriptor_returns_none() -> None:
    msg = json.dumps(["descriptor", {"uid": "abc"}]).encode()
    assert parse_zmq_message(msg) is None


def test_parse_bluesky_datum_returns_none() -> None:
    msg = json.dumps(["datum", {"resource": "r", "datum_id": "r/0"}]).encode()
    assert parse_zmq_message(msg) is None


def test_parse_bluesky_resource_returns_resource_doc() -> None:
    body = {"spec": "AD_HDF5", "root": "/data", "resource_path": "scan5.h5"}
    msg = json.dumps(["resource", body]).encode()
    result = parse_zmq_message(msg)
    assert result is not None
    doc_type, doc_body = result
    assert doc_type == "resource"
    assert doc_body["spec"] == "AD_HDF5"


def test_parse_bluesky_bad_inner_types_raises() -> None:
    msg = json.dumps(["start", "not-a-dict"]).encode()
    with pytest.raises(ValueError):
        parse_zmq_message(msg)


# ---------------------------------------------------------------------------
# _resource_hdf5_path helper
# ---------------------------------------------------------------------------


def test_resource_hdf5_path_posix() -> None:
    doc = {"spec": "AD_HDF5", "root": "/data/2024", "resource_path": "scan5/frame.h5"}
    path = _resource_hdf5_path(doc)
    assert path == Path("/data/2024/scan5/frame.h5")


def test_resource_hdf5_path_absolute() -> None:
    doc = {"spec": "AD_HDF5", "root": "/data", "resource_path": "/abs/path/frame.h5",
           "path_semantics": "absolute"}
    path = _resource_hdf5_path(doc)
    assert path == Path("/abs/path/frame.h5")


def test_resource_hdf5_path_non_hdf5_returns_none() -> None:
    doc = {"spec": "TIFF", "root": "/data", "resource_path": "frame.tiff"}
    assert _resource_hdf5_path(doc) is None


def test_resource_hdf5_path_missing_resource_path_returns_none() -> None:
    doc = {"spec": "AD_HDF5", "root": "/data"}
    assert _resource_hdf5_path(doc) is None


# ---------------------------------------------------------------------------
# _event_hdf5_path helper
# ---------------------------------------------------------------------------


def test_event_hdf5_path_finds_hdf1_full_file_name() -> None:
    doc = {
        "data": {
            "Pil300K_Sim_hdf1_full_file_name": "/home/server/data/Sim_Test_S_0011_0001.h5",
            "Pil300K_Sim_stats1_total": 432083.0,
        }
    }
    result = _event_hdf5_path(doc, "Pil300K_Sim")
    assert result == "/home/server/data/Sim_Test_S_0011_0001.h5"


def test_event_hdf5_path_returns_none_when_no_h5() -> None:
    doc = {"data": {"Pil300K_Sim_stats1_total": 432083.0}}
    assert _event_hdf5_path(doc, "Pil300K_Sim") is None


def test_event_hdf5_path_does_not_match_other_detector_field() -> None:
    # WAXS socket must not claim a SAXS file via accidental fallback
    doc = {"data": {"Pil300K_Sim_hdf1_full_file_name": "/data/Pil300K_frame.h5"}}
    assert _event_hdf5_path(doc, "Eig1M") is None


# ---------------------------------------------------------------------------
# ZmqReceiver.path prefix substitution
# ---------------------------------------------------------------------------


def test_receiver_applies_path_prefix_substitution() -> None:
    try:
        from aswaxs_live.app.qt import QtCore, QtWidgets
    except Exception:
        pytest.skip("Qt not available")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    receiver = ZmqReceiver(
        {"Pil300K": "tcp://127.0.0.1:1"},
        bluesky_names={"Pil300K": "Pil300K_Sim"},
        remote_path_prefix="/home/server/data",
        local_path_prefix=r"Y:\data",
    )
    from pathlib import Path
    result = receiver._apply_path_prefix("/home/server/data/ASWAXS/frame.h5")
    assert result == Path(r"Y:\data\ASWAXS\frame.h5")


def test_receiver_no_prefix_substitution_when_not_configured() -> None:
    try:
        from aswaxs_live.app.qt import QtCore, QtWidgets
    except Exception:
        pytest.skip("Qt not available")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    receiver = ZmqReceiver({"Pil300K": "tcp://127.0.0.1:1"})
    from pathlib import Path
    result = receiver._apply_path_prefix("/home/server/data/frame.h5")
    assert result == Path("/home/server/data/frame.h5")


# ---------------------------------------------------------------------------
# ZmqReceiver over a real localhost PUB/SUB pair
# ---------------------------------------------------------------------------


def _bind_pub_socket(context: zmq.Context) -> tuple[zmq.Socket, int]:
    """Bind a PUB socket on an OS-assigned port and return (socket, port)."""
    sock = context.socket(zmq.PUB)
    sock.setsockopt(zmq.LINGER, 0)
    port = sock.bind_to_random_port("tcp://127.0.0.1")
    return sock, port


def _run_receiver_collecting(
    endpoint: str,
    *,
    timeout: float = 3.0,
    max_messages: int = 1,
) -> tuple[list[tuple], list[str], list[tuple[str, int, str]]]:
    """Run a ZmqReceiver in a background thread; return (image_events, errors, stats)."""
    try:
        from aswaxs_live.app.qt import QtCore, QtWidgets
    except Exception:
        pytest.skip("Qt not available in this environment")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    received: list[tuple] = []
    errors: list[str] = []
    stats: list[tuple[str, int, str]] = []

    receiver = ZmqReceiver({"Pil300K": endpoint})
    thread = QtCore.QThread()
    receiver.moveToThread(thread)
    thread.started.connect(receiver.run)
    receiver.image_received.connect(lambda det, path, pl: received.append((det, path, pl)))
    receiver.error.connect(errors.append)
    receiver.message_stats.connect(lambda det, cnt, ep: stats.append((det, cnt, ep)))

    thread.start()

    deadline = time.monotonic() + timeout
    while len(received) < max_messages and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)

    receiver.stop()
    thread.quit()
    thread.wait(2000)
    return received, errors, stats


def test_zmq_receiver_delivers_valid_image_path_message() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        time.sleep(0.25)
        payload = json.dumps({"image_path": "/run/Pil300K/frame_001.h5"}).encode()
        pub.send_multipart([payload])

    threading.Thread(target=_publish, daemon=True).start()
    received, errors, stats = _run_receiver_collecting(endpoint, timeout=3.0, max_messages=1)
    pub.close(0)

    assert len(received) == 1, f"expected 1 message, got {len(received)}; errors={errors}"
    det, path, pl = received[0]
    assert det == "Pil300K"
    assert "frame_001.h5" in path
    assert len(stats) == 1
    assert stats[0][1] == 1  # count
    assert stats[0][2] == endpoint


def test_zmq_receiver_delivers_bluesky_resource_message() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        time.sleep(0.25)
        doc = ["resource", {
            "spec": "AD_HDF5",
            "root": "/data/2024",
            "resource_path": "Pil300K_scan5.h5",
            "path_semantics": "posix",
        }]
        pub.send_multipart([json.dumps(doc).encode()])

    threading.Thread(target=_publish, daemon=True).start()
    received, errors, stats = _run_receiver_collecting(endpoint, timeout=3.0, max_messages=1)
    pub.close(0)

    assert len(received) == 1, f"expected 1 message, got {len(received)}; errors={errors}"
    det, path, _ = received[0]
    assert det == "Pil300K"
    assert "Pil300K_scan5.h5" in path


def test_zmq_receiver_skips_bluesky_event_docs_without_hdf5_path() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        # Events with no .h5 data should not trigger image_received
        for doc_type in ("start", "descriptor", "datum", "stop"):
            time.sleep(0.05)
            pub.send_multipart([json.dumps([doc_type, {"uid": "x"}]).encode()])
        # Event with no h5 path — should be silently ignored
        time.sleep(0.05)
        pub.send_multipart([json.dumps(["event", {"data": {"some_val": 1.0}}]).encode()])
        # Then one good legacy message
        time.sleep(0.1)
        pub.send_multipart([json.dumps({"image_path": "/run/Pil300K/good.h5"}).encode()])

    threading.Thread(target=_publish, daemon=True).start()
    received, errors, _ = _run_receiver_collecting(endpoint, timeout=5.0, max_messages=1)
    pub.close(0)

    assert len(received) == 1
    assert len(errors) == 0, f"unexpected errors: {errors}"


def test_zmq_receiver_delivers_event_hdf5_path() -> None:
    try:
        from aswaxs_live.app.qt import QtCore, QtWidgets
    except Exception:
        pytest.skip("Qt not available in this environment")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        time.sleep(0.25)
        event_doc = ["event", {
            "seq_num": 1,
            "data": {
                "Pil300K_Sim_hdf1_full_file_name": "/home/server/data/Sim_Test_S_0011_0001.h5",
                "Pil300K_Sim_stats1_total": 432083.0,
            },
            "filled": {"Pil300K_Sim_image": False},
        }]
        pub.send_multipart([json.dumps(event_doc).encode()])

    threading.Thread(target=_publish, daemon=True).start()

    received: list[tuple] = []
    errors: list[str] = []
    receiver = ZmqReceiver(
        {"Pil300K": endpoint},
        bluesky_names={"Pil300K": "Pil300K_Sim"},
    )
    thread = QtCore.QThread()
    receiver.moveToThread(thread)
    thread.started.connect(receiver.run)
    receiver.image_received.connect(lambda det, path, pl: received.append((det, path, pl)))
    receiver.error.connect(errors.append)
    thread.start()

    deadline = time.monotonic() + 3.0
    while len(received) < 1 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)

    receiver.stop()
    thread.quit()
    thread.wait(2000)
    pub.close(0)

    assert len(received) == 1, f"expected 1, got {len(received)}; errors={errors}"
    det, path, _ = received[0]
    assert det == "Pil300K"
    assert "Sim_Test_S_0011_0001.h5" in path


def test_zmq_receiver_increments_count_across_messages() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        for i in range(3):
            time.sleep(0.25)
            payload = json.dumps({"image_path": f"/run/Pil300K/frame_{i:03d}.h5"}).encode()
            pub.send_multipart([payload])

    threading.Thread(target=_publish, daemon=True).start()
    received, errors, stats = _run_receiver_collecting(endpoint, timeout=4.0, max_messages=3)
    pub.close(0)

    assert len(received) == 3, f"expected 3 messages, got {len(received)}"
    assert [s[1] for s in stats] == [1, 2, 3]


def test_zmq_receiver_survives_malformed_message_and_continues() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        time.sleep(0.25)
        pub.send_multipart([b"not-json-at-all"])  # malformed
        time.sleep(0.25)
        payload = json.dumps({"image_path": "/run/Pil300K/good.h5"}).encode()
        pub.send_multipart([payload])  # good

    threading.Thread(target=_publish, daemon=True).start()
    received, errors, stats = _run_receiver_collecting(endpoint, timeout=4.0, max_messages=1)
    pub.close(0)

    assert len(received) == 1
    assert any("rejected" in e for e in errors), f"expected a rejection error, got: {errors}"


def test_zmq_receiver_handles_missing_image_path_field() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        time.sleep(0.25)
        pub.send_multipart([json.dumps({"other_key": "value"}).encode()])

    threading.Thread(target=_publish, daemon=True).start()
    received, errors, _ = _run_receiver_collecting(endpoint, timeout=2.0, max_messages=999)
    pub.close(0)

    assert len(received) == 0
    assert any("image_path" in e for e in errors), f"errors: {errors}"


def test_zmq_receiver_stops_cleanly_with_no_messages() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.1)

    received, errors, _ = _run_receiver_collecting(endpoint, timeout=0.3, max_messages=999)
    pub.close(0)

    assert len(received) == 0
    assert len(errors) == 0


def test_zmq_receiver_uses_last_frame_of_multipart_message() -> None:
    ctx = zmq.Context.instance()
    pub, port = _bind_pub_socket(ctx)
    endpoint = f"tcp://127.0.0.1:{port}"
    time.sleep(0.15)

    def _publish():
        time.sleep(0.25)
        topic_prefix = b"Pil300K"
        payload = json.dumps({"image_path": "/run/Pil300K/multipart.h5"}).encode()
        pub.send_multipart([topic_prefix, payload])

    threading.Thread(target=_publish, daemon=True).start()
    received, errors, _ = _run_receiver_collecting(endpoint, timeout=3.0, max_messages=1)
    pub.close(0)

    assert len(received) == 1
    assert "multipart.h5" in received[0][1]
