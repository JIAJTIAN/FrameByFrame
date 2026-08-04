from pathlib import Path

from aswaxs_live.tools.linkers import sample_position as sample_position_linker


def test_resolve_sample_position_app_from_explicit_path(tmp_path: Path) -> None:
    script = tmp_path / "main.py"
    script.write_text("print('sample position')\n", encoding="utf-8")

    assert sample_position_linker.resolve_sample_position_app(script) == script.resolve()


def test_launch_sample_position_app_uses_current_python(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "main.py"
    script.write_text("print('sample position')\n", encoding="utf-8")
    calls = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, cwd, env, text):
        calls.append((command, cwd, env, text))
        return FakeProcess()

    monkeypatch.setattr(sample_position_linker.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("QT_API", "pyside6")
    monkeypatch.setenv("QT_PLUGIN_PATH", r"C:\wrong\qt6\plugins")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", r"C:\wrong\qt6\platforms")

    process = sample_position_linker.launch_sample_position_app(script)

    assert process.poll() is None
    command, cwd, env, text = calls[0]
    assert command[0] == sample_position_linker.sys.executable
    assert command[1] == str(script.resolve())
    assert cwd == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in env["PYTHONPATH"]
    assert env["QT_API"] == "pyqt5"
    assert "QT_PLUGIN_PATH" not in env
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in env
    assert text is True
