from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_QUERY_KEYS = {
    "api_key",
    "apikey",
    "key",
    "map_key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}


class PolicyError(ValueError):
    pass


class NetworkPolicy:
    def __init__(self, allowed_hosts=None, allow_private=None):
        configured = allowed_hosts
        if configured is None:
            configured = os.environ.get("QGIS_MCP_ALLOWED_HOSTS", "")
        if isinstance(configured, str):
            configured = configured.split(",")
        self.allowed_hosts = {
            value.strip().casefold().rstrip(".") for value in configured if value.strip()
        }
        self.allow_private = (
            _truthy(os.environ.get("QGIS_MCP_ALLOW_PRIVATE_NETWORK"))
            if allow_private is None
            else bool(allow_private)
        )

    def validate(self, url, resolve=True):
        if len(str(url)) > 8192:
            raise PolicyError("The data source URL is too long")
        parsed = urlsplit(str(url))
        if parsed.scheme.casefold() not in {"http", "https"}:
            raise PolicyError("Only HTTP and HTTPS data sources are allowed")
        if parsed.username or parsed.password:
            raise PolicyError("Credentials must use a QGIS authentication configuration")
        host = (parsed.hostname or "").casefold().rstrip(".")
        if not host:
            raise PolicyError("The data source URL has no host")
        if self.allowed_hosts and not any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self.allowed_hosts
        ):
            raise PolicyError("The data source host is not allow-listed")
        if not self.allow_private:
            addresses = _addresses(host, resolve)
            if any(not address.is_global for address in addresses):
                raise PolicyError("Private, loopback, link-local, and reserved hosts are blocked")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


class OutputPathPolicy:
    def __init__(self, roots=None):
        if roots is None:
            configured = os.environ.get("QGIS_MCP_ALLOWED_OUTPUT_PATHS", "")
            roots = [item for item in configured.split(os.pathsep) if item]
        self.roots = [Path(root).expanduser().resolve() for root in roots]

    def validate(self, path, project_path=None, create_parent=True):
        target = Path(path).expanduser().resolve()
        allowed = list(self.roots)
        allowed.append((Path.home() / ".qgis-mcp" / "outputs").resolve())
        if project_path:
            project = Path(project_path).expanduser().resolve()
            allowed.append(project if project.is_dir() else project.parent)
        if not any(_inside(target, root) for root in allowed):
            raise PolicyError(
                "Output path is outside the project, QGIS MCP output folder, and allow-listed roots"
            )
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target


class DataCache:
    def __init__(self, root=None, max_bytes=512 * 1024 * 1024):
        self.root = Path(root or Path.home() / ".qgis-mcp" / "cache" / "data")
        self.max_bytes = int(max_bytes)

    def lookup(self, url, max_age_seconds):
        directory = self.root / self.key(url)
        metadata_path = directory / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            path = directory / metadata["filename"]
            if not path.is_file():
                return None
            if time.time() - float(metadata["fetched_at"]) > float(max_age_seconds):
                return None
            metadata["path"] = str(path)
            metadata["cache_hit"] = True
            return metadata
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def put(self, url, payload, filename, metadata=None):
        payload = bytes(payload)
        directory = self.root / self.key(url)
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(filename)
        destination = directory / safe_name
        _atomic_bytes(destination, payload)
        value = {
            "filename": safe_name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "fetched_at": time.time(),
            "cache_hit": False,
            **(metadata or {}),
        }
        _atomic_text(directory / "metadata.json", json.dumps(value, indent=2))
        self.prune()
        return {**value, "path": str(destination)}

    def prune(self):
        if not self.root.exists():
            return
        entries = []
        total = 0
        for metadata_path in self.root.glob("*/metadata.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                path = metadata_path.parent / metadata["filename"]
                size = path.stat().st_size
                total += size
                entries.append((float(metadata.get("fetched_at", 0)), size, path, metadata_path))
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        for _, size, path, metadata_path in sorted(entries):
            if total <= self.max_bytes:
                break
            try:
                path.unlink()
                metadata_path.unlink()
                metadata_path.parent.rmdir()
                total -= size
            except OSError:
                pass

    @staticmethod
    def key(url):
        return hashlib.sha256(str(url).encode("utf-8")).hexdigest()


def redact_url(url, secret_values=None):
    value = str(url)
    for secret in secret_values or ():
        if secret:
            value = value.replace(str(secret), "***")
    parsed = urlsplit(value)
    query = urlencode(
        [
            (key, "***" if key.casefold() in SENSITIVE_QUERY_KEYS else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def safe_extract_zip(path, destination, max_total_bytes=512 * 1024 * 1024):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    extracted = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            total += member.file_size
            if total > max_total_bytes:
                raise PolicyError("Archive exceeds the extracted-size limit")
            target = (destination / member.filename).resolve()
            if not _inside(target, destination):
                raise PolicyError("Archive contains an unsafe path")
            if target.is_symlink() or any(
                parent.is_symlink()
                for parent in target.parents
                if parent != destination and _inside(parent, destination)
            ):
                raise PolicyError("Archive extraction cannot traverse symbolic links")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            extracted.append(target)
    return extracted


def _addresses(host, resolve):
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return []
    try:
        values = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PolicyError("The data source host cannot be resolved") from exc
    return list({ipaddress.ip_address(item[4][0]) for item in values})


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_filename(value):
    name = Path(str(value or "download.bin")).name
    cleaned = "".join(character for character in name if character.isalnum() or character in ".-_")
    return cleaned[:200] or "download.bin"


def _atomic_bytes(path, payload):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(path, text):
    _atomic_bytes(path, text.encode("utf-8"))


def _truthy(value):
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}
