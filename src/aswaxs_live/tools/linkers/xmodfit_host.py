"""Non-invasive launcher/linker for the external XModFit GUI."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
from types import ModuleType

os.environ["QT_API"] = "pyqt5"

from PyQt5 import QtCore, QtWidgets

from aswaxs_live.tools.linkers.contracts import (
    ExternalToolError,
    PROJECT_DIR,
    launch_python_script,
    resolve_script,
)
from aswaxs_live.tools.linkers.qt5_theme_host import apply_tool_theme, fit_window_to_available_screen


ENV_XMODFIT_SCRIPT = "FRAMEBYFRAME_XMODFIT_SCRIPT"
LEGACY_XMODFIT_SCRIPT = Path(r"C:\Users\jiajtian\Documents\XModFit\xmodfit.py")
XMODFIT_LOCAL_MODULES = {
    "Chemical_Formula",
    "Data_Dialog",
    "Fit_Routines",
    "FunctionEditor",
    "Functions",
    "Highlighter",
    "MultiInputDialog",
    "PlotWidget",
    "Structure_Factors",
    "mplWidget",
    "readData",
    "utils",
}


class XModFitLinkerError(ExternalToolError):
    """Raised when XModFit cannot be found or launched."""


_xmodfit_module: ModuleType | None = None
_xmodfit_window: QtWidgets.QWidget | None = None
_xmodfit_root: Path | None = None


def resolve_xmodfit_script(path: str | Path | None = None) -> Path:
    env_path = os.environ.get(ENV_XMODFIT_SCRIPT)
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            PROJECT_DIR.parent / "XModFit" / "xmodfit.py",
            PROJECT_DIR.parent.parent / "XModFit" / "xmodfit.py",
            Path.cwd().parent / "XModFit" / "xmodfit.py",
            Path.cwd().parent.parent / "XModFit" / "xmodfit.py",
            Path.home() / "Documents" / "XModFit" / "xmodfit.py",
            LEGACY_XMODFIT_SCRIPT,
        ]
    )
    try:
        return resolve_script(
            explicit=path,
            candidates=candidates,
            tool_name="XModFit",
            env_var=ENV_XMODFIT_SCRIPT,
        )
    except ExternalToolError as exc:
        raise XModFitLinkerError(str(exc)) from exc


def launch_xmodfit(path: str | Path | None = None, *, data_files: list[str | Path] | None = None) -> subprocess.Popen[str]:
    """Launch original XModFit without modifying its source.

    The original script accepts one data file on the command line. If several
    files are provided, the first file is opened by the external process.
    """
    extra_args = [str(Path(data_files[0]).expanduser())] if data_files else None
    return launch_python_script(resolve_xmodfit_script(path), extra_args=extra_args)


def open_xmodfit_window(
    path: str | Path | None = None,
    *,
    data_files: list[str | Path] | None = None,
) -> QtWidgets.QWidget:
    """Open original XModFit in-process and optionally import data files."""
    global _xmodfit_window
    script_path = resolve_xmodfit_script(path)
    module = _load_xmodfit_module(script_path)
    widget_class = getattr(module, "XModFit", None)
    if widget_class is None:
        raise XModFitLinkerError("The XModFit script does not define XModFit.")
    _install_xmodfit_relative_path_wrappers(widget_class, script_path.parent)

    if _xmodfit_window is None:
        with _xmodfit_runtime_context(script_path.parent):
            _xmodfit_window = widget_class()
        _xmodfit_window.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
        _xmodfit_window.setWindowTitle("XModFit - Model Fitting")
        apply_tool_theme(_xmodfit_window)
        fit_window_to_available_screen(_xmodfit_window, (1500, 940), minimum=(900, 640), margin=80)

    with _xmodfit_runtime_context(script_path.parent):
        _import_xmodfit_data(_xmodfit_window, data_files or [])
    _set_xmodfit_loglog(_xmodfit_window)
    _xmodfit_window.show()
    _xmodfit_window.raise_()
    _xmodfit_window.activateWindow()
    return _xmodfit_window


def _load_xmodfit_module(script_path: Path) -> ModuleType:
    global _xmodfit_module, _xmodfit_root
    if _xmodfit_module is not None:
        return _xmodfit_module

    _prefer_installed_xraydb(script_path.parent)
    module_dir = str(script_path.parent)
    added_path = False
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
        added_path = True
    try:
        spec = importlib.util.spec_from_file_location("framebyframe_external_xmodfit", script_path)
        if spec is None or spec.loader is None:
            raise XModFitLinkerError(f"Cannot load XModFit script:\n{script_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with _xmodfit_runtime_context(script_path.parent):
            spec.loader.exec_module(module)
        _xmodfit_module = module
        _xmodfit_root = script_path.parent
        return module
    except XModFitLinkerError:
        raise
    except Exception as exc:  # noqa: BLE001 - report optional external GUI failures.
        raise XModFitLinkerError(f"Could not start XModFit from:\n{script_path}\n\n{exc}") from exc
    finally:
        if added_path:
            try:
                sys.path.remove(module_dir)
            except ValueError:
                pass


def _install_xmodfit_relative_path_wrappers(widget_class: type, root: Path) -> None:
    """Run XModFit callbacks from its own folder without editing XModFit."""
    if getattr(widget_class, "_framebyframe_cwd_wrapped", False):
        return
    method_names = [
        "create_menus",
        "update_catagories",
        "update_functions",
        "functionChanged",
        "openFunction",
        "addData",
        "openDataDialog",
        "dataFileSelectionChanged",
        "calcConfInterval",
        "openMCMCUserDefinedParam",
        "launch_tApp",
    ]
    for name in method_names:
        method = getattr(widget_class, name, None)
        if callable(method):
            setattr(widget_class, name, _cwd_wrapped_method(method, root))
    setattr(widget_class, "_framebyframe_cwd_wrapped", True)


def _cwd_wrapped_method(method: object, root: Path):
    signature = inspect.signature(method)
    parameters = list(signature.parameters.values())
    positional_capacity = 0
    accepts_varargs = False
    accepts_varkw = False
    accepted_keywords: set[str] = set()
    for index, parameter in enumerate(parameters):
        if index == 0 and parameter.name == "self":
            continue
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            accepts_varargs = True
        elif parameter.kind == inspect.Parameter.VAR_KEYWORD:
            accepts_varkw = True
        elif parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional_capacity += 1
            if parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
                accepted_keywords.add(parameter.name)
        elif parameter.kind == inspect.Parameter.KEYWORD_ONLY:
            accepted_keywords.add(parameter.name)

    def wrapper(self, *args: object, **kwargs: object):
        call_args = args if accepts_varargs else args[:positional_capacity]
        call_kwargs = kwargs if accepts_varkw else {key: value for key, value in kwargs.items() if key in accepted_keywords}
        with _xmodfit_runtime_context(root):
            return method(self, *call_args, **call_kwargs)

    wrapper.__name__ = getattr(method, "__name__", "framebyframe_cwd_wrapper")
    wrapper.__doc__ = getattr(method, "__doc__", None)
    return wrapper


def _import_xmodfit_data(window: QtWidgets.QWidget, data_files: list[str | Path]) -> None:
    files = [str(Path(path).expanduser()) for path in data_files if str(path).strip()]
    if not files:
        return
    add_data = getattr(window, "addData", None)
    if not callable(add_data):
        raise XModFitLinkerError("XModFit does not expose addData(fnames=...).")
    if len(files) == 1:
        _add_single_file_without_modal_dialog(window, files[0], add_data)
    else:
        add_data(fnames=files)


def _add_single_file_without_modal_dialog(window: QtWidgets.QWidget, filename: str, add_data: object) -> None:
    module = sys.modules.get(window.__class__.__module__)
    data_dialog_class = getattr(module, "Data_Dialog", None) if module is not None else None
    original_exec = getattr(data_dialog_class, "exec_", None)
    if data_dialog_class is None or not callable(original_exec):
        add_data(fnames=[filename])
        return

    def auto_accept_exec(dialog: QtWidgets.QDialog) -> int:
        _configure_xanos_component_import(dialog)
        dialog.accept()
        return int(QtWidgets.QDialog.Accepted)

    data_dialog_class.exec_ = auto_accept_exec
    try:
        add_data(fnames=[filename])
    finally:
        data_dialog_class.exec_ = original_exec


def _configure_xanos_component_import(dialog: QtWidgets.QDialog) -> None:
    data = getattr(dialog, "data", None)
    if not isinstance(data, dict) or "data" not in data:
        return
    try:
        columns = list(data["data"].columns)
    except Exception:
        return
    q_index = _first_column_index(columns, ["Q(inv Angs)", "q", "Q", "q(A^-1)"])
    if q_index is None:
        q_index = 0

    component_specs = [
        ("SAXS-term", "SAXS-term_err"),
        ("Cross-term", "Cross-term_err"),
        ("Resonant-term", "Resonant-term_err"),
    ]
    available_specs = [
        (columns.index(y_name), columns.index(err_name) + 1 if err_name in columns else 0)
        for y_name, err_name in component_specs
        if y_name in columns
    ]
    if not available_specs:
        return

    remove_all = getattr(dialog, "removeAllPlots", None)
    add_plots = getattr(dialog, "addPlots", None)
    update_plot = getattr(dialog, "updatePlot", None)
    if not callable(add_plots):
        return
    try:
        if callable(remove_all):
            remove_all()
        for y_index, yerr_combo_index in available_specs:
            add_plots(plotIndex=[q_index, y_index, yerr_combo_index])
        if callable(update_plot):
            update_plot()
    except Exception:
        return


def _first_column_index(columns: list[object], names: list[str]) -> int | None:
    normalized = {str(column).strip().lower(): index for index, column in enumerate(columns)}
    for name in names:
        index = normalized.get(name.strip().lower())
        if index is not None:
            return index
    return None


def _set_xmodfit_loglog(window: QtWidgets.QWidget) -> None:
    plot_widget = getattr(window, "plotWidget", None)
    try:
        data_list = getattr(window, "dataListWidget", None)
        if plot_widget is not None and data_list is not None and data_list.count() > 0:
            for attr in ("xLogCheckBox", "yLogCheckBox"):
                checkbox = getattr(plot_widget, attr, None)
                if checkbox is not None:
                    checkbox.setChecked(True)
            if not data_list.selectedItems():
                data_list.setCurrentRow(data_list.count() - 1)
            selection_changed = getattr(window, "dataFileSelectionChanged", None)
            if callable(selection_changed):
                selection_changed()
            plot_names = getattr(window, "pfnames", None)
            if plot_names:
                plot_widget.Plot(plot_names)
    except Exception:
        pass


@contextmanager
def _temporary_cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextmanager
def _xmodfit_runtime_context(root: Path):
    root = root.expanduser().resolve()
    previous = Path.cwd()
    root_text = str(root)
    inserted_path = False
    if not sys.path or sys.path[0] != root_text:
        sys.path.insert(0, root_text)
        inserted_path = True
    _prefer_xmodfit_local_modules(root)
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous)
        if inserted_path:
            try:
                sys.path.remove(root_text)
            except ValueError:
                pass


def _prefer_xmodfit_local_modules(root: Path) -> None:
    """Evict same-named modules loaded from XAnoS/other tools."""
    for module_name in XMODFIT_LOCAL_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        if not _module_origin_is_under(module, root):
            del sys.modules[module_name]


def _prefer_installed_xraydb(root: Path) -> None:
    """Load the maintained xraydb package instead of XModFit's old vendored copy.

    XModFit's bundled copy relies on SQLAlchemy 1.x internals.  Preloading the
    installed package lets the unchanged application use its public ``XrayDB``
    API while the shared Python 3.10 environment retains SQLAlchemy 2.
    """
    root = root.expanduser().resolve()
    loaded = sys.modules.get("xraydb")
    if loaded is not None and not _module_origin_is_under(loaded, root):
        return

    for module_name, module in list(sys.modules.items()):
        if (module_name == "xraydb" or module_name.startswith("xraydb.")) and _module_origin_is_under(module, root):
            del sys.modules[module_name]

    original_path = list(sys.path)
    try:
        sys.path[:] = [entry for entry in original_path if not _path_resolves_to(entry, root)]
        module = importlib.import_module("xraydb")
    except ImportError as exc:
        raise XModFitLinkerError(
            "XModFit requires the maintained 'xraydb' package in the shared Python environment."
        ) from exc
    finally:
        sys.path[:] = original_path

    if _module_origin_is_under(module, root):
        raise XModFitLinkerError(
            "XModFit loaded its obsolete bundled xraydb copy instead of the installed package."
        )


def _path_resolves_to(entry: str, target: Path) -> bool:
    try:
        candidate = Path(entry).expanduser().resolve() if entry else Path.cwd().resolve()
        return candidate == target
    except OSError:
        return False


def _module_origin_is_under(module: ModuleType, root: Path) -> bool:
    origin = getattr(module, "__file__", None)
    if not origin:
        return False
    try:
        Path(origin).expanduser().resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def main() -> int:
    """Run the original XModFit widget inside this isolated Qt5 host process."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--data", action="append", default=[])
    args = parser.parse_args()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("XModFit (FrameByFrame isolated host)")
    open_xmodfit_window(args.script, data_files=args.data)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
