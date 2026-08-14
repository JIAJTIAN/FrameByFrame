"""Deterministic unit tests for ZmqReceiver and parse_image_message.

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

from aswaxs_live.tools.online_reducer.zmq_receiver import ZmqReceiver, parse_image_message


# ---------------------------------------------------------------------------
# parse_image_message — pure parser tests (no network, no Qt)
# ---------------------------------------------------------------------------


def test_parse_valid_json_bytes_returns_path_and_payload() -> None:
    path, payload = parse_image_message(b'{"image_path": "/data/Pil300K/frame_001.h5"}')

    assert path == Path("/data/Pil300K/frame_001.h5")
    assert payload["image_path"] == "/data/Pil300K/frame_001.h5"


def test_parse_valid_json_string_works() -> None:
    path, payload = parse_image_message('{"image_path": "/run/Eig1M/frame.h5", "uid": "abc"}')

    assert path.name == "frame.h5"
    assert payload["uid"] == "abc"


def test_parse_missing_image_path_raises_value_error() -> None:
    with pytest.raises(ValueError, match="image_path"):
        parse_image_message(b'{"other_key": "value"}')


def test_parse_empty_image_path_raises_value_error() -> None:
    with pytest.raises(ValueError, match="image_path"):
        parse_image_message(b'{"image_path": "   "}')


def test_parse_non_dict_payload_raises_value_error() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_image_message(b'["not", "a", "dict"]')


def test_parse_malformed_json_raises_exception() -> None:
    with pytest.raises(Exception):
        parse_image_message(b"not-json-at-all")


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
    time.sleep(0.15)  # let SUB connect before first PUB message

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

    # The bad message should produce an error, the good one should be received.
    assert len(received) == 1
    assert any("rejected" in e or "image_path" in e or "JSON" in e for e in errors), \
        f"expected a rejection error, got: {errors}"


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

    # Start receiver and stop it after 0.3 s without sending anything.
    received, errors, _ = _run_receiver_collecting(endpoint, timeout=0.3, max_messages=999)
    pub.close(0)

    assert len(received) == 0
    assert len(errors) == 0


def test_zmq_receiver_uses_last_frame_of_multipart_message() -> None:
    """The receiver should parse the last part of a multipart message."""
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
