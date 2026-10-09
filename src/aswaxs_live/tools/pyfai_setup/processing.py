"""pyFAI bridge helpers used by the calibration GUI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import fabio
import numpy as np
from pyFAI import load as load_poni

from aswaxs_live.app.qt_runtime import suppress_glx_warning


def export_image_as_edf(path: str | Path, image: np.ndarray, metadata: dict[str, Any] | None = None) -> Path:
    """Write an HDF5 detector image to EDF so pyFAI tools can open it."""
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fabio.edfimage.EdfImage(data=np.asarray(image, dtype=np.float32), header=_edf_header_from_metadata(metadata)).write(str(output))
    return output


def export_mask_as_edf(path: str | Path, mask: np.ndarray) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fabio.edfimage.EdfImage(data=np.asarray(mask, dtype=np.uint8)).write(str(output))
    return output


def load_mask_from_edf(path: str | Path) -> np.ndarray:
    with fabio.open(str(Path(path).expanduser().resolve())) as image:
        data = np.asarray(image.data)
    return data.astype(bool)


def save_mask(path: str | Path, mask: np.ndarray) -> Path:
    output = Path(path).expanduser().resolve()
    np.save(output, mask.astype(np.uint8))
    return output


def load_mask(path: str | Path) -> np.ndarray:
    mask = np.load(Path(path).expanduser().resolve())
    return mask.astype(bool)


def launch_pyfai_calib2(
    edf_path: str | Path,
    calibrant: str = "AgBh",
    detector_name: str | None = None,
    energy_kev: float | None = None,
    distance_m: float | None = None,
    mask_path: str | Path | None = None,
) -> subprocess.Popen:
    """Launch pyFAI-calib2 with the optional detector geometry hints."""
    edf = Path(edf_path).expanduser().resolve()
    command = [sys.executable, "-m", "pyFAI.app.calib2", str(edf), "--calibrant", calibrant]
    if detector_name:
        command.extend(["--detector", str(detector_name)])
    if energy_kev is not None:
        command.extend(["--energy", str(float(energy_kev))])
    if distance_m is not None:
        command.extend(["--dist", str(float(distance_m))])
    if mask_path is not None:
        command.extend(["--mask", str(Path(mask_path).expanduser().resolve())])
    return subprocess.Popen(command, cwd=edf.parent, env=_pyfai_qt_env())


def launch_pyfai_drawmask(edf_path: str | Path) -> subprocess.Popen:
    edf = Path(edf_path).expanduser().resolve()
    command = [sys.executable, "-m", "pyFAI.app.drawmask", str(edf)]
    return subprocess.Popen(command, cwd=edf.parent, env=_pyfai_qt_env())


def write_pyfai_integrate_config(
    path: str | Path,
    poni_path: str | Path | None = None,
    mask_path: str | Path | None = None,
    npt: int = 1000,
    unit: str = "q_A^-1",
    h5_metadata: dict[str, Any] | None = None,
    image_shape: tuple[int, ...] | None = None,
    azimuth_range: tuple[float, float] | None = None,
) -> Path:
    """Write a pyFAI-integrate JSON config linked to HDF5 metadata/PONI/mask."""
    output = Path(path).expanduser().resolve()
    config = {
        "application": "pyfai-integrate",
        "version": 1,
        "nbpt_rad": int(npt),
        "nbpt_azim": 1,
        "unit": unit,
        "do_2D": False,
        "method": "splitbbox",
        "correct_solid_angle": True,
        "error_model": "poisson",
        "extra_options": {
            "source": "FrameByFrame-ASWAXS pyFAI GUI",
            "h5_metadata": _json_safe_metadata(h5_metadata or {}),
        },
    }
    if azimuth_range is not None:
        config["azimuth_range_min"] = float(azimuth_range[0])
        config["azimuth_range_max"] = float(azimuth_range[1])
    if poni_path is not None:
        config["poni"] = str(Path(poni_path).expanduser().resolve())
    else:
        config.update(_geometry_config_from_h5_metadata(h5_metadata or {}, image_shape))
    if mask_path is not None:
        config["do_mask"] = True
        config["mask_file"] = str(Path(mask_path).expanduser().resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
    return output


def launch_pyfai_integrate(
    edf_path: str | Path,
    config_path: str | Path,
    output_dir: str | Path | None = None,
) -> subprocess.Popen:
    """Launch pyFAI-integrate on the exported EDF bridge with a JSON config."""
    edf = Path(edf_path).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    command = [sys.executable, "-m", "pyFAI.app.integrate", "-j", str(config), "--delete"]
    if output_dir is not None:
        command.extend(["--output", str(Path(output_dir).expanduser().resolve())])
    command.append(str(edf))
    return subprocess.Popen(command, cwd=edf.parent, env=_pyfai_qt_env(force_software_gl=True))


def _pyfai_qt_env(force_software_gl: bool = False) -> dict[str, str]:
    """Return a Qt environment suitable for pyFAI tools on remote displays."""
    env = os.environ.copy()
    env["QT_API"] = "pyqt5"
    suppress_glx_warning(env)
    if force_software_gl:
        env["QT_OPENGL"] = "software"
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        env["QT_XCB_GL_INTEGRATION"] = "none"
        env["MESA_LOADER_DRIVER_OVERRIDE"] = "llvmpipe"
        env.setdefault("PYOPENGL_PLATFORM", "egl")
    return env


def _geometry_config_from_h5_metadata(metadata: dict[str, Any], image_shape: tuple[int, ...] | None) -> dict[str, Any]:
    """Build provisional pyFAI geometry from raw HDF5 metadata.

    This is intended as a convenience prefill for pyFAI-integrate. A loaded PONI
    remains the calibrated source of truth when available.
    """
    shape = tuple(int(part) for part in image_shape or () if int(part) > 0)
    pixel_x_m = _micron_to_meter(_optional_float(metadata.get("pixel_size_um")))
    pixel_y_m = _micron_to_meter(_optional_float(metadata.get("pixel_size_y_um"))) or pixel_x_m
    pixel_x_m = pixel_x_m or pixel_y_m
    detector_name = str(metadata.get("detector_name") or "").strip()
    config: dict[str, Any] = {}
    if pixel_x_m and pixel_y_m:
        config["detector"] = "Detector"
        config["pixel1"] = float(pixel_y_m)
        config["pixel2"] = float(pixel_x_m)
        if shape:
            config["shape"] = list(shape)
            config["poni1"] = float(pixel_y_m * shape[0] / 2.0)
            config["poni2"] = float(pixel_x_m * shape[1] / 2.0)
    elif detector_name:
        config["detector"] = detector_name
    distance_m = _integrate_distance_m(metadata, detector_name)
    if distance_m:
        config["dist"] = float(distance_m)
    wavelength_m = _energy_kev_to_wavelength_m(_optional_float(metadata.get("mono_energy_keV")))
    if wavelength_m:
        config["wavelength"] = float(wavelength_m)
    config.setdefault("rot1", 0.0)
    config.setdefault("rot2", 0.0)
    config.setdefault("rot3", 0.0)
    return config


def _integrate_distance_m(metadata: dict[str, Any], detector_name: str) -> float | None:
    detector_text = detector_name.lower()
    if "eiger" in detector_text or "eig" in detector_text:
        waxs_mm = _optional_float(metadata.get("waxs_distance_mm") or metadata.get("waxs_distance"))
        if waxs_mm and waxs_mm > 0:
            return waxs_mm * 1e-3
    distance = _optional_float(metadata.get("distance_m"))
    return distance if distance and distance > 0 else None


def _energy_kev_to_wavelength_m(energy_kev: float | None) -> float | None:
    if energy_kev is None or energy_kev <= 0:
        return None
    return 12.398419843320026 / energy_kev * 1e-10


def _micron_to_meter(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return value * 1e-6


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _edf_header_from_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    if not metadata:
        return {}
    header: dict[str, str] = {}
    for key, value in _json_safe_metadata(metadata).items():
        if isinstance(value, (dict, list)):
            header[f"ASWAXS_{key}"] = json.dumps(value, sort_keys=True)
        else:
            header[f"ASWAXS_{key}"] = str(value)
    return header


def _json_safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, dict):
            safe[key] = _json_safe_metadata(value)
        elif isinstance(value, (list, tuple)):
            safe[key] = [_json_safe_value(item) for item in value]
        else:
            safe[key] = _json_safe_value(value)
    return safe


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def load_poni_summary(path: str | Path) -> str:
    poni_path = Path(path).expanduser().resolve()
    ai = load_poni(str(poni_path))
    return (
        f"dist={float(ai.dist):.6f} m, "
        f"poni1={float(ai.poni1):.6f} m, "
        f"poni2={float(ai.poni2):.6f} m, "
        f"rot1={float(ai.rot1):.6f}, "
        f"rot2={float(ai.rot2):.6f}, "
        f"rot3={float(ai.rot3):.6f}"
    )
