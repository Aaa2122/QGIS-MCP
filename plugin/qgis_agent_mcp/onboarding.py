from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "qgis"
MANAGED_BEGIN = "# BEGIN QGIS Agent MCP (managed automatically)"
MANAGED_END = "# END QGIS Agent MCP"


class OnboardingError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def __init__(self, process_factory=None):
        self.process_factory = process_factory

    def run(self, command, input_text=None, timeout=30, event_pump=None):
        command = [str(item) for item in command]
        if not command:
            return CommandResult(-1, "", "Command is empty")
        if os.name == "nt" and Path(command[0]).suffix.casefold() in {".bat", ".cmd"}:
            command = [
                os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
                "/d",
                "/s",
                "/c",
                "call",
            ] + command
        if self.process_factory is None:
            from qgis.PyQt.QtCore import QProcess, QProcessEnvironment

            process = QProcess()
            environment = QProcessEnvironment.systemEnvironment()
            # QGIS launchers inject their embedded Python paths into the process
            # environment. Passing these to a standalone Python can hang or load
            # binary-incompatible modules before the MCP server starts.
            for name in (
                "PYTHONHOME",
                "PYTHONPATH",
                "QGIS_PREFIX_PATH",
                "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH",
            ):
                environment.remove(name)
            executable_name = Path(command[0]).stem.casefold()
            if executable_name.startswith("python"):
                system_root = environment.value("SystemRoot") or os.environ.get(
                    "SystemRoot", r"C:\Windows"
                )
                python_dir = str(Path(command[0]).resolve().parent)
                environment.insert(
                    "PATH",
                    os.pathsep.join(
                        [
                            python_dir,
                            str(Path(python_dir) / "Scripts"),
                            str(Path(system_root) / "System32"),
                            str(Path(system_root)),
                        ]
                    ),
                )
                environment.insert("PYTHONNOUSERSITE", "1")
                environment.insert("PYTHONUTF8", "1")
            process.setProcessEnvironment(environment)
        else:
            process = self.process_factory()
        process.start(command[0], command[1:])
        timeout_ms = max(1, int(float(timeout) * 1000))
        if not process.waitForStarted(min(timeout_ms, 5000)):
            return CommandResult(-1, "", process.errorString())
        if input_text is not None:
            process.write(str(input_text).encode("utf-8"))
            process.closeWriteChannel()
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.waitForFinished(1000)
                message = "Command timed out after {} seconds".format(timeout)
                return CommandResult(
                    -1,
                    _qprocess_text(process.readAllStandardOutput()),
                    _qprocess_text(process.readAllStandardError()) + message,
                )
            if process.waitForFinished(max(1, min(50, int(remaining * 1000)))):
                return CommandResult(
                    int(process.exitCode()),
                    _qprocess_text(process.readAllStandardOutput()),
                    _qprocess_text(process.readAllStandardError()),
                )
            if event_pump is not None:
                event_pump()


@dataclass(frozen=True)
class LauncherSpec:
    command: str
    args: tuple
    launcher_path: str
    server_root: str

    def command_line(self):
        return [self.command] + list(self.args)

    def mcp_server(self):
        return {
            "type": "stdio",
            "command": self.command,
            "args": list(self.args),
            "env": {},
        }


