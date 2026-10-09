import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
from aswaxs_live.app.qt import QtCore, QtWidgets

from aswaxs_live.app import dashboard
from aswaxs_live.workflows.queue import TaskSpec


_APP: QtWidgets.QApplication | None = None


def _app() -> QtWidgets.QApplication:
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def test_task_builder_uses_guided_pages(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    app = _app()
    window = dashboard.DashboardWindow()
    window.show()
    window.tabs.setCurrentIndex(0)
    app.processEvents()

    assert window.tabs.tabText(0) == "Task Builder"
    assert window.tabs.tabText(1) == "Dashboard"
    assert window.builder_step_titles[0] == "Raw Data"
    assert window.builder_stack.count() == 5
    assert window.builder_stack.currentIndex() == 0
    assert not window.builder_back_button.isEnabled()

    sample_folder = tmp_path / "example_sample"
    window.raw_folder_edit.setText(str(sample_folder))
    window.builder_next_button.click()
    assert window.task_name_edit.text() == "example_sample"
    assert window.builder_stack.currentIndex() == 1

    for expected_page in range(2, 5):
        window.builder_next_button.click()
        app.processEvents()
        assert window.builder_stack.currentIndex() == expected_page

    assert window.builder_next_button.isHidden()
    assert not window.add_task_button.isHidden()
    assert window.update_task_button.isHidden()

    window.editing_index = 0
    window._set_builder_step(4)
    assert window.add_task_button.isHidden()
    assert not window.update_task_button.isHidden()
    labels = {label.text() for label in window.findChildren(QtWidgets.QLabel)}
    assert "Sample thickness" in labels
    assert "Capillary thickness" not in labels
    calibration_labels = {label.text() for label in window.builder_stack.widget(3).findChildren(QtWidgets.QLabel)}
    sample_labels = {label.text() for label in window.builder_stack.widget(4).findChildren(QtWidgets.QLabel)}
    assert "GC group" not in calibration_labels
    assert "Air group" not in calibration_labels
    assert "Empty group" not in calibration_labels
    assert "GC group" in sample_labels
    assert "Air group" in sample_labels
    assert "Empty group" in sample_labels
    window.close()


def test_toolbar_does_not_bypass_wizard(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    app = _app()
    window = dashboard.DashboardWindow()

    queue_toolbar = next(toolbar for toolbar in window.findChildren(QtWidgets.QToolBar) if toolbar.windowTitle() == "Queue")
    assert window.add_to_queue_action not in queue_toolbar.actions()
    window.close()


def test_task_builder_step_buttons_jump_directly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    app = _app()
    window = dashboard.DashboardWindow()
    sample_folder = tmp_path / "direct_jump_sample"
    window.raw_folder_edit.setText(str(sample_folder))

    calibration_button = next(
        button for button in window.builder_step_buttons if button.text() == "4. Calibration"
    )
    calibration_button.click()
    app.processEvents()

    assert window.builder_stack.currentIndex() == 3
    assert window.task_name_edit.text() == "direct_jump_sample"
    assert calibration_button.property("current") is True
    assert window.builder_step_buttons[0].property("current") is False
    window.builder_step_buttons[1].click()
    app.processEvents()
    assert window.builder_stack.currentIndex() == 1
    window.close()


def test_needs_attention_tooltip_shows_validation_reasons(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    app = _app()
    window = dashboard.DashboardWindow()
    task = TaskSpec(
        task_name="bad_task",
        raw_folder=str(tmp_path / "raw"),
        output_dir=str(tmp_path / "out"),
        num_energies=1,
        num_groups=1,
        num_frames=1,
        pil300k_poni="",
        pil300k_mask="",
        eig1m_poni="",
        eig1m_mask="",
        status="Needs Attention",
        message="Pil300K: no files; Pil300K PONI missing; Pil300K mask missing",
    )

    tooltip = window._task_tooltip(task)

    assert "Status: Needs Attention" in tooltip
    assert "Needs attention:" in tooltip
    assert "- Pil300K: no files" in tooltip
    assert "- Pil300K PONI missing" in tooltip
    assert "Task information:" in tooltip
    window.close()


def test_sequence_step_auto_scans_monitor_pvs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    raw = tmp_path / "sample"
    pil = raw / "Pil300K"
    eig = raw / "Eig1M"
    pil.mkdir(parents=True)
    eig.mkdir(parents=True)
    with h5py.File(pil / "pil_0001.h5", "w") as handle:
        handle.create_dataset("/entry/instrument/NDAttributes/OLD_SPDS", data=1.0)
    with h5py.File(eig / "eig_0001.h5", "w") as handle:
        handle.create_dataset("/entry/instrument/NDAttributes/OLD_WPDS", data=1.0)

    app = _app()
    window = dashboard.DashboardWindow()
    window.raw_folder_edit.setText(str(raw))
    for _step in range(2):
        window.builder_next_button.click()
        app.processEvents()

    assert window.builder_stack.currentIndex() == 2
    assert window.pil_monitor_combo.findText("OLD_SPDS") >= 0
    assert window.eig_monitor_combo.findText("OLD_WPDS") >= 0
    sequence_page = window.builder_stack.widget(2)
    calibration_page = window.builder_stack.widget(3)
    assert window.scan_monitor_button in sequence_page.findChildren(QtWidgets.QPushButton)
    assert window.scan_monitor_button not in calibration_page.findChildren(QtWidgets.QPushButton)
    window.close()


def test_raw_file_lists_show_names_but_retain_complete_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    app = _app()
    window = dashboard.DashboardWindow()
    paths = [str(tmp_path / "sample" / "Pil300K" / f"frame_{index:05d}.h5") for index in range(1000)]

    window._set_files_edit(window.pil_files_edit, paths)
    app.processEvents()

    assert window.pil_files_edit.model().rowCount() == 1000
    first = window.pil_files_edit.model().index(0, 0)
    assert first.data() == "frame_00000.h5"
    assert first.data(QtCore.Qt.ToolTipRole) == paths[0]
    assert window._files_from_edit(window.pil_files_edit) == paths
    window.close()


def test_compact_dashboard_drawers_and_single_detector_panels(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    app = _app()
    window = dashboard.DashboardWindow()
    window.show()
    window.tabs.setCurrentIndex(1)
    app.processEvents()

    assert not window.lower_tabs.isVisible()
    window.log_drawer_button.setChecked(True)
    app.processEvents()
    assert window.lower_tabs.isVisible()
    assert window.lower_tabs.currentWidget() is window.log_panel
    window.log_drawer_button.setChecked(False)
    assert not window.lower_tabs.isVisible()

    window.tabs.setCurrentIndex(0)
    window._set_builder_step(0)
    window._set_detector_mode("pil300k")
    app.processEvents()
    assert not window.pil_raw_group.isHidden()
    assert window.eig_raw_group.isHidden()
    assert not window.pil_calibration_group.isHidden()
    assert window.eig_calibration_group.isHidden()
    window.close()


def test_sequence_summary_and_navigation_follow_selected_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "BUILDER_SETTINGS_PATH", tmp_path / "builder.json")
    monkeypatch.setattr(dashboard, "DEFAULT_QUEUE_PATH", tmp_path / "queue.json")
    app = _app()
    window = dashboard.DashboardWindow()
    window.energy_spin.setValue(20)
    window.group_spin.setValue(6)
    window.frame_spin.setValue(100)
    window._set_detector_mode("both")
    window._set_builder_step(2)
    app.processEvents()

    assert "12,000 frame file(s) per detector" in window.sequence_summary_label.text()
    assert "24,000 total" in window.sequence_summary_label.text()
    assert window.builder_back_button.text() == "Back: Task Type"
    assert window.builder_next_button.text() == "Next: Calibration"

    window._set_detector_mode("pil300k")
    assert "12,000 total" in window.sequence_summary_label.text()
    window.close()
