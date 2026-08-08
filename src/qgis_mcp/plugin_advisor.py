from __future__ import annotations

import gzip
import json
import math
import os
import re
import secrets
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

OFFICIAL_REPOSITORY = "https://plugins.qgis.org/plugins/plugins.xml"
MAX_CATALOG_BYTES = 16 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 12 * 60 * 60
PROPOSAL_TTL_SECONDS = 15 * 60

_STOPWORDS = {
    "add",
    "avec",
    "besoin",
    "can",
    "dans",
    "des",
    "faire",
    "for",
    "have",
    "les",
    "need",
    "plugin",
    "pour",
    "qgis",
    "that",
    "the",
    "this",
    "tool",
    "une",
    "want",
    "with",
}

_CONCEPT_GROUPS = (
    "osm openstreetmap quickosm overpass",
    "basemap fond carte tiles xyz satellite imagery imagerie",
    "geocode geocoder geocoding address adresse reverse",
    "routing route network itineraire isochrone shortest",
    "cad dxf dwg autocad",
    "lidar point cloud nuage points las laz pdal",
    "topology topologie geometry geometrie validate repair quality qualite",
    "field mobile survey terrain synchronisation offline",
    "database base donnees postgis spatialite geopackage sql",
    "chart plot graph diagram graphique dataviz",
    "hydrology hydrologie watershed bassin catchment drainage",
    "terrain elevation dem mnt slope pente contour hillshade",
    "time temporal animation temps chronologie",
    "3d three dimensional scene mesh",
    "web publish server webmap webmapping qgiscloud",
)


def _normalize(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(character)
    )


def _tokens(value: Any) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", _normalize(value))
        if len(token) > 2 and token not in _STOPWORDS
    }
    return {
        token[:-1] if len(token) > 4 and token.endswith("s") else token
        for token in tokens
    }


_CONCEPT_INDEX: dict[str, set[str]] = {}
for _concept in _CONCEPT_GROUPS:
    _members = _tokens(_concept)
    for _member in _members:
        _CONCEPT_INDEX[_member] = _members


def _expanded_tokens(value: Any) -> tuple[set[str], set[str]]:
    original = _tokens(value)
    expanded = set(original)
    for token in original:
        expanded.update(_CONCEPT_INDEX.get(token, ()))
    return original, expanded


