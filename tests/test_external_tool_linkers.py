from pathlib import Path

from aswaxs_live.tools.linkers import contracts as tool_contracts
from aswaxs_live.tools.linkers import xanos as xanos_linker
from aswaxs_live.tools.linkers import xmodfit as xmodfit_linker

def test_xanos_resolve_from_explicit_path(tmp_path: Path) -> None:
    script = tmp_path / "XAnoS_Components.py"
    script.write_text("class XAnoS_Components: pass\n", encoding="utf-8")

    assert xanos_linker.resolve_xanos_components_path(script) == script.resolve()


def test_xmodfit_resolve_from_explicit_path(tmp_path: Path) -> None:
    script = tmp_path / "xmodfit.py"
    script.write_text("print('xmodfit')\n", encoding="utf-8")

    assert xmodfit_linker.resolve_xmodfit_script(script) == script.resolve()


def test_shared_python_launcher_uses_current_python(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "xmodfit.py"
    script.write_text("print('xmodfit')\n", encoding="utf-8")
    calls = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, cwd, env, text):
        calls.append((command, cwd, env, text))
        return FakeProcess()

    monkeypatch.setattr(tool_contracts.subprocess, "Popen", fake_popen)

    process = tool_contracts.launch_python_script(script)

    assert process.poll() is None
    command, cwd, env, text = calls[0]
    assert command[0] == tool_contracts.sys.executable
    assert command[1] == str(script.resolve())
    assert cwd == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in env["PYTHONPATH"]
    assert text is True


def test_xanos_launcher_isolates_qt5_and_forwards_all_data(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "XAnoS_Components.py"
    first = tmp_path / "energy_001.dat"
    second = tmp_path / "energy_002.dat"
    script.write_text("class XAnoS_Components: pass\n", encoding="utf-8")
    first.write_text("data", encoding="utf-8")
    second.write_text("data", encoding="utf-8")
    calls = []

    class FakeProcess:
        pass

    def fake_popen(command, cwd, env, text):
        calls.append((command, cwd, env, text))
        return FakeProcess()

    monkeypatch.setattr(xanos_linker.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("FRAMEBYFRAME_XANOS_PYTHON", r"C:\envs\xanos\python.exe")
    monkeypatch.setenv("QT_PLUGIN_PATH", r"C:\wrong\qt6\plugins")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", r"C:\wrong\qt6\platforms")
    xanos_linker.open_xanos_components_window([first, second], script_path=script)

    command, cwd, env, text = calls[0]
    assert command[0] == r"C:\envs\xanos\python.exe"
    assert command[1:3] == ["-m", "aswaxs_live.tools.linkers.xanos_host"]
    assert command.count("--data") == 2
    assert str(first.resolve()) in command
    assert str(second.resolve()) in command
    assert cwd == str(tmp_path.resolve())
    assert env["QT_API"] == "pyqt5"
    assert "QT_PLUGIN_PATH" not in env
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in env
    assert text is True


def test_xmodfit_launcher_isolates_qt5_and_forwards_data(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "xmodfit.py"
    data = tmp_path / "components.dat"
    script.write_text("class XModFit: pass\n", encoding="utf-8")
    data.write_text("data", encoding="utf-8")
    calls = []

    class FakeProcess:
        pass

    def fake_popen(command, cwd, env, text):
        calls.append((command, cwd, env, text))
        return FakeProcess()

    monkeypatch.setattr(xmodfit_linker.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("FRAMEBYFRAME_XMODFIT_PYTHON", r"C:\envs\xmodfit\python.exe")
    monkeypatch.setenv("QT_PLUGIN_PATH", r"C:\wrong\qt6\plugins")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", r"C:\wrong\qt6\platforms")
    xmodfit_linker.launch_xmodfit(script, data_files=[data])

    command, cwd, env, text = calls[0]
    assert command[0] == r"C:\envs\xmodfit\python.exe"
    assert command[1:3] == ["-m", "aswaxs_live.tools.linkers.xmodfit_host"]
    assert command[-2:] == ["--data", str(data.resolve())]
    assert cwd == str(tmp_path.resolve())
    assert env["QT_API"] == "pyqt5"
    assert "QT_PLUGIN_PATH" not in env
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in env
    assert text is True


def test_shared_environment_uses_current_python_for_both_tools(tmp_path: Path, monkeypatch) -> None:
    xanos_script = tmp_path / "XAnoS_Components.py"
    xmodfit_script = tmp_path / "xmodfit.py"
    xanos_script.write_text("class XAnoS_Components: pass\n", encoding="utf-8")
    xmodfit_script.write_text("class XModFit: pass\n", encoding="utf-8")
    calls = []

    class FakeProcess:
        pass

    def fake_popen(command, cwd, env, text):
        calls.append(command)
        return FakeProcess()

    for name in (
        "FRAMEBYFRAME_XANOS_PYTHON",
        "FRAMEBYFRAME_XMODFIT_PYTHON",
        "FRAMEBYFRAME_QT5_PYTHON",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(xanos_linker.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(xmodfit_linker.subprocess, "Popen", fake_popen)

    xanos_linker.open_xanos_components_window(script_path=xanos_script)
    xmodfit_linker.launch_xmodfit(xmodfit_script)

    assert calls[0][0] == xanos_linker.sys.executable
    assert calls[1][0] == xmodfit_linker.sys.executable