class RuntimeManager:
    """Installs a stable launcher script for the server bundled in the plugin."""

    def __init__(
        self,
        plugin_dir,
        qgis_prefix=None,
        home=None,
        python_executable=None,
        runner=None,
    ):
        self.plugin_dir = Path(plugin_dir).resolve()
        self.server_root = self.plugin_dir / "_server"
        self.qgis_prefix = Path(qgis_prefix).resolve() if qgis_prefix else None
        self.home = Path(home).expanduser() if home else Path.home()
        self.python_executable = python_executable
        self.runner = runner or CommandRunner()

    @property
    def launcher_path(self):
        return self.home / ".qgis-mcp" / "bin" / "qgis_mcp_launcher.py"

    def ensure(self, event_pump=None):
        main_module = self.server_root / "qgis_mcp" / "__main__.py"
        if not main_module.is_file():
            raise OnboardingError(
                "The MCP runtime is not bundled with this plugin installation. "
                "Reinstall QGIS Agent MCP from an official package."
            )
        event_pump = event_pump or _qgis_event_pump()
        python = self._find_python(event_pump=event_pump)
        launcher = self.launcher_path
        launcher.parent.mkdir(parents=True, exist_ok=True)
        source = (
            "# Generated by QGIS Agent MCP. Do not edit.\n"
            "import runpy\n"
            "import sys\n"
            "server_root = {!r}\n"
            "if server_root not in sys.path:\n"
            "    sys.path.insert(0, server_root)\n"
            "runpy.run_module('qgis_mcp', run_name='__main__')\n"
        ).format(str(self.server_root))
        _atomic_write(launcher, source)
        spec = LauncherSpec(
            command=str(python),
            args=(str(launcher),),
            launcher_path=str(launcher),
            server_root=str(self.server_root),
        )
        probe_kwargs = {"timeout": 30}
        if event_pump is not None:
            probe_kwargs["event_pump"] = event_pump
        probe = self.runner.run(spec.command_line() + ["--help"], **probe_kwargs)
        if probe.returncode != 0:
            raise OnboardingError(
                "The bundled MCP launcher could not start with {}: {}".format(
                    python, _result_message(probe)
                )
            )
        return spec

    def _find_python(self, event_pump=None):
        candidates = []
        configured = self.python_executable or os.environ.get(
            "QGIS_MCP_LAUNCHER_PYTHON"
        )
        if configured:
            candidates.append(Path(configured))
        executable = Path(sys.executable)
        if executable.stem.casefold().startswith("python"):
            candidates.append(executable)
        if self.qgis_prefix:
            roots = [self.qgis_prefix]
            roots.extend(list(self.qgis_prefix.parents)[:3])
            for root in roots:
                candidates.extend(root.glob("apps/Python*/python.exe"))
                candidates.extend(root.glob("bin/python3"))
        for name in ("python3", "python"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        seen = set()
        for candidate in candidates:
            key = str(candidate).casefold()
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            probe_kwargs = {"timeout": 10}
            if event_pump is not None:
                probe_kwargs["event_pump"] = event_pump
            probe = self.runner.run(
                [
                    str(candidate),
                    "-I",
                    "-c",
                    "import sys; print('%s.%s' % sys.version_info[:2])",
                ],
                **probe_kwargs,
            )
            if probe.returncode != 0:
                continue
            try:
                major, minor = [int(value) for value in probe.stdout.strip().split(".")[:2]]
            except (TypeError, ValueError):
                continue
            if (major, minor) >= (3, 10):
                return candidate.resolve()
        raise OnboardingError(
            "No compatible Python 3.10+ runtime was found. Install the standalone "
            "QGIS MCP launcher or set QGIS_MCP_LAUNCHER_PYTHON."
        )


def _qgis_event_pump():
    try:
        from qgis.PyQt.QtCore import QCoreApplication, QEventLoop

        if QCoreApplication.instance() is not None:
            return lambda: QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25
            )
    except (ImportError, RuntimeError):
        pass
    return None


