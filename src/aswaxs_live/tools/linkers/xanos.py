"""Subprocess-only linker for the separately installed XAnoS Components GUI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from aswaxs_live.paths import PROJECT_DIR
from aswaxs_live.tools.linkers.contracts import ExternalToolError, resolve_script


ENV_XANOS_COMPONENTS = "FRAMEBYFRAME_XANOS_COMPONENTS"
LEGACY_ENV_XANOS_COMPONENTS = "ASWAXS_XANOS_COMPONENTS"
ENV_XANOS_PYTHON = "FRAMEBYFRAME_XANOS_PYTHON"
ENV_QT5_PYTHON = "FRAMEBYFRAME_QT5_PYTHON"
LEGACY_XANOS_COMPONENTS_PATH = Path(r"C:\Users\jiajtian\Documents\Playground\XAnoS\XAnoS_Components.py")


class XAnoSLinkerError(ExternalToolError):
    """Raised when the isolated XAnoS process cannot be started."""


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
) -> subprocess.Popen[str]:
    """Launch XAnoS in an isolated Qt5 process and preload exported data."""
    script = resolve_xanos_components_path(script_path)
    command = [
        os.environ.get(ENV_XANOS_PYTHON)
        or os.environ.get(ENV_QT5_PYTHON)
        or sys.executable,
        "-m",
        "aswaxs_live.tools.linkers.xanos_host",
        "--script",
        str(script),
    ]
    if not send_saved_components_to_xmodfit:
        command.append("--no-xmodfit-handoff")
    for data_file in data_files or []:
        if str(data_file).strip():
            command.extend(["--data", str(Path(data_file).expanduser().resolve())])
    return _launch_isolated(command, script.parent)


def _launch_isolated(command: list[str], cwd: Path) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["QT_API"] = "pyqt5"
    env.pop("QT_PLUGIN_PATH", None)
    env.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    source_root = str(PROJECT_DIR / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([source_root, existing]) if existing else source_root
    try:
        return subprocess.Popen(command, cwd=str(cwd), env=env, text=True)
    except OSError as exc:
        raise XAnoSLinkerError(f"Could not start isolated XAnoS process:\n{exc}") from exc
