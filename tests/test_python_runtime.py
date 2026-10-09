from __future__ import annotations

from types import SimpleNamespace

import pytest

from aswaxs_live.app.launcher import require_python_310


def test_python_310_is_supported() -> None:
    require_python_310(SimpleNamespace(major=3, minor=10))


@pytest.mark.parametrize("minor", [9, 11, 12])
def test_other_python_minors_are_rejected(minor: int) -> None:
    with pytest.raises(RuntimeError, match="requires Python 3.10"):
        require_python_310(SimpleNamespace(major=3, minor=minor))
