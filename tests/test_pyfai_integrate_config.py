import json
from pathlib import Path

import numpy as np
from pyFAI.io.integration_config import WorkerConfig

from aswaxs_live.tools.pyfai_setup.processing import _pyfai_qt_env, export_image_as_edf, write_pyfai_integrate_config


def test_pyfai_integrate_config_prefills_geometry_from_h5_metadata(tmp_path: Path):
    metadata = {
        "mono_energy_keV": 12.0,
        "distance_m": 5.6,
        "pixel_size_um": 172.0,
        "pixel_size_y_um": 172.0,
        "detector_name": "Pilatus300k",
        "motor_readings": {"SD_X": 1.2, "SD_Y": 3.4},
    }

    config_path = write_pyfai_integrate_config(
        tmp_path / "from_h5.azimint.json",
        h5_metadata=metadata,
        image_shape=(619, 487),
        npt=1500,
        unit="q_A^-1",
        azimuth_range=(-30.0, 45.0),
    )

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["dist"] == 5.6
    assert np.isclose(raw["pixel1"], 172.0e-6)
    assert np.isclose(raw["pixel2"], 172.0e-6)
    assert raw["shape"] == [619, 487]
    assert np.isclose(raw["poni1"], 172.0e-6 * 619 / 2.0)
    assert np.isclose(raw["poni2"], 172.0e-6 * 487 / 2.0)
    assert raw["extra_options"]["h5_metadata"]["motor_readings"]["SD_X"] == 1.2
    assert raw["azimuth_range_min"] == -30.0
    assert raw["azimuth_range_max"] == 45.0

    parsed = WorkerConfig.from_dict(raw).as_dict()
    assert parsed["nbpt_rad"] == 1500
    assert parsed["unit"] == "q_A^-1"
    assert parsed["poni"]["dist"] == 5.6
    assert parsed["poni"]["wavelength"] > 0


def test_exported_edf_carries_h5_metadata_header(tmp_path: Path):
    path = export_image_as_edf(
        tmp_path / "bridge.edf",
        np.ones((4, 5), dtype=np.float32),
        metadata={"mono_energy_keV": 12.0, "motor_readings": {"SPDS": 42}},
    )

    text = path.read_text(encoding="latin-1", errors="ignore")
    assert "ASWAXS_mono_energy_keV" in text
    assert "ASWAXS_motor_readings" in text


def test_pyfai_integrate_env_forces_software_gl():
    env = _pyfai_qt_env(force_software_gl=True)

    assert env["QT_API"] == "pyqt5"
    assert env["QT_OPENGL"] == "software"
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert env["QT_XCB_GL_INTEGRATION"] == "none"