class CodexConnector:
    name = "Codex"
    description = "Codex app, CLI and IDE extension"
    restart_hint = (
        "Quit every open Codex app, CLI session or IDE window, then reopen it. "
        "The QGIS tools will appear in the new session."
    )
    requires_executable = False

    def manual_help(self):
        return (
            "Codex configuration file:\n{}\n\nIf Codex uses a custom location, "
            "set CODEX_HOME before starting QGIS. Check that this file and its "
            "parent folder are writable, then retry Connect / Repair."
        ).format(self.config_path)

    def __init__(self, home=None, config_path=None):
        self.home = Path(home).expanduser() if home else Path.home()
        configured_home = os.environ.get("CODEX_HOME")
        codex_home = (
            Path(configured_home).expanduser()
            if configured_home and home is None
            else self.home / ".codex"
        )
        self.config_path = (
            Path(config_path).expanduser() if config_path else codex_home / "config.toml"
        )

    def detected(self):
        return bool(shutil.which("codex")) or self.config_path.parent.exists()

    def status(self):
        if not self.config_path.is_file():
            return "not_configured"
        text = _read_text(self.config_path, self.name)
        has_begin = MANAGED_BEGIN in text
        has_end = MANAGED_END in text
        if has_begin != has_end:
            return "conflict"
        return "configured" if has_begin else "not_configured"

    def install(self, spec):
        command = _toml_string(spec.command)
        args = ", ".join(_toml_string(item) for item in spec.args)
        block = (
            "{}\n"
            "[mcp_servers.{}]\n"
            "command = {}\n"
            "args = [{}]\n"
            "{}"
        ).format(MANAGED_BEGIN, SERVER_NAME, command, args, MANAGED_END)
        _update_codex_config(self.config_path, block)
        return (
            "Codex configured for the current user. Restart the Codex app, CLI, "
            "or IDE extension if it is already open."
        )

    def remove(self):
        if not self.config_path.is_file():
            return "Codex was not configured."
        text = _read_text(self.config_path, self.name)
        updated = _remove_managed_block(text)
        if updated == text:
            return "No QGIS-managed Codex configuration was found."
        _backup_and_write(self.config_path, updated)
        return "QGIS MCP was removed from Codex."


