from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_PACKAGE_BYTES = 25 * 1024 * 1024
REQUIRED_FILES = {"metadata.txt", "__init__.py", "LICENSE"}
ALLOWED_HIDDEN_FILES: set[str] = set()
BINARY_SUFFIXES = {".bin", ".class", ".dll", ".dylib", ".exe", ".jar", ".pyd", ".so"}

# Active CRITICAL Bandit rules from the QGIS Plugins security rules reference.
QGIS_BLOCKING_BANDIT_RULES = {
    "B101",
    "B102",
    "B103",
    "B105",
    "B106",
    "B107",
    "B111",
    "B201",
    "B202",
    "B301",
    "B302",
    "B304",
    "B305",
    "B306",
    "B307",
    "B312",
    "B321",
    "B323",
    "B401",
    "B402",
    "B412",
    "B413",
    "B501",
    "B502",
    "B503",
    "B505",
    "B506",
    "B507",
    "B601",
    "B602",
    "B604",
    "B605",
    "B609",
    "B610",
    "B611",
    "B612",
    "B613",
    "B615",
    "B701",
}


def validate_package_structure(package: Path) -> tuple[str, list[str]]:
    issues: list[str] = []
    if package.stat().st_size > MAX_PACKAGE_BYTES:
        issues.append("package exceeds the QGIS 25 MB limit")
    with zipfile.ZipFile(package) as archive:
        names = [name for name in archive.namelist() if name and not name.endswith("/")]
    roots = {PurePosixPath(name).parts[0] for name in names}
    if len(roots) != 1:
        return "", ["package must contain exactly one root directory"]
    root = roots.pop()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", root):
        issues.append("plugin root name contains unsupported characters")
    relative_names = {
        str(PurePosixPath(*PurePosixPath(name).parts[1:])) for name in names
    }
    missing = sorted(REQUIRED_FILES - relative_names)
    if missing:
        issues.append("missing required files: {}".format(", ".join(missing)))
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            issues.append("unsafe archive path: {}".format(name))
        if Path(path.name).suffix.casefold() in BINARY_SUFFIXES:
            issues.append("binary file is not allowed: {}".format(name))
        for part in path.parts[1:]:
            if part.startswith(".") and part not in ALLOWED_HIDDEN_FILES:
                issues.append("unexpected hidden path: {}".format(name))
                break
        if "__pycache__" in path.parts or path.suffix.casefold() in {".pyc", ".pyo"}:
            issues.append("generated Python cache is not allowed: {}".format(name))
    return root, sorted(set(issues))


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def scan_package(package: Path) -> list[str]:
    root_name, issues = validate_package_structure(package)
    if issues:
        return issues
    with tempfile.TemporaryDirectory(prefix="qgis-plugin-security-") as temporary:
        temporary_path = Path(temporary).resolve()
        with zipfile.ZipFile(package) as archive:
            for member in archive.infolist():
                destination = (temporary_path / member.filename).resolve()
                if temporary_path not in destination.parents and destination != temporary_path:
                    return ["unsafe archive path: {}".format(member.filename)]
                archive.extract(member, temporary_path)
        plugin_root = temporary_path / root_name

        bandit = _run(
            [sys.executable, "-m", "bandit", "-r", str(plugin_root), "-f", "json"]
        )
        try:
            bandit_data = json.loads(bandit.stdout)
        except json.JSONDecodeError:
            issues.append("Bandit did not return valid JSON: {}".format(bandit.stderr.strip()))
        else:
            for finding in bandit_data.get("results", []):
                if finding.get("test_id") in QGIS_BLOCKING_BANDIT_RULES:
                    issues.append(
                        "{} {}:{} {}".format(
                            finding["test_id"],
                            Path(finding["filename"]).name,
                            finding["line_number"],
                            finding["issue_text"],
                        )
                    )

        secrets = _run(
            [sys.executable, "-m", "detect_secrets", "scan", str(plugin_root)]
        )
        try:
            secrets_data = json.loads(secrets.stdout)
        except json.JSONDecodeError:
            issues.append(
                "detect-secrets did not return valid JSON: {}".format(secrets.stderr.strip())
            )
        else:
            for filename, findings in secrets_data.get("results", {}).items():
                for finding in findings:
                    issues.append(
                        "secret {}:{} {}".format(
                            filename,
                            finding.get("line_number"),
                            finding.get("type"),
                        )
                    )

        flake8_command = [
            sys.executable,
            "-m",
            "flake8",
            "--select=E9,F63,F7,F82",
            str(plugin_root),
        ]
        flake8 = _run(flake8_command)
        if flake8.returncode:
            issues.extend(line for line in flake8.stdout.splitlines() if line.strip())
    return issues


def latest_package(root: Path) -> Path:
    packages = sorted(
        (root / "dist").glob("qgis_agent_mcp-*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not packages:
        raise FileNotFoundError("No plugin ZIP found in dist; run scripts/build_plugin.py first")
    return packages[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run QGIS publication security checks")
    parser.add_argument("package", nargs="?", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    package = (args.package or latest_package(root)).resolve()
    issues = scan_package(package)
    if issues:
        print("QGIS publication security gate failed:")
        for issue in issues:
            print("- {}".format(issue))
        return 1
    print("QGIS publication security gate passed for {}".format(package))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
