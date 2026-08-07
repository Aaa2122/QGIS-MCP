from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from build_plugin import build_plugin
from qgis_agent_mcp.onboarding import (
    MANAGED_BEGIN,
    AntigravityConnector,
    ClaudeCodeConnector,
    CodexConnector,
    CommandResult,
    CommandRunner,
    CursorConnector,
    LauncherSpec,
    OnboardingError,
    OpenCodeConnector,
    RuntimeManager,
    universal_config,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeQProcess:
    def __init__(self):
        self.process = None
        self.input_data = None
        self.input_sent = False
        self.stdout = b""
        self.stderr = b""

    def start(self, program, arguments):
        self.process = subprocess.Popen(
            [program] + list(arguments),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def waitForStarted(self, timeout):
        return self.process is not None

    def write(self, value):
        self.input_data = bytes(value)

    def closeWriteChannel(self):
        return None

    def waitForFinished(self, timeout):
        payload = self.input_data if not self.input_sent else None
        self.input_sent = True
        try:
            self.stdout, self.stderr = self.process.communicate(
                input=payload, timeout=max(timeout, 1) / 1000
            )
        except subprocess.TimeoutExpired:
            return False
        return True

    def kill(self):
        self.process.kill()

    def readAllStandardOutput(self):
        return self.stdout

    def readAllStandardError(self):
        return self.stderr

    def exitCode(self):
        return self.process.returncode

    @staticmethod
    def errorString():
        return "Process could not start"


def launcher_spec(tmp_path):
    launcher = tmp_path / ".qgis-mcp" / "bin" / "qgis_mcp_launcher.py"
    return LauncherSpec(
        command=sys.executable,
        args=(str(launcher),),
        launcher_path=str(launcher),
        server_root=str(tmp_path / "_server"),
    )


def test_runtime_manager_installs_and_probes_bundled_server(tmp_path):
    plugin_dir = tmp_path / "qgis_agent_mcp"
    server = plugin_dir / "_server" / "qgis_mcp"
    shutil.copytree(ROOT / "src" / "qgis_mcp", server)
    manager = RuntimeManager(
        plugin_dir=plugin_dir,
        home=tmp_path / "home",
        python_executable=sys.executable,
        runner=CommandRunner(process_factory=FakeQProcess),
    )
    spec = manager.ensure()
    assert Path(spec.launcher_path).is_file()
    assert spec.command == str(Path(sys.executable).resolve())
    launcher = Path(spec.launcher_path).read_text(encoding="utf-8")
    assert repr(str((plugin_dir / "_server").resolve())) in launcher


def test_runtime_manager_requires_packaged_server(tmp_path):
    with pytest.raises(OnboardingError, match="not bundled"):
        RuntimeManager(
            plugin_dir=tmp_path / "plugin",
            home=tmp_path,
            python_executable=sys.executable,
        ).ensure()


def test_command_runner_pumps_events_while_waiting():
    pumps = []
    result = CommandRunner(process_factory=FakeQProcess).run(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(0.2); print('ready')",
        ],
        timeout=2,
        event_pump=lambda: pumps.append(True),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ready"
    assert pumps


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command wrapper")
def test_command_runner_supports_cmd_launchers_with_spaces(tmp_path):
    launcher = tmp_path / "Unusual CLI Folder" / "claude.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\r\necho claude-ready\r\n", encoding="utf-8")
    result = CommandRunner(process_factory=FakeQProcess).run([str(launcher)], timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == "claude-ready"


def test_codex_config_is_preserved_backed_up_and_idempotent(tmp_path):
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "gpt-test"\n', encoding="utf-8")
    connector = CodexConnector(home=home)
    spec = launcher_spec(tmp_path)
    connector.install(spec)
    connector.install(spec)
    text = config.read_text(encoding="utf-8")
    assert 'model = "gpt-test"' in text
    assert text.count(MANAGED_BEGIN) == 1
    assert "[mcp_servers.qgis]" in text
    assert connector.status() == "configured"
    assert list(config.parent.glob("config.toml.qgis-mcp-*.bak"))
    connector.remove()
    assert MANAGED_BEGIN not in config.read_text(encoding="utf-8")


def test_codex_refuses_to_overwrite_unmanaged_qgis_entry(tmp_path):
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.qgis]\ncommand = "some-other-server"\n',
        encoding="utf-8",
    )
    with pytest.raises(OnboardingError, match="not created by this plugin"):
        CodexConnector(home=home).install(launcher_spec(tmp_path))