class ClaudeCodeConnector:
    name = "Claude Code"
    description = "Claude Code CLI"
    restart_hint = (
        "Close the current Claude Code session and start a new one. Run /mcp "
        "in the new session to see and verify the QGIS server."
    )
    requires_executable = True

    def manual_help(self):
        location = self.executable or "not found"
        return (
            "Detected Claude executable:\n{}\n\nIf this is incorrect, choose "
            "Options -> Locate Claude and select claude.exe, claude.cmd or the "
            "Claude launcher. You can also set CLAUDE_CODE_EXECUTABLE before "
            "starting QGIS."
        ).format(location)

    def __init__(self, executable=None, runner=None, home=None):
        self.home = Path(home).expanduser() if home else Path.home()
        self.executable = self._find_executable(executable)
        self.runner = runner or CommandRunner()

    def set_executable(self, executable):
        candidate = Path(executable).expanduser()
        if not candidate.is_file():
            raise OnboardingError(
                "The selected Claude Code executable does not exist: {}".format(candidate)
            )
        self.executable = str(candidate.resolve())
        if not self.detected():
            self.executable = None
            raise OnboardingError(
                "The selected file could not run Claude Code. Select the Claude "
                "executable itself, not its containing folder."
            )

    def _find_executable(self, explicit=None):
        if explicit:
            explicit_path = Path(explicit).expanduser()
            return (
                str(explicit_path.resolve())
                if explicit_path.is_file()
                else str(explicit)
            )
        configured = os.environ.get("CLAUDE_CODE_EXECUTABLE")
        candidates = []
        if configured:
            candidates.append(configured)
        found = shutil.which("claude")
        if found:
            candidates.append(found)
        candidates.extend(
            [
                self.home / ".local" / "bin" / "claude",
                self.home / ".local" / "bin" / "claude.exe",
                self.home / "AppData" / "Local" / "Programs" / "Claude" / "claude.exe",
                self.home / "AppData" / "Roaming" / "npm" / "claude.cmd",
            ]
        )
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if path.is_file():
                return str(path.resolve())
        return None

    def detected(self):
        if not self.executable:
            return False
        result = self.runner.run([self.executable, "--version"], timeout=10)
        return result.returncode == 0

    def status(self):
        if not self.executable:
            return "not_detected"
        result = self.runner.run(
            [self.executable, "mcp", "get", SERVER_NAME], timeout=15
        )
        if result.returncode != 0:
            return "not_configured"
        details = "{}\n{}".format(result.stdout, result.stderr)
        return "configured" if _looks_like_managed_launcher(details) else "conflict"

    def install(self, spec):
        if not self.detected():
            raise OnboardingError(
                "Claude Code was not detected. Use “Locate Claude…” and select "
                "the executable if it is installed outside PATH."
            )
        existing = self.runner.run(
            [self.executable, "mcp", "get", SERVER_NAME], timeout=15
        )
        if existing.returncode == 0:
            existing_details = "{}\n{}".format(existing.stdout, existing.stderr)
            if not _looks_like_managed_launcher(existing_details):
                raise OnboardingError(
                    "Claude Code already contains a QGIS server that was not "
                    "created by this plugin. Remove or rename it before connecting."
                )
            removed = self.runner.run(
                [
                    self.executable,
                    "mcp",
                    "remove",
                    "--scope",
                    "user",
                    SERVER_NAME,
                ],
                timeout=15,
            )
            if removed.returncode != 0:
                raise OnboardingError(
                    "Claude Code could not replace the previous managed QGIS "
                    "entry: {}".format(_result_message(removed))
                )
        command = [
            self.executable,
            "mcp",
            "add",
            "--transport",
            "stdio",
            "--scope",
            "user",
            SERVER_NAME,
            "--",
            spec.command,
        ] + list(spec.args)
        result = self.runner.run(command, timeout=30)
        if result.returncode != 0:
            raise OnboardingError(
                "Claude Code could not be configured: {}".format(
                    _result_message(result)
                )
            )
        return (
            "Claude Code configured for the current user. Start a new session or "
            "open /mcp to verify it."
        )

    def remove(self):
        if not self.executable:
            raise OnboardingError("Claude Code was not detected on this machine.")
        existing = self.runner.run(
            [self.executable, "mcp", "get", SERVER_NAME], timeout=15
        )
        if existing.returncode != 0:
            return "Claude Code was not configured."
        if not _looks_like_managed_launcher(
            "{}\n{}".format(existing.stdout, existing.stderr)
        ):
            raise OnboardingError(
                "The QGIS entry in Claude Code is not managed by this plugin, "
                "so it was left unchanged."
            )
        result = self.runner.run(
            [
                self.executable,
                "mcp",
                "remove",
                "--scope",
                "user",
                SERVER_NAME,
            ],
            timeout=15,
        )
        if result.returncode != 0:
            raise OnboardingError(_result_message(result))
        return "QGIS MCP was removed from Claude Code."


