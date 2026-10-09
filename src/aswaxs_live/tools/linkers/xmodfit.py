"""Subprocess-only launcher for the separately installed XModFit GUI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from aswaxs_live.paths import PROJECT_DIR
from aswaxs_live.tools.linkers.contracts import ExternalToolError, resolve_script


ENV_XMODFIT_SCRIPT = "FRAMEBYFRAME_XMODFIT_SCRIPT"
ENV_XMODFIT_PYTHON = "FRAMEBYFRAME_XMODFIT_PYTHON"
ENV_QT5_PYTHON = "FRAMEBYFRAME_QT5_PYTHON"
LEGACY_XMODFIT_SCRIPT = Path(r"C:\Users\jiajtian\Documents\XModFit\xmodfit.py")


class XModFitLinkerError(ExternalToolError):
    """Raised when the isolated XModFit process cannot be started."""


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


def launch_xmodfit(
    path: str | Path | None = None,
    *,
    data_files: list[str | Path] | None = None,
) -> subprocess.Popen[str]:
    """Launch XModFit in an isolated Qt5 process and preload all supplied data."""
    script = resolve_xmodfit_script(path)
    command = [
        os.environ.get(ENV_XMODFIT_PYTHON)
        or os.environ.get(ENV_QT5_PYTHON)
        or sys.executable,
        "-m",
        "aswaxs_live.tools.linkers.xmodfit_host",
        "--script",
        str(script),
    ]
    for data_file in data_files or []:
        if str(data_file).strip():
            command.extend(["--data", str(Path(data_file).expanduser().resolve())])
    env = os.environ.copy()
    env["QT_API"] = "pyqt5"
    env.pop("QT_PLUGIN_PATH", None)
    env.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    source_root = str(PROJECT_DIR / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([source_root, existing]) if existing else source_root
    try:
        return subprocess.Popen(command, cwd=str(script.parent), env=env, text=True)
    except OSError as exc:
        raise XModFitLinkerError(f"Could not start isolated XModFit process:\n{exc}") from exc


def open_xmodfit_window(
    path: str | Path | None = None,
    *,
    data_files: list[str | Path] | None = None,
) -> subprocess.Popen[str]:
    """Compatibility alias; the window is always hosted out of process in v6."""
    return launch_xmodfit(path, data_files=data_files)