def test_codex_honors_custom_home_and_unusual_unicode_paths(monkeypatch, tmp_path):
    codex_home = tmp_path / "Custom Codex Ω" / "configuration"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    spec = LauncherSpec(
        command=str(tmp_path / "Python Folder" / "python.exe"),
        args=(str(tmp_path / "Zoë User" / "qgis_mcp_launcher.py"),),
        launcher_path="unused",
        server_root="unused",
    )
    connector = CodexConnector()
    connector.install(spec)
    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "Zoë User" in text
    assert "Python Folder" in text


def test_codex_detects_single_quoted_qgis_table(tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "[mcp_servers.'qgis']\ncommand = 'another-server'\n",
        encoding="utf-8",
    )
    with pytest.raises(OnboardingError, match="not created by this plugin"):
        CodexConnector(home=tmp_path).install(launcher_spec(tmp_path))


def test_codex_reports_invalid_utf8_without_overwriting(tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"\xff\xfe")
    with pytest.raises(OnboardingError, match="not valid UTF-8"):
        CodexConnector(home=tmp_path).install(launcher_spec(tmp_path))
    assert config.read_bytes() == b"\xff\xfe"


class FakeRunner:
    def __init__(self):
        self.commands = []

    def run(self, command, input_text=None, timeout=30):
        self.commands.append(list(command))
        if "-c" in command:
            return CommandResult(0, "3.12", "")
        if command[-1] == "--version":
            return CommandResult(0, "2.1.0", "")
        if "get" in command:
            return CommandResult(1, "", "not found")
        return CommandResult(0, "ok", "")


def test_runtime_manager_prefers_standalone_qgis_python(monkeypatch, tmp_path):
    qgis_root = tmp_path / "QGIS"
    qgis_prefix = qgis_root / "apps" / "qgis-ltr"
    qgis_prefix.mkdir(parents=True)
    standalone = qgis_root / "apps" / "Python312" / "python.exe"
    standalone.parent.mkdir(parents=True)
    standalone.write_bytes(b"placeholder")
    broken_path_python = qgis_root / "bin" / "python3.exe"
    broken_path_python.parent.mkdir(parents=True)
    broken_path_python.write_bytes(b"placeholder")
    monkeypatch.setattr("qgis_agent_mcp.onboarding.sys.executable", "qgis-ltr-bin.exe")
    monkeypatch.setattr(
        "qgis_agent_mcp.onboarding.shutil.which",
        lambda name: str(broken_path_python) if name == "python3" else None,
    )
    runner = FakeRunner()
    manager = RuntimeManager(
        plugin_dir=tmp_path / "plugin",
        qgis_prefix=qgis_prefix,
        runner=runner,
    )
    assert manager._find_python() == standalone.resolve()
    assert runner.commands[0][1] == "-I"


def test_claude_connector_uses_user_scoped_stdio_configuration(tmp_path):
    runner = FakeRunner()
    connector = ClaudeCodeConnector(executable="claude", runner=runner)
    connector.install(launcher_spec(tmp_path))
    add = next(command for command in runner.commands if "add" in command)
    assert add[:4] == ["claude", "mcp", "add", "--transport"]
    assert "--scope" in add
    assert "user" in add
    assert "--" in add
    assert sys.executable in add


class ExistingClaudeRunner(FakeRunner):
    def __init__(self, existing):
        super().__init__()
        self.existing = existing

    def run(self, command, input_text=None, timeout=30):
        self.commands.append(list(command))
        if command[-1] == "--version":
            return CommandResult(0, "2.1.0", "")
        if "get" in command:
            return CommandResult(0, self.existing, "")
        return CommandResult(0, "ok", "")


def test_claude_refuses_to_remove_unmanaged_existing_entry(tmp_path):
    runner = ExistingClaudeRunner("command: another-mcp-server")
    connector = ClaudeCodeConnector(executable="claude", runner=runner)
    with pytest.raises(OnboardingError, match="not created by this plugin"):
        connector.install(launcher_spec(tmp_path))
    assert not any("remove" in command for command in runner.commands)


def test_claude_repairs_only_recognized_managed_entry(tmp_path):
    runner = ExistingClaudeRunner(
        "command: python\nargs: C:\\Users\\A\\.qgis-mcp\\bin\\qgis_mcp_launcher.py"
    )
    connector = ClaudeCodeConnector(executable="claude", runner=runner)
    connector.install(launcher_spec(tmp_path))
    assert any("remove" in command for command in runner.commands)
    assert any("add" in command for command in runner.commands)


