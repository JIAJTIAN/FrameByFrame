"""Launcher bridge for the external sample-position planning app."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from aswaxs_live.paths import PROJECT_DIR

PLATFORM_NAME = os.name
ENV_SAMPLE_POSITION_APP = "FRAMEBYFRAME_SAMPLE_POSITION_APP"


class SamplePositionBridgeError(RuntimeError):
    """Raised when the sample-position app cannot be found or started."""


def resolve_sample_position_app(path: str | Path | None = None) -> Path:
    candidates = [Path(path).expanduser()] if path else _default_sample_position_candidates()
    for candidate in candidates:
        script_path = candidate.resolve()
        if script_path.exists() and script_path.is_file():
            return script_path
    checked = "\n".join(str(candidate) for candidate in candidates)
    if path:
        raise SamplePositionBridgeError(
            "Cannot find the sample-position planning app.\n\n"
            f"Expected script:\n{Path(path).expanduser()}"
        )
    raise SamplePositionBridgeError(
        "Cannot find the sample-position planning app.\n\n"
        f"Checked:\n{checked}\n\n"
        f"Set {ENV_SAMPLE_POSITION_APP} to the app's main.py path if it lives somewhere else."
    )


def launch_sample_position_app(path: str | Path | None = None) -> subprocess.Popen[str]:
    script_path = resolve_sample_position_app(path)
    env = os.environ.copy()
    env["QT_API"] = "pyqt5"
    env.pop("QT_PLUGIN_PATH", None)
    env.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    project_root = str(script_path.parent)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([project_root, existing_pythonpath]) if existing_pythonpath else project_root
    return subprocess.Popen(
        [sys.executable, str(script_path)],
        cwd=project_root,
        env=env,
        text=True,
    )


def _default_sample_position_candidates() -> list[Path]:
    env_path = os.environ.get(ENV_SAMPLE_POSITION_APP)
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            PROJECT_DIR.parent / "ASWAXS_Sample_Position_App" / "main.py",
            PROJECT_DIR.parent.parent / "ASWAXS_Sample_Position_App" / "main.py",
            Path.cwd().parent / "ASWAXS_Sample_Position_App" / "main.py",
            Path.cwd().parent.parent / "ASWAXS_Sample_Position_App" / "main.py",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not _candidate_is_compatible(candidate):
            continue
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _candidate_is_compatible(candidate: Path) -> bool:
    if PLATFORM_NAME != "nt" and re.match(r"^[A-Za-z]:[\\/]", str(candidate)):
        return False
    return True
