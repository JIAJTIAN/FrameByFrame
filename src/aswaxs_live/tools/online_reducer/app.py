from __future__ import annotations

import time
from pathlib import Path
import re
import shutil
import sys
import tempfile

from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot

from aswaxs_live.app.qt_runtime import suppress_glx_warning
from aswaxs_live.app.theme import apply_tool_theme, fit_window_to_available_screen

from .config import OnlineConfig
from .engine import DEFAULT_V6_ROOT, OnlineReductionEngine, ensure_v6_importable
from .workspace import OnlineAnalysisWorkspace
from .zmq_receiver import ZmqReceiver


APP_ROOT = Path(__file__).resolve().parents[4]
SETTINGS_PATH = APP_ROOT / "online_reducer_settings.json"
WINDOW_SETTINGS_GROUP = "online_reducer_window"


def export_temporary_analysis_h5(session_dir: Path, destination: Path, sample_name: str) -> Path:
    session_dir = Path(session_dir).resolve()
    sources = sorted(path for path in session_dir.rglob("*.h5") if path.is_file())
    if not sources:
        raise FileNotFoundError("No temporary analysis HDF5 files are available yet.")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", sample_name).strip("._") or "online_1d"
    export_root = Path(destination).expanduser().resolve() / f"{safe_name}_online_1d"
    for source in sources:
        target = export_root / source.relative_to(session_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return export_root


def export_experiments_to_canonical(
    session_dir: Path, experiments: list[dict[str, str]]
) -> list[Path]:
    session_dir = Path(session_dir).resolve()
    written: list[Path] = []
    for values in experiments:
        source_root = session_dir / values["storage_name"]
        destination_root = Path(values["canonical_output_root"]).expanduser().resolve()
        for source in sorted(source_root.rglob("*.h5")):
            target = destination_root / source.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            written.append(target)
    return written


class MainWindow(QtWidgets.QMainWindow):
    enqueue_file = Signal(str, str, object)
    # Both forwarded through the main-thread event queue so they arrive at
    # engine_thread in FIFO order relative to enqueue_file events.
    _forward_plan_started = Signal(str, str)  # detector, scan_id
    _forward_plan_stopped = Signal(str, str)  # detector, scan_id

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FrameByFrame Online 1-D Reducer")
        self.config = OnlineConfig.load(SETTINGS_PATH)
        self._temporary_session = tempfile.TemporaryDirectory(prefix="framebyframe_online_1d_")
        self.session_dir = Path(self._temporary_session.name).resolve()
        self._experiments: dict[str, dict[str, str]] = {}
        self.receiver_thread = None
        self.receiver = None
        self.engine_thread = None
        self.engine = None
        # Per-detector: epoch seconds of last successfully received message.
        self._last_received: dict[str, float] = {}
        # Per-detector: total messages received this session.
        self._message_counts: dict[str, int] = {}
        # Per-detector: endpoint string (set when receiver starts).
        self._endpoints: dict[str, str] = {}
        self._build_ui()
        self._build_menus()
        self._load_config_into_ui()
        self._set_running(False)
        apply_tool_theme(self)
        self.start_button.setObjectName("PrimaryActionButton")
        fit_window_to_available_screen(self, (1480, 900), minimum=(980, 640))
        self._restore_window_layout()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 6)
        root.setSpacing(5)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root.addWidget(self.main_splitter, 1)
        self.setup_scroll = QtWidgets.QScrollArea()
        self.setup_scroll.setWidgetResizable(True)
        self.setup_scroll.setMinimumWidth(370)
        self.setup_scroll.setMaximumWidth(500)
        self.config_panel = QtWidgets.QWidget()
        self.setup_scroll.setWidget(self.config_panel)
        self.main_splitter.addWidget(self.setup_scroll)
        right = QtWidgets.QWidget()
        self.main_splitter.addWidget(right)
        self.main_splitter.setChildrenCollapsible(True)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([430, 790])

        form_root = QtWidgets.QVBoxLayout(self.config_panel)
        self.run_group = QtWidgets.QGroupBox("Run")
        run_form = QtWidgets.QFormLayout(self.run_group)
        self.sample = QtWidgets.QLineEdit()
        run_form.addRow("Fallback title", self.sample)
        temporary_note = QtWidgets.QLabel("Derived 1-D HDF5 data stays in temporary session storage until exported.")
        temporary_note.setWordWrap(True)
        temporary_note.setStyleSheet("color: #536273;")
        run_form.addRow(temporary_note)
        form_root.addWidget(self.run_group)

        zmq_group = QtWidgets.QGroupBox("ZMQ source (Bluesky RunEngine SUB)")
        zmq_form = QtWidgets.QFormLayout(zmq_group)
        self.saxs_endpoint = QtWidgets.QLineEdit()
        self.waxs_endpoint = QtWidgets.QLineEdit()
        self.saxs_det_name = QtWidgets.QLineEdit()
        self.saxs_det_name.setPlaceholderText("Pil300K")
        self.waxs_det_name = QtWidgets.QLineEdit()
        self.waxs_det_name.setPlaceholderText("Eig1M")
        self.remote_path_prefix = QtWidgets.QLineEdit()
        self.remote_path_prefix.setPlaceholderText("/home/chem_epics/.../data/ASWAXS/User")
        self.remote_path_prefix.setToolTip(
            "Server-side path prefix in the Bluesky stream (posix). "
            "Replaced by the local path prefix below when assembling file paths."
        )
        self.local_path_prefix = QtWidgets.QLineEdit()
        self.local_path_prefix.setPlaceholderText(r"Y:\ASWAXS\User")
        self.local_path_prefix.setToolTip(
            "Local Windows path where the beamline data directory is mounted. "
            "Replaces the remote prefix above."
        )
        self.zmq_debug = QtWidgets.QCheckBox("Log raw ZMQ frames")
        self.zmq_debug.setToolTip(
            "Print every raw ZMQ frame to the Messages log before parsing. "
            "Useful for diagnosing beamline path or format issues."
        )
        zmq_form.addRow("SAXS endpoint", self.saxs_endpoint)
        zmq_form.addRow("WAXS endpoint", self.waxs_endpoint)
        zmq_form.addRow("SAXS detector name", self.saxs_det_name)
        zmq_form.addRow("WAXS detector name", self.waxs_det_name)
        zmq_form.addRow("Remote path prefix", self.remote_path_prefix)
        zmq_form.addRow("Local path prefix", self.local_path_prefix)
        zmq_form.addRow("", self.zmq_debug)
        form_root.addWidget(zmq_group)

        cal_group = QtWidgets.QGroupBox("Detector calibration")
        cal_form = QtWidgets.QFormLayout(cal_group)
        self.pil_poni = self._path_row(cal_form, "Pil300K PONI")
        self.pil_mask = self._path_row(cal_form, "Pil300K mask")
        self.eig_poni = self._path_row(cal_form, "Eig1M PONI")
        self.eig_mask = self._path_row(cal_form, "Eig1M mask")
        self.pil_monitor = QtWidgets.QLineEdit(); self.eig_monitor = QtWidgets.QLineEdit()
        cal_form.addRow("Pil monitor", self.pil_monitor)
        cal_form.addRow("Eig monitor", self.eig_monitor)
        form_root.addWidget(cal_group)

        seq_group = QtWidgets.QGroupBox("Sequence and 1-D integration")
        seq_form = QtWidgets.QFormLayout(seq_group)
        self.frames = self._spin(1, 100000)
        self.frames.setToolTip("Number of repeated detector frames in one measurement. Set to 1 to keep every frame separate.")
        self.npt = self._spin(10, 100000)
        self.dataset = QtWidgets.QLineEdit()
        self.settle = QtWidgets.QDoubleSpinBox(); self.settle.setRange(0, 30); self.settle.setDecimals(2); self.settle.setSuffix(" s")
        self.settle.setToolTip(
            "Wait between two file-size checks before opening the raw HDF5 read-only. "
            "This avoids reading a detector file while it is still being written."
        )
        seq_form.addRow("Frames per measurement", self.frames); seq_form.addRow("q bins", self.npt)
        seq_form.addRow("HDF5 dataset", self.dataset); seq_form.addRow("File stability wait", self.settle)
        form_root.addWidget(seq_group)

        form_root.addStretch()
        self.apply_setup_button = QtWidgets.QPushButton("Save && Close Setup")
        self.apply_setup_button.setObjectName("PrimaryActionButton")
        self.apply_setup_button.setMinimumHeight(38)
        self.apply_setup_button.setToolTip("Validate and save these settings, then close the entire setup panel.")
        form_root.addWidget(self.apply_setup_button)

        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(5)
        action_row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start Online 1-D Reduction")
        self.start_button.setMinimumHeight(38)
        self.stop_button = QtWidgets.QPushButton("Stop")
        action_row.addWidget(self.start_button); action_row.addWidget(self.stop_button)
        action_row.addStretch(1)
        right_layout.addLayout(action_row)

        self.analysis_workspace = OnlineAnalysisWorkspace(self)
        self.analysis_workspace.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        right_layout.addWidget(self.analysis_workspace, 1)

        status_strip = QtWidgets.QFrame()
        status_strip.setObjectName("ToolStatus")
        status_layout = QtWidgets.QHBoxLayout(status_strip)
        status_layout.setContentsMargins(8, 4, 8, 4)
        self.status_label = QtWidgets.QLabel("Stopped")
        self.progress_labels: dict[str, QtWidgets.QLabel] = {}
        status_layout.addWidget(self.status_label, 1)
        for detector, display_name in (("Pil300K", "SAXS"), ("Eig1M", "WAXS")):
            label = QtWidgets.QLabel(f"{display_name}: idle")
            label.setToolTip(f"{detector}: no endpoint configured yet")
            self.progress_labels[detector] = label
            status_layout.addWidget(label)
        self.log_button = QtWidgets.QToolButton()
        self.log_button.setText("Messages")
        self.log_button.setCheckable(True)
        self.log_button.setChecked(False)
        self.log_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log_button.setArrowType(QtCore.Qt.ArrowType.UpArrow)
        status_layout.addWidget(self.log_button)
        right_layout.addWidget(status_strip)

        self._status_refresh_timer = QtCore.QTimer(self)
        self._status_refresh_timer.setInterval(3000)
        self._status_refresh_timer.timeout.connect(self._refresh_status_labels)

        self.log_box = QtWidgets.QPlainTextEdit(); self.log_box.setReadOnly(True); self.log_box.setMaximumBlockCount(5000)
        self.log_box.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.log_box.hide()
        right_layout.addWidget(self.log_box)

        self.apply_setup_button.clicked.connect(self.apply_setup_and_hide)
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.log_button.toggled.connect(self._set_log_visible)
        self.setup_scroll.hide()
        self.main_splitter.setSizes([0, 1200])

    def _build_menus(self) -> None:
        self.setup_action = QtGui.QAction("Acquisition Setup...", self)
        self.setup_action.setCheckable(True)
        self.setup_action.setShortcut(QtGui.QKeySequence("Ctrl+,"))
        self.setup_action.toggled.connect(self._set_setup_visible)

        file_menu = self.file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(self.setup_action)
        file_menu.addSeparator()
        file_menu.addAction("Close", self.close, QtGui.QKeySequence.StandardKey.Close)

        run_menu = self.run_menu = self.menuBar().addMenu("Run")
        self.start_action = run_menu.addAction("Start Online Reduction", self.start)
        run_menu.addAction("Stop", self.stop)

        self.messages_action = QtGui.QAction("Messages", self)
        self.messages_action.setCheckable(True)
        self.messages_action.toggled.connect(self.log_button.setChecked)
        self.log_button.toggled.connect(self.messages_action.setChecked)
        view_menu = self.view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self.setup_action)
        view_menu.addAction(self.messages_action)

    def _set_setup_visible(self, visible: bool) -> None:
        self.setup_scroll.setVisible(visible)
        if visible:
            self.main_splitter.setSizes([430, max(600, self.main_splitter.width() - 430)])

    def _set_log_visible(self, visible: bool) -> None:
        self.log_box.setVisible(visible)
        self.log_button.setArrowType(QtCore.Qt.ArrowType.DownArrow if visible else QtCore.Qt.ArrowType.UpArrow)

    def apply_setup_and_hide(self) -> None:
        try:
            self.config = self._config_from_ui()
            self._validate(self.config)
            self.config.save(SETTINGS_PATH)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Setup needs attention", str(exc))
            return
        self.setup_action.setChecked(False)
        self.status_label.setText("Setup saved. Ready to start online 1-D reduction.")

    def _path_row(self, form: QtWidgets.QFormLayout, label: str, directory: bool = False) -> QtWidgets.QLineEdit:
        edit = QtWidgets.QLineEdit(); button = QtWidgets.QPushButton("Browse")
        row = QtWidgets.QWidget(); layout = QtWidgets.QHBoxLayout(row); layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1); layout.addWidget(button)
        def browse():
            if directory:
                value = QtWidgets.QFileDialog.getExistingDirectory(self, label, edit.text())
            else:
                value, _ = QtWidgets.QFileDialog.getOpenFileName(self, label, edit.text())
            if value: edit.setText(value)
        button.clicked.connect(browse); form.addRow(label, row)
        return edit

    @staticmethod
    def _spin(low: int, high: int) -> QtWidgets.QSpinBox:
        widget = QtWidgets.QSpinBox(); widget.setRange(low, high); return widget

    def _load_config_into_ui(self) -> None:
        c = self.config
        mapping = [(self.sample,c.sample_name),(self.saxs_endpoint,c.saxs_endpoint),(self.waxs_endpoint,c.waxs_endpoint),
                   (self.saxs_det_name,c.saxs_detector_name),(self.waxs_det_name,c.waxs_detector_name),
                   (self.remote_path_prefix,c.remote_path_prefix),(self.local_path_prefix,c.local_path_prefix),
                   (self.pil_poni,c.pil300k_poni),(self.pil_mask,c.pil300k_mask),(self.eig_poni,c.eig1m_poni),(self.eig_mask,c.eig1m_mask),
                   (self.pil_monitor,c.pil300k_monitor_key),(self.eig_monitor,c.eig1m_monitor_key),(self.dataset,c.dataset_path)]
        for widget, value in mapping: widget.setText(str(value))
        for widget, value in [(self.frames,c.num_frames),(self.npt,c.npt)]:
            widget.setValue(value)
        self.settle.setValue(c.settle_seconds)

    def _config_from_ui(self) -> OnlineConfig:
        return OnlineConfig(
            sample_name=self.sample.text().strip(),
            saxs_endpoint=self.saxs_endpoint.text().strip(), waxs_endpoint=self.waxs_endpoint.text().strip(),
            saxs_detector_name=self.saxs_det_name.text().strip() or "Pil300K",
            waxs_detector_name=self.waxs_det_name.text().strip() or "Eig1M",
            remote_path_prefix=self.remote_path_prefix.text().strip(),
            local_path_prefix=self.local_path_prefix.text().strip(),
            pil300k_poni=self.pil_poni.text().strip(), pil300k_mask=self.pil_mask.text().strip(),
            eig1m_poni=self.eig_poni.text().strip(), eig1m_mask=self.eig_mask.text().strip(),
            num_frames=self.frames.value(),
            dataset_path=self.dataset.text().strip(), pil300k_monitor_key=self.pil_monitor.text().strip(), eig1m_monitor_key=self.eig_monitor.text().strip(),
            npt=self.npt.value(), settle_seconds=self.settle.value())

    def _validate(self, c: OnlineConfig) -> None:
        if not c.sample_name: raise ValueError("Sample / task name is required")
        for label, value in [("Pil300K PONI",c.pil300k_poni),("Pil300K mask",c.pil300k_mask),("Eig1M PONI",c.eig1m_poni),("Eig1M mask",c.eig1m_mask)]:
            if not Path(value).is_file(): raise ValueError(f"{label} file does not exist: {value}")

    def start(self) -> None:
        try:
            self.config = self._config_from_ui(); self._validate(self.config); ensure_v6_importable()
            self.config.save(SETTINGS_PATH)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot start", str(exc)); return
        self.setup_action.setChecked(False)
        self._append_log("Starting online 1-D reduction engine...")
        self.engine_thread = QtCore.QThread(self); self.engine = OnlineReductionEngine(self.config, self.session_dir)
        self.engine.moveToThread(self.engine_thread); self.engine_thread.started.connect(self.engine.initialize)
        self.engine.log.connect(self._append_log); self.engine.error.connect(self._on_error); self.engine.progress.connect(self._on_progress)
        self.engine.curve_ready.connect(self.analysis_workspace.add_curve)
        self.engine.image_ready.connect(self.analysis_workspace.update_detector_image)
        self.engine.experiment_discovered.connect(self._on_experiment_discovered)
        self.engine.plan_exported.connect(self._on_plan_exported)
        self.engine.detector_reset.connect(self.analysis_workspace.clear_detector)
        self.engine.ready.connect(self._start_receiver); self.enqueue_file.connect(self.engine.process_file)
        self._forward_plan_started.connect(self.engine.reset_detector)
        self._forward_plan_stopped.connect(self.engine.finalize_plan)
        self.engine.stopped.connect(self.engine_thread.quit); self.engine_thread.finished.connect(self._on_engine_stopped)
        self.engine_thread.start(); self._set_running(True)

    def _start_receiver(self) -> None:
        endpoints = {"Pil300K": self.config.saxs_endpoint, "Eig1M": self.config.waxs_endpoint}
        bluesky_names = {
            "Pil300K": self.config.saxs_detector_name,
            "Eig1M": self.config.waxs_detector_name,
        }
        self._endpoints = dict(endpoints)
        self._last_received.clear()
        self._message_counts.clear()
        self.receiver_thread = QtCore.QThread(self)
        self.receiver = ZmqReceiver(
            endpoints,
            bluesky_names=bluesky_names,
            debug=self.zmq_debug.isChecked(),
            remote_path_prefix=self.config.remote_path_prefix,
            local_path_prefix=self.config.local_path_prefix,
        )
        self.receiver.moveToThread(self.receiver_thread); self.receiver_thread.started.connect(self.receiver.run)
        self.receiver.plan_started.connect(self._forward_plan_started)
        self.receiver.plan_stopped.connect(self._forward_plan_stopped)
        self.receiver.image_received.connect(self.enqueue_file)
        self.receiver.image_received.connect(self._log_received_message)
        self.receiver.message_stats.connect(self._on_message_stats)
        self.receiver.status.connect(self._append_log)
        self.receiver.error.connect(self._on_error)
        self.receiver.stopped.connect(self.receiver_thread.quit); self.receiver_thread.start()
        for detector, endpoint in endpoints.items():
            label = self.progress_labels.get(detector)
            if label is not None:
                label.setToolTip(endpoint)
        self._status_refresh_timer.start()
        self.status_label.setText("Listening and reducing")

    def stop(self) -> None:
        self._status_refresh_timer.stop()
        if self.receiver: self.receiver.stop()
        if self.engine: QtCore.QMetaObject.invokeMethod(self.engine, "shutdown", QtCore.Qt.ConnectionType.QueuedConnection)
        self._append_log("Stop requested; finalizing V6 HDF5 outputs")
        self.stop_button.setEnabled(False)
        self.status_label.setText("Stopping and finalizing outputs...")

    def _on_engine_stopped(self) -> None:
        self._append_log("Online reduction stopped cleanly")
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running); self.stop_button.setEnabled(running); self.config_panel.setEnabled(not running)
        self.start_action.setEnabled(not running)
        self.setup_action.setToolTip(
            "Show setup values (read-only while reduction is running)."
            if running else "Show or hide acquisition and detector settings."
        )
        if not running: self.status_label.setText("Stopped")

    def _on_plan_exported(self, detector: str, paths: list) -> None:
        label = "SAXS" if detector == "Pil300K" else "WAXS"
        destinations = sorted({str(Path(p).parent) for p in paths})
        self.status_label.setText(
            f"{label} plan saved — {len(paths)} HDF5 file(s) exported to Extracted."
        )
        self._append_log(
            f"[{label}] Plan auto-exported to Extracted:\n" + "\n".join(destinations)
        )

    def _on_experiment_discovered(self, metadata: object) -> None:
        values = {str(key): str(value) for key, value in dict(metadata).items()}
        self._experiments[values["experiment_uid"]] = values
        self.status_label.setText(
            f"Experiment detected: {values['experiment_title']} [{values['experiment_uid'][:8]}]"
        )

    def export_session_h5_to(self, destination: Path) -> Path:
        return export_temporary_analysis_h5(self.session_dir, destination, self.config.sample_name)

    def _log_received_message(self, detector: str, path_str: str, payload: object) -> None:
        if not self.log_box.isVisible():
            return
        display = "SAXS" if detector == "Pil300K" else "WAXS"
        self.log_box.appendPlainText(f"[{display}] {Path(path_str).name}")

    def _append_log(self, text: str) -> None:
        self.log_box.appendPlainText(text)

    def _on_error(self, text: str) -> None:
        self._append_log("ERROR: " + text)
        self.status_label.setText(f"Error: {text[:120]}")
        if not self.log_box.isVisible():
            self.log_button.setChecked(True)

    def _on_message_stats(self, detector: str, count: int, endpoint: str) -> None:
        self._last_received[detector] = time.monotonic()
        self._message_counts[detector] = count
        label = self.progress_labels.get(detector)
        if label is not None:
            display_name = "SAXS" if detector == "Pil300K" else "WAXS"
            label.setText(f"{display_name}: {count} received | just now")

    def _refresh_status_labels(self) -> None:
        now = time.monotonic()
        for detector, label in self.progress_labels.items():
            display_name = "SAXS" if detector == "Pil300K" else "WAXS"
            count = self._message_counts.get(detector, 0)
            last = self._last_received.get(detector)
            if last is None:
                endpoint = self._endpoints.get(detector, "")
                label.setText(f"{display_name}: listening" + (f" ({endpoint})" if endpoint else ""))
            else:
                elapsed = int(now - last)
                if elapsed < 5:
                    age = "just now"
                elif elapsed < 60:
                    age = f"{elapsed}s ago"
                else:
                    age = f"{elapsed // 60}m ago"
                label.setText(f"{display_name}: {count} received | last: {age}")

    def _on_progress(self, detector: str, sequence: int, energy: int, group: int, frame: int) -> None:
        display_name = "SAXS" if detector == "Pil300K" else "WAXS"
        count = self._message_counts.get(detector, sequence)
        last = self._last_received.get(detector)
        label = self.progress_labels.get(detector)
        if label is not None:
            age = "just now" if last is not None and (time.monotonic() - last) < 5 else ""
            suffix = f" | last: {age}" if age else f" | M{group} F{frame}"
            label.setText(f"{display_name}: {count} received{suffix}")

    def _restore_window_layout(self) -> None:
        settings = QtCore.QSettings()
        settings.beginGroup(WINDOW_SETTINGS_GROUP)
        geometry = settings.value("geometry")
        splitter_state = settings.value("workspace_splitter")
        active_tab = settings.value("active_tab", 0, type=int)
        browser_visible = settings.value("browser_visible", True, type=bool)
        settings.endGroup()
        if geometry:
            self.restoreGeometry(geometry)
        if splitter_state:
            self.analysis_workspace.splitter.restoreState(splitter_state)
        self.analysis_workspace.tabs.setCurrentIndex(max(0, min(active_tab, self.analysis_workspace.tabs.count() - 1)))
        self.analysis_workspace.browser_button.setChecked(browser_visible)

    def _save_window_layout(self) -> None:
        settings = QtCore.QSettings()
        settings.beginGroup(WINDOW_SETTINGS_GROUP)
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("workspace_splitter", self.analysis_workspace.splitter.saveState())
        settings.setValue("active_tab", self.analysis_workspace.tabs.currentIndex())
        settings.setValue("browser_visible", self.analysis_workspace.browser_button.isChecked())
        settings.endGroup()

    def closeEvent(self, event) -> None:
        self._status_refresh_timer.stop()
        self._save_window_layout()
        if self.receiver is not None:
            self.receiver.stop()
        if self.receiver_thread is not None and self.receiver_thread.isRunning():
            self.receiver_thread.wait(2000)
        if self.engine is not None and self.engine_thread is not None and self.engine_thread.isRunning():
            QtCore.QMetaObject.invokeMethod(self.engine, "shutdown", QtCore.Qt.ConnectionType.BlockingQueuedConnection)
            self.engine_thread.quit()
            self.engine_thread.wait(5000)
        self._temporary_session.cleanup()
        event.accept()


def main() -> int:
    suppress_glx_warning()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("FrameByFrame Online 1-D Reducer")
    app.setOrganizationName("ChemMatCARS")
    window = MainWindow(); window.show()
    return app.exec()
