# Qt 6 Migration Report

## Scope

FrameByFrame-ASWAXS v5 commit `679355e` was copied to the v6 project before
editing. The v5 baseline and final immutability check both passed 79 tests. Its
working tree retained only the two pre-existing modified runtime JSON files.

Version 6 is standardized on Python 3.10 (`>=3.10,<3.11`) and installed in the
existing `pydm-env` environment. That environment may contain PySide6 for
FrameByFrame, PyQt5 for PyDM/XAnoS/XModFit, and PyQt6 for other tools because
FrameByFrame and the external applications load their bindings in separate
processes.

NumPy is constrained to `>=2.1,<2.4` because NumPy 2.4 removes `np.trapz`, which
is used by the concluded v5 scientific implementation. The constraint preserves
that implementation rather than rewriting numerical behavior for the migration.

The unified requirements retain Pylint below 3 because XModFit imports the
legacy `pylint.epylint` module. The isolated XModFit host
preloads the maintained `xraydb` package,
avoiding XModFit's bundled SQLAlchemy 1.x-era database wrapper while leaving
the external source tree unchanged.

The complete `aswaxs_live.reduction` package and the `TaskSpec` scientific data
model are byte-for-byte identical between v5 and v6. Raw HDF5 reads remain
read-only; all derived writes still target analysis/output files.

## API migration

- Centralized first-party binding selection and imports in `app/qt.py`.
- Replaced PyQt5 with PySide6 6.8 and reject preloaded PyQt5/PyQt6.
- Replaced `pyqtSignal`/`pyqtSlot` with `Signal`/`Slot`.
- Replaced Qt5 unscoped enums with Qt6 scoped enums.
- Moved `QAction` use to `QtGui`.
- Replaced `exec_()` with `exec()` in first-party Qt6 code.
- Replaced the Qt5 drop-event position call with the Qt6 position API.
- Replaced Matplotlib `backend_qt5agg`/`Qt5Agg` with `backend_qtagg`/`qtagg`.
- Set `QT_API=pyside6` before Matplotlib, pyqtgraph, or pyFAI GUI imports.
- Retained explicit menu references for PySide6 object ownership.
- Made non-GUI package imports lazy to avoid binding initialization as a side
  effect.

XAnoS and XModFit remain separate installations. Their public linkers are
subprocess-only; optional Qt5 imports exist solely in isolated host modules.
Data-file preload and the XAnoS-to-XModFit save handoff remain in those hosts.

## Validation

- v5 baseline: 79 passed.
- v6 Python 3.10.18 `pydm-env` suite: 89 passed.
- All unified FrameByFrame requirements installed successfully. The inherited
  environment still has unrelated `numba-scipy`/SciPy and Torch/SymPy version
  conflicts that predated this retargeting.
- Qt-binding tests confirm the first-party process contains PySide6 only and
  rejects preloaded PyQt5/PyQt6.
- XAnoS and XModFit widgets both construct successfully offscreen using the
  same Python 3.10.18 executable in separate PyQt5 processes.
- Offscreen launcher smoke: clean exit.
- Standard-DPI GUI capture: all eight required views at 1280x800 and
  1920x1080, zero layout issues.
- Forced 2x high-DPI capture: all views and both logical sizes, zero layout
  issues.
- File-dialog style metrics: 16 px small icons and 24 px list icons.
- Screenshots and JSON reports: `outputs/gui_smoke_qt6` and
  `outputs/gui_smoke_qt6_highdpi`; the `pydm-env` rerun is in
  `outputs/gui_smoke_pydm_env`.

## Remaining compatibility risks

- The shared Python 3.10 `pydm-env` uses the current interpreter automatically.
  For non-shared installations, configure
  `FRAMEBYFRAME_XANOS_PYTHON` and `FRAMEBYFRAME_XMODFIT_PYTHON` when they use
  different environments. `FRAMEBYFRAME_QT5_PYTHON` is the shared fallback.
- External application widget APIs are upstream-owned, so a future upstream
  rename can require a host-adapter update.
- pyFAI GUI command behavior can vary by pyFAI version and graphics driver.
  Software-GL environment controls remain enabled for the integrate subprocess.
- Hardware-display and GPU behavior should still receive a brief workstation
  smoke test even though standard and forced high-DPI offscreen validation pass.

## Launch

```powershell
cd C:\Users\jiajtian\Documents\Playground\ASWAXS_reduction_pipeline_v6
conda activate pydm-env
python run_gui.py
```
