from __future__ import annotations

import json
from pathlib import Path

import zmq
from aswaxs_live.app.qt import QtCore, Signal, Slot


# Bluesky document types that carry no image data — silently ignored.
_SILENT_DOC_TYPES = frozenset({
    "descriptor", "event", "event_page",
    "datum", "datum_page", "bulk_datum", "bulk_events",
})

# Area Detector HDF5 resource spec strings (case-insensitive check).
_HDF5_SPEC_KEYWORDS = ("HDF5", "HDF")


def _resource_hdf5_path(doc: dict) -> Path | None:
    """Return the HDF5 file path from a Bluesky resource document, or None."""
    spec = str(doc.get("spec", "")).upper()
    if not any(kw in spec for kw in _HDF5_SPEC_KEYWORDS):
        return None
    root = str(doc.get("root", "")).strip()
    resource_path = str(doc.get("resource_path", "")).strip()
    if not resource_path:
        return None
    path_semantics = str(doc.get("path_semantics", "posix"))
    if path_semantics == "absolute" or not root:
        return Path(resource_path)
    return Path(root) / resource_path


def _detector_matches(hdf5_path: Path, detector: str) -> bool:
    """Return True if the file path appears to belong to this detector."""
    name_lower = hdf5_path.name.lower()
    return detector.lower() in name_lower


def parse_zmq_message(raw: bytes | str) -> tuple[str, dict] | None:
    """
    Parse a ZMQ message from either Bluesky or legacy format.

    Returns (doc_type, doc_body) where doc_type is one of:
      "start", "stop", "resource", "image_path"  — caller handles each.
    Returns None for silently-skipped document types.
    Raises ValueError for truly malformed messages.
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    payload = json.loads(text)

    # Bluesky RunEngine format: ["doc_type", {doc_body}]
    if isinstance(payload, list) and len(payload) == 2:
        doc_type, doc_body = payload
        if not isinstance(doc_type, str) or not isinstance(doc_body, dict):
            raise ValueError(
                f"Bluesky list payload must be [str, dict], "
                f"got [{type(doc_type).__name__}, {type(doc_body).__name__}]"
            )
        if doc_type in _SILENT_DOC_TYPES:
            return None
        return doc_type, doc_body

    # Legacy format: {"image_path": "..."}
    if isinstance(payload, dict):
        image_path = str(payload.get("image_path", "")).strip()
        if not image_path:
            raise ValueError("Missing 'image_path' in legacy ZMQ message")
        return "image_path", payload

    raise ValueError(f"Unexpected payload type: {type(payload).__name__}")


class ZmqReceiver(QtCore.QObject):
    image_received = Signal(str, str, object)
    message_stats = Signal(str, int, str)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(
        self,
        endpoints: dict[str, str],
        bluesky_names: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.endpoints = dict(endpoints)
        # Maps canonical detector key → Bluesky stream detector name used in filenames.
        # Defaults to the canonical key itself when not provided.
        self.bluesky_names: dict[str, str] = dict(bluesky_names) if bluesky_names else {}
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
                    raw = b""
                    try:
                        frames = sock.recv_multipart()
                        raw = frames[-1]
                        result = parse_zmq_message(raw)
                        if result is None:
                            continue  # silently skip (event, descriptor, datum, …)
                        doc_type, doc_body = result

                        if doc_type == "start":
                            scan_id = doc_body.get("scan_id", "?")
                            plan = doc_body.get("plan_name", "?")
                            dets = doc_body.get("detectors", [])
                            self.status.emit(
                                f"Scan {scan_id} started — plan={plan}, detectors={dets}"
                            )

                        elif doc_type == "stop":
                            exit_status = doc_body.get("exit_status", "?")
                            num_events = doc_body.get("num_events", {})
                            self.status.emit(
                                f"Scan stopped — status={exit_status}, events={num_events}"
                            )

                        elif doc_type == "resource":
                            hdf5_path = _resource_hdf5_path(doc_body)
                            if hdf5_path is not None:
                                # When both detectors share one endpoint, filter by filename.
                                # When on separate endpoints, emit regardless.
                                all_detectors = list(sockets.values())
                                same_endpoint = all_detectors.count(all_detectors[0]) == len(all_detectors)
                                match_name = self.bluesky_names.get(detector, detector)
                                if same_endpoint and not _detector_matches(hdf5_path, match_name):
                                    continue
                                counts[detector] += 1
                                self.image_received.emit(detector, str(hdf5_path), doc_body)
                                self.message_stats.emit(
                                    detector, counts[detector], endpoint_for[sock]
                                )

                        elif doc_type == "image_path":
                            # Legacy {"image_path": "..."} contract
                            path = Path(str(doc_body.get("image_path", ""))).expanduser().resolve()
                            counts[detector] += 1
                            self.image_received.emit(detector, str(path), doc_body)
                            self.message_stats.emit(
                                detector, counts[detector], endpoint_for[sock]
                            )

                        # All other doc types (e.g. "datum_page", custom): silently ignore.

                    except Exception as exc:
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
