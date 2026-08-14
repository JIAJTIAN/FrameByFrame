from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass
class OnlineConfig:
    sample_name: str = "online_aswaxs"
    saxs_endpoint: str = "tcp://164.54.169.92:5551"
    waxs_endpoint: str = "tcp://164.54.169.92:5552"
    pil300k_poni: str = ""
    pil300k_mask: str = ""
    eig1m_poni: str = ""
    eig1m_mask: str = ""
    num_frames: int = 1
    dataset_path: str = "entry/data/data"
    pil300k_monitor_key: str = "SPDS"
    eig1m_monitor_key: str = "WPDS"
    npt: int = 1000
    settle_seconds: float = 0.2

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "OnlineConfig":
        if not path.is_file():
            return cls()
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = cls.__dataclass_fields__
        return cls(**{key: value for key, value in values.items() if key in known})