class JsonFileConnector:
    description = ""
    restart_hint = ""
    requires_executable = False
    executable_names = ()

    def manual_help(self):
        return (
            "User-level MCP configuration:\n{}\n\nIf automatic setup fails, "
            "make sure the parent folder is writable. Use 'Copy manual config' "
            "in this window, open this file in the client, merge the copied "
            "qgis entry under mcpServers, save, then restart the client."
        ).format(self.config_path)

    def __init__(self, config_path, home=None):
        self.home = Path(home).expanduser() if home else Path.home()
        self.config_path = Path(config_path).expanduser()

    def detected(self):
        return bool(
            any(shutil.which(name) for name in self.executable_names)
            or self.config_path.is_file()
            or self.config_path.parent.exists()
        )

    def status(self):
        document = self._read_document()
        server = document.get("mcpServers", {}).get(SERVER_NAME)
        if server is None:
            return "not_configured"
        return "configured" if _looks_like_managed_server(server) else "conflict"

    def install(self, spec):
        document = self._read_document()
        servers = document.setdefault("mcpServers", {})
        existing = servers.get(SERVER_NAME)
        if existing is not None and not _looks_like_managed_server(existing):
            raise OnboardingError(
                "{} already contains a QGIS MCP entry that was not created by "
                "this plugin. Remove or rename it before connecting.".format(self.name)
            )
        servers[SERVER_NAME] = _json_stdio_server(spec)
        _backup_and_write(
            self.config_path,
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        )
        return "{} configured for the current user.".format(self.name)

    def remove(self):
        if not self.config_path.is_file():
            return "{} was not configured.".format(self.name)
        document = self._read_document()
        servers = document.get("mcpServers", {})
        existing = servers.get(SERVER_NAME)
        if existing is None:
            return "{} was not configured.".format(self.name)
        if not _looks_like_managed_server(existing):
            raise OnboardingError(
                "The QGIS entry in {} is not managed by this plugin, so it was "
                "left unchanged.".format(self.name)
            )
        del servers[SERVER_NAME]
        _backup_and_write(
            self.config_path,
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        )
        return "QGIS MCP was removed from {}.".format(self.name)

    def _read_document(self):
        if not self.config_path.is_file():
            return {}
        text = _read_text(self.config_path, self.name)
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OnboardingError(
                "{} configuration is not valid JSON at line {}, column {}. "
                "It was left unchanged: {}".format(
                    self.name, exc.lineno, exc.colno, self.config_path
                )
            ) from exc
        if not isinstance(document, dict):
            raise OnboardingError(
                "{} configuration must contain a JSON object: {}".format(
                    self.name, self.config_path
                )
            )
        servers = document.get("mcpServers")
        if servers is not None and not isinstance(servers, dict):
            raise OnboardingError(
                "{} configuration has an invalid mcpServers value. It was left "
                "unchanged: {}".format(self.name, self.config_path)
            )
        return document


class CursorConnector(JsonFileConnector):
    name = "Cursor"
    description = "Cursor IDE and Cursor Agent"
    restart_hint = (
        "Quit every Cursor window and reopen Cursor. Then check Settings → "
        "Tools & MCP; QGIS should be listed and enabled."
    )
    executable_names = ("cursor", "cursor-agent")

    def manual_help(self):
        return (
            "Cursor user MCP configuration:\n{}\n\nManual fallback:\n"
            "1. In Cursor, open Settings -> Tools & MCP.\n"
            "2. Open the raw MCP configuration.\n"
            "3. Use 'Copy manual config' here and merge the qgis entry.\n"
            "4. Save, fully quit Cursor and reopen it."
        ).format(self.config_path)

    def __init__(self, home=None, config_path=None):
        home_path = Path(home).expanduser() if home else Path.home()
        super().__init__(
            config_path or home_path / ".cursor" / "mcp.json",
            home=home_path,
        )


class AntigravityConnector(JsonFileConnector):
    name = "Antigravity"
    description = "Google Antigravity IDE and CLI"
    restart_hint = (
        "Quit and reopen Antigravity, or open Settings → Customizations and "
        "press Refresh. QGIS should appear under Installed MCP Servers."
    )
    executable_names = ("agy", "antigravity")

    def manual_help(self):
        return (
            "Antigravity MCP configuration selected by QGIS:\n{}\n\n"
            "Manual fallback:\n"
            "1. Open Antigravity Settings -> Customizations.\n"
            "2. Open MCP Servers -> Manage MCP Servers -> View raw config.\n"
            "3. Use 'Copy manual config' here and merge the qgis entry.\n"
            "4. Save and press Refresh, or fully restart Antigravity."
        ).format(self.config_path)

    def __init__(self, home=None, config_path=None):
        home_path = Path(home).expanduser() if home else Path.home()
        if config_path is None:
            configured_home = os.environ.get("GEMINI_CLI_HOME")
            gemini_home = (
                Path(configured_home).expanduser()
                if configured_home and home is None
                else home_path / ".gemini"
            )
            current = gemini_home / "config" / "mcp_config.json"
            known_locations = (
                current,
                gemini_home / "antigravity" / "mcp_config.json",
                gemini_home / "antigravity-ide" / "mcp_config.json",
                gemini_home / "antigravity-cli" / "mcp_config.json",
            )
            config_path = next(
                (candidate for candidate in known_locations if candidate.is_file()),
                current,
            )
        super().__init__(config_path, home=home_path)


