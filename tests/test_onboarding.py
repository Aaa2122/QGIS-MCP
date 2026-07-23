from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from build_plugin import build_plugin
from qgis_agent_mcp.onboarding import (
    MANAGED_BEGIN,
    ClaudeCodeConnector,
    CodexConnector,
    CommandResult,
    LauncherSpec,
    OnboardingError,
    RuntimeManager,
    universal_config,
)


ROOT = Path(__file__).resolve().parents[1]


def launcher_spec(tmp_path):
    return LauncherSpec(
        command=sys.executable,
        args=(str(tmp_path / "launcher.py"),),
        launcher_path=str(tmp_path / "launcher.py"),
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


class FakeRunner:
    def __init__(self):
        self.commands = []

    def run(self, command, input_text=None, timeout=30):
        self.commands.append(list(command))
        if command[-1] == "--version":
            return CommandResult(0, "2.1.0", "")
        if "get" in command:
            return CommandResult(1, "", "not found")
        return CommandResult(0, "ok", "")


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
    assert "qgis_agent_mcp/metadata.txt" in names
    assert "qgis_agent_mcp/_server/qgis_mcp/__main__.py" in names
    assert not any(name.casefold().endswith(".md") for name in names)
    assert not any("__pycache__" in name for name in names)
