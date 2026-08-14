"""Non-invasive linker for the external XAnoS Components GUI."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

os.environ["QT_API"] = "pyqt5"

from PyQt5 import QtCore, QtWidgets

from aswaxs_live.tools.linkers.xmodfit import XModFitLinkerError, open_xmodfit_window
from aswaxs_live.tools.linkers.contracts import ExternalToolError, PROJECT_DIR, resolve_script
from aswaxs_live.tools.linkers.qt5_theme_host import apply_tool_theme, fit_window_to_available_screen


ENV_XANOS_COMPONENTS = "FRAMEBYFRAME_XANOS_COMPONENTS"
LEGACY_ENV_XANOS_COMPONENTS = "ASWAXS_XANOS_COMPONENTS"
LEGACY_XANOS_COMPONENTS_PATH = Path(r"C:\Users\jiajtian\Documents\Playground\XAnoS\XAnoS_Components.py")


class XAnoSLinkerError(ExternalToolError):
    """Raised when the XAnoS component GUI cannot be loaded."""


_xanos_module: ModuleType | None = None


def resolve_xanos_components_path(path: str | Path | None = None) -> Path:
    env_path = os.environ.get(ENV_XANOS_COMPONENTS) or os.environ.get(LEGACY_ENV_XANOS_COMPONENTS)
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            PROJECT_DIR.parent / "XAnoS" / "XAnoS_Components.py",
            PROJECT_DIR.parent.parent / "XAnoS" / "XAnoS_Components.py",
            Path.cwd().parent / "XAnoS" / "XAnoS_Components.py",
            Path.cwd().parent.parent / "XAnoS" / "XAnoS_Components.py",
            LEGACY_XANOS_COMPONENTS_PATH,
        ]
    )
    try:
        return resolve_script(
            explicit=path,
            candidates=candidates,
            tool_name="the XAnoS component GUI",
            env_var=ENV_XANOS_COMPONENTS,
        )
    except ExternalToolError as exc:
        raise XAnoSLinkerError(str(exc)) from exc


def open_xanos_components_window(
    data_files: list[str | Path] | None = None,
    *,
    script_path: str | Path | None = None,
    send_saved_components_to_xmodfit: bool = True,
) -> QtWidgets.QWidget:
    """Open original XAnoS_Components and optionally preload XAnoS-format files."""
    module = _load_xanos_module(resolve_xanos_components_path(script_path))
    widget_class = getattr(module, "XAnoS_Components", None)
    if widget_class is None:
        raise XAnoSLinkerError("The XAnoS script does not define XAnoS_Components.")

    try:
        import pyqtgraph as pg

        pg.setConfigOptions(background="w", foreground="k")
    except Exception:
        pass

    window = widget_class()
    window.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
    window.setWindowTitle("XAnoS Components - ASAXS Component Extraction")
    apply_tool_theme(window)
    fit_window_to_available_screen(window, (1500, 940), minimum=(900, 640), margin=80)
    _install_xanos_component_calculation_defaults(window)
    _set_xanos_loglog_defaults(window)
    if send_saved_components_to_xmodfit:
        _install_xmodfit_save_handoff(window)
    _preload_xanos_data(window, data_files or [])
    _auto_apply_fluorescence_background(window)
    _set_xanos_loglog_defaults(window)
    window.show()
    window.raise_()
    window.activateWindow()
    return window


def _install_xanos_component_calculation_defaults(window: QtWidgets.QWidget) -> None:
    if getattr(window, "_framebyframe_component_defaults_installed", False):
        return
    original_split = getattr(window, "ASAXS_split", None)
    if not callable(original_split):
        return

    def split_without_spike_prompt(*_args: object, **_kwargs: object) -> object:
        original_question = QtWidgets.QMessageBox.question
        xanos_message_box = _xanos_module_message_box(window)
        original_xanos_question = getattr(xanos_message_box, "question", None)

        def default_no_for_spikes(parent: object, title: str, text: str, *q_args: object, **q_kwargs: object) -> object:
            if str(title).strip().lower() == "data spike filter" and "remove data spikes" in str(text).lower():
                return QtWidgets.QMessageBox.No
            return original_question(parent, title, text, *q_args, **q_kwargs)

        QtWidgets.QMessageBox.question = default_no_for_spikes
        if xanos_message_box is not None and callable(original_xanos_question):
            xanos_message_box.question = default_no_for_spikes
        try:
            return original_split()
        finally:
            QtWidgets.QMessageBox.question = original_question
            if xanos_message_box is not None and callable(original_xanos_question):
                xanos_message_box.question = original_xanos_question
            _set_xanos_loglog_defaults(window)

    window.ASAXS_split = split_without_spike_prompt
    setattr(window, "_framebyframe_component_defaults_installed", True)
    calc_button = getattr(window, "calcASAXSPushButton", None)
    if calc_button is not None:
        try:
            calc_button.clicked.disconnect()
        except TypeError:
            pass
        calc_button.clicked.connect(window.ASAXS_split)


def _install_xmodfit_save_handoff(window: QtWidgets.QWidget) -> None:
    if getattr(window, "_framebyframe_xmodfit_handoff_installed", False):
        return
    original_save = getattr(window, "save_ASAXS", None)
    if not callable(original_save):
        return

    def save_and_open_xmodfit() -> None:
        saved_path: str | None = None
        original_get_save_name = QtWidgets.QFileDialog.getSaveFileName
        xanos_file_dialog = _xanos_module_file_dialog(window)
        original_xanos_get_save_name = getattr(xanos_file_dialog, "getSaveFileName", None)

        def tracking_get_save_name(*args: object, **kwargs: object) -> tuple[str, str]:
            nonlocal saved_path
            result = original_get_save_name(*args, **kwargs)
            try:
                saved_path = result[0]
            except Exception:
                saved_path = None
            return result

        QtWidgets.QFileDialog.getSaveFileName = tracking_get_save_name
        if xanos_file_dialog is not None and callable(original_xanos_get_save_name):
            xanos_file_dialog.getSaveFileName = tracking_get_save_name
        save_error: BaseException | None = None
        try:
            original_save()
        except Exception as exc:  # noqa: BLE001
            save_error = exc
        finally:
            QtWidgets.QFileDialog.getSaveFileName = original_get_save_name
            if xanos_file_dialog is not None and callable(original_xanos_get_save_name):
                xanos_file_dialog.getSaveFileName = original_xanos_get_save_name

        if save_error is not None:
            QtWidgets.QMessageBox.warning(
                window,
                "Save Error",
                f"An error occurred while saving ASAXS components:\n\n{save_error}",
            )
            return

        if not saved_path:
            return
        output_path = Path(saved_path)
        if output_path.suffix == "":
            output_path = output_path.with_suffix(".txt")
        if not output_path.exists():
            QtWidgets.QMessageBox.warning(
                window,
                "Cannot Send to XModFit",
                f"The saved file was not found on disk:\n{output_path}\n\n"
                "XModFit will not be opened.",
            )
            return
        try:
            xmodfit_proc = open_xmodfit_window(data_files=[output_path])
            if hasattr(window, "statusBar"):
                window.statusBar().showMessage(
                    f"Saved ASAXS components and opening XModFit: {output_path}"
                )
            _show_xmodfit_launch_tip(window)
            _schedule_xmodfit_health_check(window, xmodfit_proc, output_path)
        except XModFitLinkerError as exc:
            QtWidgets.QMessageBox.warning(
                window,
                "Cannot Send to XModFit",
                f"Saved ASAXS components, but XModFit could not open them.\n\n{exc}",
            )
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                window,
                "Cannot Send to XModFit",
                f"Saved ASAXS components, but an unexpected error prevented XModFit from opening.\n\n{exc}",
            )

    window.save_ASAXS = save_and_open_xmodfit
    setattr(window, "_framebyframe_xmodfit_handoff_installed", True)
    save_button = getattr(window, "saveASAXSPushButton", None)
    if save_button is not None:
        try:
            save_button.clicked.disconnect()
        except TypeError:
            pass
        save_button.clicked.connect(window.save_ASAXS)


def _show_xmodfit_launch_tip(window: QtWidgets.QWidget) -> None:
    """Show a brief tooltip near the Save button confirming XModFit is opening."""
    save_button = getattr(window, "saveASAXSPushButton", None)
    anchor = save_button if save_button is not None else window
    try:
        rect = anchor.rect()
        pos = anchor.mapToGlobal(QtCore.QPoint(rect.center().x(), rect.bottom()))
        QtWidgets.QToolTip.showText(pos, "XModFit is opening...", anchor, rect, 4000)
    except Exception:
        pass


def _schedule_xmodfit_health_check(
    parent: QtWidgets.QWidget,
    proc: object,
    data_path: Path,
    delay_ms: int = 5000,
) -> None:
    """Warn the user if the XModFit subprocess exits within *delay_ms* ms of launch."""
    def _check() -> None:
        rc = getattr(proc, "poll", lambda: None)()
        if rc is None:
            return
        stderr_text = ""
        stderr_fh = getattr(proc, "stderr", None)
        if stderr_fh is not None:
            try:
                import io
                if isinstance(stderr_fh, io.IOBase) and not stderr_fh.closed:
                    stderr_text = stderr_fh.read() or ""
            except Exception:
                pass
        detail = f"\nError output:\n{stderr_text[:800]}\n" if stderr_text.strip() else ""
        QtWidgets.QMessageBox.warning(
            parent,
            "XModFit Closed Unexpectedly",
            f"XModFit exited immediately after launching (exit code: {rc}).{detail}\n"
            f"Saved file is still available at:\n{data_path}\n\n"
            "Check the terminal for more details. "
            "You can open the file in XModFit manually via its Data menu.",
        )

    QtCore.QTimer.singleShot(delay_ms, _check)


def _xanos_module_file_dialog(window: QtWidgets.QWidget) -> object | None:
    module = sys.modules.get(window.__class__.__module__)
    if module is None:
        return None
    return getattr(module, "QFileDialog", None)


def _xanos_module_message_box(window: QtWidgets.QWidget) -> object | None:
    module = sys.modules.get(window.__class__.__module__)
    if module is None:
        return None
    return getattr(module, "QMessageBox", None)


def _preload_xanos_data(window: QtWidgets.QWidget, data_files: list[str | Path]) -> None:
    files = [str(Path(path)) for path in data_files if str(path).strip()]
    if not files:
        return
    import_data = getattr(window, "import_data", None)
    if callable(import_data):
        import_data(files)
        _select_all_imported_data(window)
        _set_xanos_loglog_defaults(window)


def _select_all_imported_data(window: QtWidgets.QWidget) -> None:
    data_list = getattr(window, "dataListWidget", None)
    if data_list is None:
        return
    try:
        data_list.blockSignals(True)
        data_list.selectAll()
        data_list.blockSignals(False)
        data_selection_changed = getattr(window, "dataSelectionChanged", None)
        if callable(data_selection_changed):
            data_selection_changed()
    except Exception:
        try:
            data_list.blockSignals(False)
        except Exception:
            pass


def _auto_apply_fluorescence_background(window: QtWidgets.QWidget) -> None:
    data_list = getattr(window, "dataListWidget", None)
    data = getattr(window, "data", None)
    if data_list is None or not data:
        return
    try:
        range_edit = getattr(window, "xrfBkgRangeLineEdit", None)
        if range_edit is not None:
            q_min, q_max = _high_q_background_range(data)
            if q_min is not None and q_max is not None and q_max > q_min:
                range_edit.setText(f"{q_min:.6g}:{q_max:.6g}")

        checkbox = getattr(window, "xrfBkgCheckBox", None)
        if checkbox is not None:
            checkbox.setEnabled(True)
            checkbox.setChecked(True)

        baseline_row = _row_closest_to_edge(window)
        if baseline_row is None:
            baseline_row = 0 if data_list.count() > 0 else None
        if baseline_row is not None:
            data_list.blockSignals(True)
            data_list.clearSelection()
            data_list.item(baseline_row).setSelected(True)
            data_list.blockSignals(False)
            calc_baseline = getattr(window, "calc_XRF_baseline", None)
            if callable(calc_baseline):
                calc_baseline()
            data_list.selectAll()

        data_selection_changed = getattr(window, "dataSelectionChanged", None)
        if callable(data_selection_changed):
            data_selection_changed()
    except Exception:
        try:
            data_list.blockSignals(False)
        except Exception:
            pass


def _row_closest_to_edge(window: QtWidgets.QWidget) -> int | None:
    data_list = getattr(window, "dataListWidget", None)
    data = getattr(window, "data", None)
    if data_list is None or not data or data_list.count() == 0:
        return None
    try:
        edge_energy = float(getattr(window, "edgeEnergy", 0.0))
    except Exception:
        edge_energy = 0.0
    if edge_energy <= 0:
        return None

    best_row: int | None = None
    best_delta: float | None = None
    for row in range(data_list.count()):
        item = data_list.item(row)
        if item is None or ": " not in item.text():
            continue
        filename = item.text().split(": ", 1)[1]
        record = data.get(filename)
        if not isinstance(record, dict) or "Energy" not in record:
            continue
        try:
            delta = abs(float(record["Energy"]) - edge_energy)
        except Exception:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_row = row
    return best_row


def _high_q_background_range(data: dict[object, object]) -> tuple[float | None, float | None]:
    q_maxima: list[float] = []
    for record in data.values():
        if not isinstance(record, dict) or "x" not in record:
            continue
        try:
            q_values = [float(value) for value in record["x"] if float(value) > 0]
        except Exception:
            continue
        if q_values:
            q_maxima.append(max(q_values))
    if not q_maxima:
        return None, None
    q_max = min(q_maxima)
    return 0.85 * q_max, 0.98 * q_max


def _set_xanos_loglog_defaults(window: QtWidgets.QWidget) -> None:
    for attr in (
        "dataPlotWidget",
        "directComponentPlotWidget",
        "crossComponentPlotWidget",
        "ASAXSPlotWidget",
    ):
        plot_widget = getattr(window, attr, None)
        _set_plot_widget_loglog(plot_widget)


def _set_plot_widget_loglog(plot_widget: object | None) -> None:
    if plot_widget is None:
        return
    for checkbox_name in ("xLogCheckBox", "yLogCheckBox"):
        checkbox = getattr(plot_widget, checkbox_name, None)
        if checkbox is not None:
            previous = checkbox.blockSignals(True)
            checkbox.setChecked(True)
            checkbox.blockSignals(previous)
    selected_names = getattr(plot_widget, "selDataNames", None)
    data = getattr(plot_widget, "data", None)
    if not selected_names or not data:
        return
    update_plot = getattr(plot_widget, "updatePlot", None)
    if callable(update_plot):
        try:
            update_plot()
        except Exception:
            pass


def _load_xanos_module(script_path: Path) -> ModuleType:
    global _xanos_module
    if _xanos_module is not None:
        return _xanos_module

    module_dir = str(script_path.parent)
    added_path = False
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
        added_path = True
    try:
        spec = importlib.util.spec_from_file_location("framebyframe_external_xanos_components", script_path)
        if spec is None or spec.loader is None:
            raise XAnoSLinkerError(f"Cannot load XAnoS script:\n{script_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _xanos_module = module
        return module
    except XAnoSLinkerError:
        raise
    except Exception as exc:  # noqa: BLE001 - report optional external GUI failures.
        raise XAnoSLinkerError(f"Could not start XAnoS Components from:\n{script_path}\n\n{exc}") from exc
    finally:
        if added_path:
            try:
                sys.path.remove(module_dir)
            except ValueError:
                pass


def main() -> int:
    """Run the original XAnoS widget inside this isolated Qt5 host process."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--data", action="append", default=[])
    parser.add_argument("--no-xmodfit-handoff", action="store_true")
    args = parser.parse_args()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("XAnoS Components (FrameByFrame isolated host)")
    window = open_xanos_components_window(
        args.data,
        script_path=args.script,
        send_saved_components_to_xmodfit=not args.no_xmodfit_handoff,
    )
    exit_code = app.exec_()
    # Keep the Python wrapper alive until all queued pyqtgraph/Qt events have
    # finished. Dropping it before exec_() caused native Qt5 access violations.
    window.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