class OpenCodeConnector(JsonFileConnector):
    name = "OpenCode"
    description = "Open-source AI coding agent"
    restart_hint = (
        "Quit the current OpenCode session and start a new one. Run "
        "opencode mcp list to verify that QGIS is enabled."
    )
    executable_names = ("opencode",)

    def __init__(self, home=None, config_path=None):
        home_path = Path(home).expanduser() if home else Path.home()
        configured = os.environ.get("OPENCODE_CONFIG") if home is None else None
        target = config_path or configured or (
            home_path / ".config" / "opencode" / "opencode.json"
        )
        super().__init__(target, home=home_path)

    def manual_help(self):
        return (
            "OpenCode global configuration:\n{}\n\nManual fallback:\n"
            "1. Use 'Copy manual config' in this window.\n"
            "2. Open the file above and add the qgis entry under the mcp object.\n"
            "3. Save, restart OpenCode and run 'opencode mcp list'.\n\n"
            "If you use another file, set OPENCODE_CONFIG before starting QGIS."
        ).format(self.config_path)

    @staticmethod
    def manual_config(spec):
        return json.dumps(
            {
                "mcp": {
                    SERVER_NAME: {
                        "type": "local",
                        "command": [str(spec.command)]
                        + [str(item) for item in spec.args],
                        "enabled": True,
                        "environment": {},
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )

    def status(self):
        document = self._read_document()
        servers = document.get("mcp", {})
        if servers is not None and not isinstance(servers, dict):
            return "conflict"
        server = servers.get(SERVER_NAME) if servers else None
        if server is None:
            return "not_configured"
        return "configured" if _looks_like_opencode_server(server) else "conflict"

    def install(self, spec):
        document = self._read_document()
        servers = document.setdefault("mcp", {})
        if not isinstance(servers, dict):
            raise OnboardingError(
                "OpenCode configuration has an invalid mcp value. It was left "
                "unchanged: {}".format(self.config_path)
            )
        existing = servers.get(SERVER_NAME)
        if existing is not None and not _looks_like_opencode_server(existing):
            raise OnboardingError(
                "OpenCode already contains a QGIS MCP entry that was not created "
                "by this plugin. Remove or rename it before connecting."
            )
        document.setdefault("$schema", "https://opencode.ai/config.json")
        servers[SERVER_NAME] = {
            "type": "local",
            "command": [str(spec.command)] + [str(item) for item in spec.args],
            "enabled": True,
            "environment": {},
        }
        _backup_and_write(
            self.config_path,
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        )
        return "OpenCode configured for the current user."

    def remove(self):
        if not self.config_path.is_file():
            return "OpenCode was not configured."
        document = self._read_document()
        servers = document.get("mcp", {})
        existing = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
        if existing is None:
            return "OpenCode was not configured."
        if not _looks_like_opencode_server(existing):
            raise OnboardingError(
                "The QGIS entry in OpenCode is not managed by this plugin, so it "
                "was left unchanged."
            )
        del servers[SERVER_NAME]
        _backup_and_write(
            self.config_path,
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        )
        return "QGIS MCP was removed from OpenCode."


def universal_config(spec):
    return json.dumps(
        {"mcpServers": {SERVER_NAME: spec.mcp_server()}},
        ensure_ascii=False,
        indent=2,
    )


def health_check(spec, timeout=15, event_pump=None):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "qgis-onboarding", "version": "0.4.7"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "qgis_session_snapshot",
                "arguments": {"detail": "summary"},
            },
        },
    ]
    payload = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in messages)
    result = CommandRunner().run(
        spec.command_line(),
        input_text=payload,
        timeout=timeout,
        event_pump=event_pump,
    )
    if result.returncode != 0:
        raise OnboardingError(
            "The MCP health check failed: {}".format(_result_message(result))
        )
    responses = []
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            responses.append(message)
    response = next((item for item in responses if item.get("id") == 2), None)
    if response is None:
        raise OnboardingError("The MCP server did not return the QGIS test response.")
    if "error" in response:
        raise OnboardingError(
            "QGIS rejected the test request: {}".format(
                response["error"].get("message", response["error"])
            )
        )
    structured = response.get("result", {}).get("structuredContent", {})
    if "revision" not in structured:
        raise OnboardingError("The MCP response did not contain a QGIS session revision.")
    return {
        "revision": structured["revision"],
        "layer_count": structured.get("project", {}).get("layer_count", 0),
    }