def _version_tuple(value: Any) -> tuple[int, ...]:
    numbers = [int(item) for item in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple(numbers + [0] * (4 - len(numbers)))


def _qgis_line(value: Any) -> str:
    version = _version_tuple(value)
    return "{}.{}".format(version[0], version[1])


def _compatible(plugin: dict[str, Any], qgis_version: str) -> bool:
    current = _version_tuple(qgis_version)
    minimum = _version_tuple(plugin.get("qgis_minimum_version"))
    maximum_text = str(plugin.get("qgis_maximum_version") or "").strip()
    maximum = _version_tuple(maximum_text) if maximum_text else (current[0], 99, 99, 99)
    return minimum <= current <= maximum


def _bool(value: Any) -> bool:
    return _normalize(value).strip() in {"1", "true", "yes"}


def _integer(value: Any) -> int:
    try:
        return max(0, int(float(str(value or "0"))))
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(str(value or "0"))
    except (TypeError, ValueError):
        return 0.0


def _child_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


def _summary(value: Any, limit: int = 280) -> str:
    compact = " ".join(str(value or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _maintenance(value: Any) -> tuple[float, str]:
    text = str(value or "").strip()
    if not text:
        return 0.0, "unknown"
    try:
        updated = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - updated).days)
    except ValueError:
        return 0.0, "unknown"
    if age_days <= 180:
        return 8.0, "recent"
    if age_days <= 365:
        return 5.0, "maintained"
    if age_days <= 730:
        return 0.0, "aging"
    return -10.0, "stale"


class PluginCatalog:
    """Cached reader for the official QGIS plugin repository."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        fetcher: Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]
        | None = None,
    ) -> None:
        configured = os.environ.get("QGIS_MCP_PLUGIN_CATALOG_CACHE")
        self.cache_dir = Path(
            cache_dir or configured or Path.home() / ".qgis-mcp" / "cache"
        ).expanduser()
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._fetcher = fetcher or self._download
        self._memory: dict[str, dict[str, Any]] = {}

    def load(self, qgis_version: str, *, refresh: bool = False) -> dict[str, Any]:
        line = _qgis_line(qgis_version)
        now = time.time()
        cached = self._memory.get(line) or self._read_cache(line)
        if cached and not refresh and now - float(cached.get("fetched_at", 0)) < self.ttl_seconds:
            self._memory[line] = cached
            return self._snapshot(cached, stale=False, cache_hit=True)

        headers = {"Accept": "application/xml"}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = str(cached["etag"])
        if cached and cached.get("last_modified"):
            headers["If-Modified-Since"] = str(cached["last_modified"])
        url = OFFICIAL_REPOSITORY + "?" + urllib.parse.urlencode({"qgis": line})
        try:
            status, response_headers, payload = self._fetcher(url, headers)
            if status == 304 and cached:
                cached["fetched_at"] = now
                self._write_cache(line, cached)
                self._memory[line] = cached
                return self._snapshot(cached, stale=False, cache_hit=True)
            if status != 200:
                raise OSError("Official QGIS repository returned HTTP {}".format(status))
            records = self._parse(payload, qgis_version)
            cached = {
                "version": 1,
                "qgis_line": line,
                "fetched_at": now,
                "etag": response_headers.get("etag"),
                "last_modified": response_headers.get("last-modified"),
                "plugins": records,
            }
            self._write_cache(line, cached)
            self._memory[line] = cached
            return self._snapshot(cached, stale=False, cache_hit=False)
        except (ET.ParseError, OSError, TimeoutError, urllib.error.URLError) as exc:
            if not cached:
                raise OSError("Cannot read the official QGIS plugin repository: {}".format(exc)) from exc
            self._memory[line] = cached
            snapshot = self._snapshot(cached, stale=True, cache_hit=True)
            snapshot["warning"] = _summary(exc, 300)
            return snapshot

    def status(self) -> dict[str, Any]:
        return {
            "source": OFFICIAL_REPOSITORY,
            "cached_qgis_lines": sorted(self._memory),
            "cache_directory": str(self.cache_dir),
            "cache_ttl_seconds": self.ttl_seconds,
        }

    @staticmethod
    def _download(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(
            url,
            headers={**headers, "User-Agent": "qgis-agent-mcp/0.4.9"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                length = _integer(response.headers.get("Content-Length"))
                if length > MAX_CATALOG_BYTES:
                    raise OSError("Official plugin catalog exceeds the size limit")
                payload = response.read(MAX_CATALOG_BYTES + 1)
                if len(payload) > MAX_CATALOG_BYTES:
                    raise OSError("Official plugin catalog exceeds the size limit")
                return (
                    int(response.status),
                    {key.casefold(): value for key, value in response.headers.items()},
                    payload,
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, {}, b""
            raise

    @staticmethod
    def _parse(payload: bytes, qgis_version: str) -> list[dict[str, Any]]:
        root = ET.fromstring(payload)
        if root.tag != "plugins":
            raise ET.ParseError("Unexpected official plugin catalog root")
        best: dict[tuple[str, bool], dict[str, Any]] = {}
        for node in root.findall("pyqgis_plugin"):
            file_name = _child_text(node, "file_name")
            package = file_name.partition(".")[0].strip()
            if not package or re.fullmatch(r"[A-Za-z0-9_-]+", package) is None:
                continue
            record = {
                "package": package,
                "plugin_id": node.attrib.get("plugin_id") or None,
                "name": node.attrib.get("name", package).strip(),
                "version": node.attrib.get("version", "").strip(),
                "description": _child_text(node, "description"),
                "about": _child_text(node, "about"),
                "category": _child_text(node, "category"),
                "tags": _child_text(node, "tags"),
                "author": _child_text(node, "author_name"),
                "trusted": _bool(_child_text(node, "trusted")),
                "experimental": _bool(_child_text(node, "experimental")),
                "deprecated": _bool(_child_text(node, "deprecated")),
                "qgis_minimum_version": _child_text(node, "qgis_minimum_version"),
                "qgis_maximum_version": _child_text(node, "qgis_maximum_version"),
                "homepage": _child_text(node, "homepage"),
                "repository": _child_text(node, "repository"),
                "tracker": _child_text(node, "tracker"),
                "downloads": _integer(_child_text(node, "downloads")),
                "average_vote": _number(_child_text(node, "average_vote")),
                "rating_votes": _integer(_child_text(node, "rating_votes")),
                "update_date": _child_text(node, "update_date"),
                "external_dependencies": _child_text(node, "external_dependencies"),
                "plugin_dependencies": _child_text(node, "plugin_dependencies"),
                "server": _bool(_child_text(node, "server")),
            }
            if not _compatible(record, qgis_version):
                continue
            key = (package.casefold(), bool(record["experimental"]))
            previous = best.get(key)
            if previous is None or _version_tuple(record["version"]) > _version_tuple(
                previous["version"]
            ):
                best[key] = record
        return sorted(best.values(), key=lambda item: (item["package"].casefold(), item["experimental"]))

    def _cache_path(self, line: str) -> Path:
        return self.cache_dir / "official-qgis-plugins-{}.json.gz".format(line)

    def _read_cache(self, line: str) -> dict[str, Any] | None:
        try:
            value = json.loads(gzip.decompress(self._cache_path(line).read_bytes()))
            if isinstance(value, dict) and isinstance(value.get("plugins"), list):
                return value
        except (OSError, ValueError, json.JSONDecodeError, gzip.BadGzipFile):
            pass
        return None

    def _write_cache(self, line: str, value: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix="qgis-plugins-", suffix=".tmp", dir=str(self.cache_dir)
            )
            try:
                payload = gzip.compress(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                os.replace(temporary, self._cache_path(line))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            return

    @staticmethod
    def _snapshot(value: dict[str, Any], *, stale: bool, cache_hit: bool) -> dict[str, Any]:
        return {
            "plugins": value["plugins"],
            "catalog": {
                "source": OFFICIAL_REPOSITORY,
                "qgis_line": value.get("qgis_line"),
                "plugin_count": len(value["plugins"]),
                "fetched_at": value.get("fetched_at"),
                "stale": stale,
                "cache_hit": cache_hit,
            },
        }


class PluginAdvisor:
    """Ranks native, installed and official plugin capabilities for one task."""

    def __init__(self, catalog: PluginCatalog | None = None) -> None:
        self.catalog = catalog or PluginCatalog()
        self._proposals: dict[str, dict[str, Any]] = {}
        self._token_cache: dict[tuple[Any, ...], dict[str, set[str]]] = {}

    def recommend(
        self,
        task: str,
        qgis_version: str,
        installed: list[dict[str, Any]],
        native_matches: list[dict[str, Any]],
        *,
        limit: int = 3,
        include_experimental: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        task = str(task or "").strip()
        if not task:
            raise ValueError("task is required")
        snapshot = self.catalog.load(qgis_version, refresh=refresh)
        installed_ranked = self._rank_installed(task, installed)[:3]
        installed_packages = {
            str(item.get("package", "")).casefold() for item in installed
        }
        ranked = self._rank_catalog(
            task,
            snapshot["plugins"],
            include_experimental=include_experimental,
        )
        recommendations = []
        recommended_packages: set[str] = set()
        for candidate in ranked:
            package_key = candidate["package"].casefold()
            if package_key in installed_packages or package_key in recommended_packages:
                continue
            recommended_packages.add(package_key)
            proposal_id = self._create_proposal(candidate)
            candidate["installation"] = {
                "proposal_id": proposal_id,
                "confirmation_required": True,
                "expires_in_seconds": PROPOSAL_TTL_SECONDS,
                "instruction": (
                    "Ask the user before calling qgis_plugins action=install with this proposal_id."
                ),
            }
            recommendations.append(candidate)
            if len(recommendations) >= max(1, min(int(limit), 3)):
                break
        if native_matches and float(native_matches[0].get("relevance", 0)) >= 60:
            preferred_path = {
                "kind": "native_qgis",
                "name": native_matches[0].get("name"),
                "reason": "A relevant native QGIS capability is already available.",
            }
        elif installed_ranked and float(installed_ranked[0].get("score", 0)) >= 60:
            preferred_path = {
                "kind": "installed_plugin",
                "name": installed_ranked[0].get("package"),
                "reason": "A relevant plugin is already installed.",
            }
        elif recommendations:
            preferred_path = {
                "kind": "new_plugin",
                "name": recommendations[0].get("package"),
                "reason": "No strong native or installed match was found.",
            }
        else:
            preferred_path = {
                "kind": "none",
                "name": None,
                "reason": "No sufficiently relevant capability was found.",
            }
        result = {
            "task": task,
            "priority": ["native_qgis", "installed_plugins", "new_plugins"],
            "preferred_path": preferred_path,
            "native_matches": native_matches[:3],
            "installed_matches": installed_ranked,
            "recommendations": recommendations,
            "catalog": snapshot["catalog"],
            "installation_policy": {
                "automatic_installation": False,
                "explicit_user_confirmation_required": True,
                "official_qgis_repository_only": True,
                "untrusted_plugins_require_extra_confirmation": True,
            },
        }
        if snapshot.get("warning"):
            result["catalog_warning"] = snapshot["warning"]
        if not recommendations:
            result["message"] = (
                "No suitable new stable plugin was found; prefer the native or installed matches."
            )
        return result

    def search(
        self,
        query: str,
        qgis_version: str,
        installed: list[dict[str, Any]],
        *,
        limit: int = 5,
        include_experimental: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        snapshot = self.catalog.load(qgis_version, refresh=refresh)
        installed_packages = {
            str(item.get("package", "")).casefold(): item for item in installed
        }
        matches = self._rank_catalog(
            query,
            snapshot["plugins"],
            include_experimental=include_experimental,
        )[: max(1, min(int(limit), 20))]
        for match in matches:
            local = installed_packages.get(match["package"].casefold())
            match["installed"] = bool(local)
            match["active"] = bool((local or {}).get("active"))
        result = {"query": query, "matches": matches, "catalog": snapshot["catalog"]}
        if snapshot.get("warning"):
            result["catalog_warning"] = snapshot["warning"]
        return result

    def describe(
        self,
        package: str,
        qgis_version: str,
        installed: list[dict[str, Any]],
        *,
        include_experimental: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        package = str(package or "").strip()
        if not package:
            raise ValueError("plugin is required")
        snapshot = self.catalog.load(qgis_version, refresh=refresh)
        candidates = [
            item
            for item in snapshot["plugins"]
            if item["package"].casefold() == package.casefold()
            and (include_experimental or not item["experimental"])
        ]
        if not candidates:
            raise KeyError("Compatible plugin not found in the official QGIS repository")
        candidate = max(candidates, key=lambda item: _version_tuple(item["version"]))
        local = next(
            (
                item
                for item in installed
                if str(item.get("package", "")).casefold() == package.casefold()
            ),
            None,
        )
        return {
            "plugin": self._public_plugin(candidate, score=None, matched_terms=[]),
            "installed": bool(local),
            "active": bool((local or {}).get("active")),
            "catalog": snapshot["catalog"],
        }

    def validate_proposal(
        self,
        proposal_id: str,
        package: str,
        *,
        confirm_installation: bool,
        confirm_untrusted: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._prune_proposals()
        proposal = self._proposals.get(str(proposal_id or ""))
        if proposal is None:
            raise ValueError("A current proposal_id from qgis_plugin_advisor is required")
        if proposal["package"].casefold() != str(package or "").casefold():
            raise ValueError("proposal_id does not match the requested plugin")
        replay_key = str(idempotency_key or "").strip() or None
        consumed_by = proposal.get("consumed_by")
        if consumed_by is not None and consumed_by != replay_key:
            raise ValueError("This installation proposal has already been used")
        if not confirm_installation:
            raise ValueError("Explicit user confirmation is required before plugin installation")
        if not proposal["trusted"] and not confirm_untrusted:
            raise ValueError("This plugin is not marked trusted; extra user confirmation is required")
        return dict(proposal)

    def complete_proposal(
        self, proposal_id: str, idempotency_key: str | None = None
    ) -> None:
        proposal_id = str(proposal_id or "")
        replay_key = str(idempotency_key or "").strip() or None
        if replay_key and proposal_id in self._proposals:
            self._proposals[proposal_id]["consumed_by"] = replay_key
        else:
            self._proposals.pop(proposal_id, None)

    def status(self) -> dict[str, Any]:
        self._prune_proposals()
        return {
            **self.catalog.status(),
            "active_installation_proposals": sum(
                proposal.get("consumed_by") is None
                for proposal in self._proposals.values()
            ),
            "idempotent_replays": sum(
                proposal.get("consumed_by") is not None
                for proposal in self._proposals.values()
            ),
            "proposal_ttl_seconds": PROPOSAL_TTL_SECONDS,
        }

    def _rank_catalog(
        self,
        query: str,
        plugins: list[dict[str, Any]],
        *,
        include_experimental: bool,
    ) -> list[dict[str, Any]]:
        ranked = []
        for plugin in plugins:
            if plugin["deprecated"] or (plugin["experimental"] and not include_experimental):
                continue
            score, matched, reasons = self._score(query, plugin)
            if score <= 0:
                continue
            ranked.append(self._public_plugin(plugin, score=score, matched_terms=matched, reasons=reasons))
        ranked.sort(
            key=lambda item: (
                -float(item["score"]),
                not bool(item["quality"]["trusted"]),
                -int(item["quality"]["downloads"]),
                item["name"].casefold(),
            )
        )
        return ranked

    def _rank_installed(
        self, query: str, installed: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        ranked = []
        for item in installed:
            metadata = item.get("metadata") or {}
            plugin = {
                "package": item.get("package", ""),
                "name": metadata.get("name") or item.get("package", ""),
                "description": metadata.get("description", ""),
                "about": metadata.get("about", ""),
                "tags": metadata.get("tags", ""),
                "category": metadata.get("category", ""),
                "downloads": 0,
                "average_vote": 0,
                "rating_votes": 0,
                "trusted": True,
                "experimental": _bool(metadata.get("experimental")),
                "deprecated": _bool(metadata.get("deprecated")),
                "external_dependencies": metadata.get("external_dependencies", ""),
                "plugin_dependencies": metadata.get("plugin_dependencies", ""),
                "update_date": metadata.get("update_date", ""),
            }
            score, matched, reasons = self._score(query, plugin)
            if score <= 0:
                continue
            ranked.append(
                {
                    "package": plugin["package"],
                    "name": plugin["name"],
                    "summary": _summary(plugin["description"] or plugin["about"]),
                    "active": bool(item.get("active")),
                    "loaded": bool(item.get("loaded")),
                    "score": round(score, 2),
                    "matched_terms": matched,
                    "reasons": reasons + ["Already installed; prefer this before adding a plugin."],
                }
            )
        ranked.sort(key=lambda item: (-item["score"], not item["active"], item["name"].casefold()))
        return ranked

    def _score(
        self, query: str, plugin: dict[str, Any]
    ) -> tuple[float, list[str], list[str]]:
        original, expanded = _expanded_tokens(query)
        if not original:
            return 0.0, [], []
        fields = self._plugin_tokens(plugin)
        matched_original = original & set().union(*fields.values())
        matched_expanded = expanded & set().union(*fields.values())
        if not matched_expanded:
            return 0.0, [], []
        score = 0.0
        for field, weight in {
            "name": 34,
            "tags": 26,
            "category": 18,
            "description": 14,
            "about": 8,
        }.items():
            score += len(expanded & fields[field]) * weight
            score += len(original & fields[field]) * weight
        normalized_query = _normalize(query).strip()
        normalized_name = _normalize(plugin.get("name", ""))
        if normalized_query and normalized_query in normalized_name:
            score += 80
        coverage = len(matched_original) / max(1, len(original))
        score += coverage * 70
        if plugin.get("trusted"):
            score += 7
        downloads = _integer(plugin.get("downloads"))
        if downloads:
            score += min(9.0, math.log10(downloads + 1) * 1.8)
        votes = _integer(plugin.get("rating_votes"))
        rating = _number(plugin.get("average_vote"))
        if votes and rating:
            score += min(6.0, max(0.0, rating) * min(1.0, votes / 30.0))
        if plugin.get("external_dependencies"):
            score -= 6
        if plugin.get("plugin_dependencies"):
            score -= 3
        if plugin.get("experimental"):
            score -= 20
        maintenance_score, maintenance = _maintenance(plugin.get("update_date"))
        score += maintenance_score
        reasons = ["Matched: {}.".format(", ".join(sorted(matched_expanded)[:8]))]
        if coverage >= 0.75:
            reasons.append("Strong task-term coverage.")
        if plugin.get("trusted"):
            reasons.append("Marked trusted by the QGIS repository.")
        if downloads >= 10000:
            reasons.append("Established usage in the official repository.")
        if plugin.get("external_dependencies"):
            reasons.append("Requires external dependencies; review them before installation.")
        if plugin.get("plugin_dependencies"):
            reasons.append("Requires other QGIS plugins; QGIS will request separate approval.")
        if maintenance == "recent":
            reasons.append("Updated recently.")
        elif maintenance == "stale":
            reasons.append("Maintenance appears stale; review compatibility carefully.")
        return score, sorted(matched_expanded), reasons

    def _plugin_tokens(self, plugin: dict[str, Any]) -> dict[str, set[str]]:
        cache_key = (
            plugin.get("package", ""),
            plugin.get("version", ""),
            bool(plugin.get("experimental")),
            plugin.get("update_date", ""),
        )
        cached = self._token_cache.get(cache_key)
        if cached is not None:
            return cached
        fields = {
            "name": _tokens(
                "{} {}".format(plugin.get("package", ""), plugin.get("name", ""))
            ),
            "tags": _tokens(plugin.get("tags", "")),
            "category": _tokens(plugin.get("category", "")),
            "description": _tokens(plugin.get("description", "")),
            "about": _tokens(plugin.get("about", "")),
        }
        if len(self._token_cache) >= 10_000:
            self._token_cache.clear()
        self._token_cache[cache_key] = fields
        return fields

    @staticmethod
    def _public_plugin(
        plugin: dict[str, Any],
        *,
        score: float | None,
        matched_terms: list[str],
        reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        _, maintenance = _maintenance(plugin.get("update_date"))
        risks = []
        if not plugin.get("trusted"):
            risks.append("Not marked trusted by the official QGIS repository.")
        if plugin.get("experimental"):
            risks.append("Experimental release.")
        if plugin.get("external_dependencies"):
            risks.append("Requires external dependencies.")
        if plugin.get("plugin_dependencies"):
            risks.append("Requires other QGIS plugins.")
        if maintenance == "stale":
            risks.append("No recent repository update.")
        value = {
            "package": plugin["package"],
            "name": plugin["name"],
            "version": plugin["version"],
            "summary": _summary(plugin.get("description") or plugin.get("about")),
            "matched_terms": matched_terms,
            "reasons": reasons or [],
            "risks": risks,
            "compatibility": {
                "minimum_qgis": plugin.get("qgis_minimum_version") or None,
                "maximum_qgis": plugin.get("qgis_maximum_version") or None,
            },
            "quality": {
                "trusted": bool(plugin.get("trusted")),
                "experimental": bool(plugin.get("experimental")),
                "deprecated": bool(plugin.get("deprecated")),
                "downloads": _integer(plugin.get("downloads")),
                "average_vote": round(_number(plugin.get("average_vote")), 2),
                "rating_votes": _integer(plugin.get("rating_votes")),
                "updated_at": plugin.get("update_date") or None,
                "maintenance": maintenance,
                "external_dependencies": plugin.get("external_dependencies") or None,
                "plugin_dependencies": plugin.get("plugin_dependencies") or None,
            },
            "links": {
                key: plugin.get(key)
                for key in ("homepage", "repository", "tracker")
                if plugin.get(key)
            },
        }
        if score is not None:
            value["score"] = round(score, 2)
        return value

    def _create_proposal(self, plugin: dict[str, Any]) -> str:
        proposal_id = "qp_" + secrets.token_urlsafe(12)
        self._proposals[proposal_id] = {
            "proposal_id": proposal_id,
            "package": plugin["package"],
            "version": plugin["version"],
            "trusted": bool(plugin["quality"]["trusted"]),
            "experimental": bool(plugin["quality"]["experimental"]),
            "expires_at": time.monotonic() + PROPOSAL_TTL_SECONDS,
        }
        return proposal_id

    def _prune_proposals(self) -> None:
        now = time.monotonic()
        for proposal_id, proposal in list(self._proposals.items()):
            if float(proposal["expires_at"]) <= now:
                self._proposals.pop(proposal_id, None)
