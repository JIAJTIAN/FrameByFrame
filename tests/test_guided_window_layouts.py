import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aswaxs_live.app.qt import QtWidgets

from aswaxs_live.tools.iq_viewer.viewer import H5IqViewerDialog
from aswaxs_live.tools.online_reducer.app import MainWindow as OnlineReducerWindow
from aswaxs_live.tools.pyfai_setup.gui import PreprocessingWindow
from aswaxs_live.tools.rack_builder.app import RackBuilderDialog


_APP: QtWidgets.QApplication | None = None


def _app() -> QtWidgets.QApplication:
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def test_pyfai_setup_uses_four_clickable_guided_steps() -> None:
    app = _app()
    window = PreprocessingWindow()
    window.show()
    app.processEvents()

    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == [
        "1. Load",
        "2. Calibrate",
        "3. Mask",
        "4. Integrate",
    ]
    assert window.tabs.currentIndex() == 0
    assert not window.step_back_button.isEnabled()
    file_menu = window.file_menu
    assert "Open HDF5..." in [action.text() for action in file_menu.actions()]
    assert window.file_path_edit.isHidden()
    window.step_next_button.click()
    assert window.tabs.currentIndex() == 1
    window._set_step(3)
    assert window.step_next_button.isHidden()
    window.close()


def test_iq_viewer_embeds_compact_qc_and_collapsible_curve_controls() -> None:
    app = _app()
    dialog = H5IqViewerDialog()
    dialog.resize(1000, 700)
    dialog.show()
    app.processEvents()

    assert dialog.minimumSizeHint().width() <= 1000
    assert dialog.frame_stability_widget.compact
    assert not dialog.frame_stability_widget.controls_widget.isVisible()
    qc = dialog.frame_stability_widget
    qc.group_combo.addItems(["E001 G001", "E001 G002", "E001 G003"])
    qc._update_series_navigation()
    qc.next_series_button.click()
    assert qc.group_combo.currentIndex() == 1
    qc.previous_series_button.click()
    assert qc.group_combo.currentIndex() == 0
    qc.previous_series_button.click()
    assert qc.group_combo.currentIndex() == 2
    dialog.curve_controls_action.setChecked(False)
    app.processEvents()
    assert not dialog.curve_controls_panel.isVisible()
    dialog.viewer_tabs.setCurrentIndex(1)
    assert not dialog.curve_controls_action.isEnabled()
    dialog.close()


def test_rack_editor_uses_compact_two_column_actions() -> None:
    app = _app()
    dialog = RackBuilderDialog(group_count=13)
    dialog.show()
    app.processEvents()

    editor = next(box for box in dialog.findChildren(QtWidgets.QGroupBox) if box.title() == "Selected Capillary")
    assert editor.maximumWidth() == 360
    assert dialog.minimumSizeHint().height() < 500
    file_menu = dialog.file_menu
    assert "Rack Setup..." in [action.text() for action in file_menu.actions()]
    assert not hasattr(dialog, "group_count_spin")
    dialog.close()


def test_online_reducer_setup_is_accessed_from_file_menu() -> None:
    app = _app()
    window = OnlineReducerWindow()
    window.show()
    app.processEvents()

    file_menu = window.file_menu
    assert "Acquisition Setup..." in [action.text() for action in file_menu.actions()]
    assert not window.setup_scroll.isVisible()
    window.setup_action.setChecked(True)
    app.processEvents()
    assert window.setup_scroll.isVisible()
    window.close()