def _update_codex_config(path, block):
    path = Path(path)
    text = _read_text(path, "Codex") if path.is_file() else ""
    without_managed = _remove_managed_block(text)
    section = re.compile(
        r"""(?mx)^\s*\[
        \s*mcp_servers\s*\.\s*
        (?:qgis|"qgis"|'qgis')
        \s*\]\s*$
        """
    )
    if section.search(without_managed):
        raise OnboardingError(
            "Codex already contains a QGIS MCP configuration that was not created "
            "by this plugin. Remove or rename that entry before connecting."
        )
    updated = without_managed.rstrip()
    if updated:
        updated += "\n\n"
    updated += block + "\n"
    if updated != text:
        _backup_and_write(path, updated)


def _remove_managed_block(text):
    pattern = re.compile(
        r"(?ms)^[ \t]*"
        + re.escape(MANAGED_BEGIN)
        + r"\s*$.*?^[ \t]*"
        + re.escape(MANAGED_END)
        + r"\s*$\n?"
    )
    return pattern.sub("", text)


def _backup_and_write(path, text):
    path = Path(path)
    try:
        if path.is_file():
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(
                "{}.qgis-mcp-{}-{:06d}.bak".format(
                    path.name, timestamp, time.time_ns() % 1000000
                )
            )
            shutil.copy2(str(path), str(backup))
        _atomic_write(path, text)
    except OnboardingError:
        raise
    except OSError as exc:
        raise OnboardingError(
            "Could not safely update {}. Check that the file and its folder are "
            "writable, then try again: {}".format(path, exc)
        ) from exc


def _atomic_write(path, text):
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=path.name + "-", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    except OSError as exc:
        raise OnboardingError(
            "Could not write {}. Check permissions, file locks and available "
            "disk space: {}".format(path, exc)
        ) from exc
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _read_text(path, client_name):
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise OnboardingError(
            "{} configuration is not valid UTF-8 and was left unchanged: {}".format(
                client_name, path
            )
        ) from exc
    except OSError as exc:
        raise OnboardingError(
            "Could not read {} configuration at {}: {}".format(client_name, path, exc)
        ) from exc


def _json_stdio_server(spec):
    return {
        "command": str(spec.command),
        "args": [str(item) for item in spec.args],
        "env": {},
    }


def _looks_like_managed_server(server):
    if not isinstance(server, dict):
        return False
    command = str(server.get("command", ""))
    args = server.get("args", [])
    if not isinstance(args, list):
        return False
    return bool(command) and any(
        Path(str(argument)).name.casefold() == "qgis_mcp_launcher.py"
        for argument in args
    )


def _looks_like_opencode_server(server):
    if not isinstance(server, dict) or server.get("type") != "local":
        return False
    command = server.get("command", [])
    if not isinstance(command, list):
        return False
    return any(
        Path(str(argument)).name.casefold() == "qgis_mcp_launcher.py"
        for argument in command
    )


def _looks_like_managed_launcher(value):
    normalized = str(value).replace("\\", "/").casefold()
    return "qgis_mcp_launcher.py" in normalized and "/.qgis-mcp/" in normalized


def _toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def _result_message(result):
    return (result.stderr or result.stdout or "unknown error").strip()


def _qprocess_text(value):
    return bytes(value).decode("utf-8", errors="replace")
