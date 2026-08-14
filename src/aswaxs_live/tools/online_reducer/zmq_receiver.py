from __future__ import annotations

import json
from pathlib import Path

import zmq
from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot


def parse_image_message(raw: bytes | str) -> tuple[Path, dict]:
    """Parse the shared reducer's JSON/image_path message contract."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("ZMQ payload must be a JSON object")
    image_path = str(payload.get("image_path", "")).strip()
    if not image_path:
        raise ValueError("Missing 'image_path' field")
    return Path(image_path), payload


class ZmqReceiver(QtCore.QObject):
    image_received = Signal(str, str, object)
    # Emitted after each successfully parsed message: (detector, total_count, endpoint)
    message_stats = Signal(str, int, str)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, endpoints: dict[str, str]) -> None:
        super().__init__()
        self.endpoints = dict(endpoints)
        self._running = False

    @Slot()
    def run(self) -> None:
        self._running = True
        context = zmq.Context()
        poller = zmq.Poller()
        sockets: dict[object, str] = {}
        endpoint_for: dict[object, str] = {}
        counts: dict[str, int] = {detector: 0 for detector in self.endpoints}
        try:
            for detector, endpoint in self.endpoints.items():
                sock = context.socket(zmq.SUB)
                sock.setsockopt_string(zmq.SUBSCRIBE, "")
                sock.setsockopt(zmq.LINGER, 0)
                sock.connect(endpoint)
                poller.register(sock, zmq.POLLIN)
                sockets[sock] = detector
                endpoint_for[sock] = endpoint
                self.status.emit(f"{detector}: listening on {endpoint}")
            while self._running:
                for sock, event in dict(poller.poll(100)).items():
                    if not event & zmq.POLLIN:
                        continue
                    detector = sockets[sock]
                    try:
                        frames = sock.recv_multipart()
                        raw = frames[-1]
                        path, payload = parse_image_message(raw)
                        counts[detector] += 1
                        self.image_received.emit(detector, str(path), payload)
                        self.message_stats.emit(detector, counts[detector], endpoint_for[sock])
                    except Exception as exc:  # keep the listener alive after a bad message
                        self.error.emit(f"{detector}: rejected ZMQ message: {exc}")
        except Exception as exc:
            self.error.emit(f"ZMQ listener stopped: {exc}")
        finally:
            for sock in sockets:
                sock.close(0)
            context.term()
            self.stopped.emit()

    @Slot()
    def stop(self) -> None:
        self._running = False