@pytest.mark.parametrize(
    ("connector_class", "relative_path"),
    [
        (CursorConnector, Path(".cursor") / "mcp.json"),
        (
            AntigravityConnector,
            Path(".gemini") / "config" / "mcp_config.json",
        ),
    ],
)
def test_json_connectors_preserve_config_backup_and_remove(
    connector_class, relative_path, tmp_path
):
    config = tmp_path / relative_path
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"existing": {"command": "tool"}}}),
        encoding="utf-8",
    )
    connector = connector_class(home=tmp_path)
    connector.install(launcher_spec(tmp_path))
    document = json.loads(config.read_text(encoding="utf-8"))
    assert document["theme"] == "dark"
    assert document["mcpServers"]["existing"]["command"] == "tool"
    assert document["mcpServers"]["qgis"]["args"]
    assert connector.status() == "configured"
    assert list(config.parent.glob(config.name + ".qgis-mcp-*.bak"))
    connector.remove()
    document = json.loads(config.read_text(encoding="utf-8"))
    assert "qgis" not in document["mcpServers"]
    assert "existing" in document["mcpServers"]


def test_json_connector_refuses_invalid_json_and_unmanaged_collision(tmp_path):
    config = tmp_path / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text("{invalid", encoding="utf-8")
    connector = CursorConnector(home=tmp_path)
    with pytest.raises(OnboardingError, match="not valid JSON"):
        connector.install(launcher_spec(tmp_path))
    assert config.read_text(encoding="utf-8") == "{invalid"

    config.write_text(
        json.dumps({"mcpServers": {"qgis": {"command": "another-server"}}}),
        encoding="utf-8",
    )
    with pytest.raises(OnboardingError, match="not created by this plugin"):
        connector.install(launcher_spec(tmp_path))


def test_antigravity_keeps_existing_legacy_location(tmp_path):
    legacy = tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    connector = AntigravityConnector(home=tmp_path)
    assert connector.config_path == legacy
    connector.install(launcher_spec(tmp_path))
    assert "qgis" in json.loads(legacy.read_text(encoding="utf-8"))["mcpServers"]


def test_antigravity_detects_existing_ide_specific_location(tmp_path):
    ide_config = tmp_path / ".gemini" / "antigravity-ide" / "mcp_config.json"
    ide_config.parent.mkdir(parents=True)
    ide_config.write_text("{}", encoding="utf-8")
    connector = AntigravityConnector(home=tmp_path)
    assert connector.config_path == ide_config
    assert str(ide_config) in connector.manual_help()


def test_opencode_connector_uses_native_schema_and_preserves_config(tmp_path):
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"theme": "qgis", "mcp": {"existing": {"type": "remote"}}}),
        encoding="utf-8",
    )
    connector = OpenCodeConnector(home=tmp_path)
    connector.install(launcher_spec(tmp_path))
    document = json.loads(config.read_text(encoding="utf-8"))
    assert document["theme"] == "qgis"
    assert document["mcp"]["existing"]["type"] == "remote"
    server = document["mcp"]["qgis"]
    assert server["type"] == "local"
    assert server["enabled"] is True
    assert server["command"][0] == sys.executable
    assert server["command"][1].endswith("qgis_mcp_launcher.py")
    assert connector.status() == "configured"
    connector.remove()
    assert "qgis" not in json.loads(config.read_text(encoding="utf-8"))["mcp"]


def test_opencode_honors_custom_config_and_protects_unmanaged_entry(
    monkeypatch, tmp_path
):
    config = tmp_path / "OpenCode Custom" / "settings.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcp": {"qgis": {"type": "local", "command": ["other"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))
    connector = OpenCodeConnector()
    assert connector.config_path == config
    with pytest.raises(OnboardingError, match="not created by this plugin"):
        connector.install(launcher_spec(tmp_path))


def test_opencode_manual_config_matches_documented_local_format(tmp_path):
    value = json.loads(OpenCodeConnector.manual_config(launcher_spec(tmp_path)))
    server = value["mcp"]["qgis"]
    assert server["type"] == "local"
    assert server["command"][0] == sys.executable


def test_universal_configuration_is_standard_stdio_json(tmp_path):
    value = json.loads(universal_config(launcher_spec(tmp_path)))
    server = value["mcpServers"]["qgis"]
    assert server["type"] == "stdio"
    assert server["command"] == sys.executable
    assert server["args"]


def test_plugin_zip_contains_bundled_server_and_no_markdown(tmp_path):
    output = build_plugin(ROOT, tmp_path / "plugin.zip")
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        metadata = archive.read("qgis_agent_mcp/metadata.txt").decode("utf-8")
    assert "qgis_agent_mcp/metadata.txt" in names
    assert "qgis_agent_mcp/.flake8" not in names
    assert "qgis_agent_mcp/icon.png" in names
    assert "qgis_agent_mcp/LICENSE" in names
    assert "qgis_agent_mcp/_server/qgis_mcp/__main__.py" in names
    assert not any(name.casefold().endswith(".md") for name in names)
    assert not any("__pycache__" in name for name in names)
    assert "qgisMinimumVersion=3.44" in metadata
    assert "qgisMaximumVersion=4.99" in metadata
    assert "experimental=False" in metadata
