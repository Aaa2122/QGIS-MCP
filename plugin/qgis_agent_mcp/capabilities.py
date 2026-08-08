from __future__ import annotations

import inspect
import re
import time
import unicodedata

import qgis.utils
from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QAction, QApplication, QWidget

from .processing_schema import algorithm_schemas


class ObjectRegistry:
    def __init__(self, iface):
        self.iface = iface
        self._objects = {}
        self._refreshed_at = 0.0

    def refresh(self, force=False):
        if (
            not force
            and self._objects
            and time.monotonic() - self._refreshed_at < 1.0
        ):
            return self._objects
        self._objects = {}
        roots = [self.iface.mainWindow()]
        roots.extend(QApplication.topLevelWidgets())
        seen = set()
        for root in roots:
            for obj in [root] + root.findChildren(QObject):
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                runtime_id = self._runtime_id(obj)
                self._objects[runtime_id] = obj
        self._refreshed_at = time.monotonic()
        return self._objects

    def get(self, runtime_id):
        obj = self._objects.get(runtime_id)
        if obj is None:
            self.refresh(force=True)
            obj = self._objects.get(runtime_id)
        if obj is None:
            raise KeyError("Qt target is stale or unknown; search the UI again")
        try:
            obj.objectName()
        except RuntimeError as exc:
            self._objects.pop(runtime_id, None)
            raise KeyError("Qt target was deleted; search the UI again") from exc
        return obj

    @staticmethod
    def _runtime_id(obj):
        name = obj.objectName() or ""
        return "qt:{}:{}:{:x}".format(type(obj).__name__, name, id(obj))

    @staticmethod
    def summarize(runtime_id, obj):
        value = {
            "id": runtime_id,
            "class": type(obj).__name__,
            "object_name": obj.objectName(),
            "enabled": obj.isEnabled() if hasattr(obj, "isEnabled") else None,
            "visible": obj.isVisible() if hasattr(obj, "isVisible") else None,
        }
        if isinstance(obj, QAction):
            value.update(
                {
                    "kind": "action",
                    "text": _clean(obj.text()),
                    "tool_tip": _clean(obj.toolTip()),
                    "checkable": obj.isCheckable(),
                    "checked": obj.isChecked(),
                    "shortcut": obj.shortcut().toString(),
                }
            )
        elif isinstance(obj, QWidget):
            value.update(
                {
                    "kind": "widget",
                    "title": _clean(obj.windowTitle()),
                    "text": _widget_text(obj),
                    "geometry": {
                        "x": obj.geometry().x(),
                        "y": obj.geometry().y(),
                        "width": obj.geometry().width(),
                        "height": obj.geometry().height(),
                    },
                }
            )
        else:
            value["kind"] = "object"
        return value


class CapabilityIndex:
    API_ROOTS = {
        "project": lambda iface: QgsProject.instance(),
        "iface": lambda iface: iface,
        "canvas": lambda iface: iface.mapCanvas(),
        "active_layer": lambda iface: iface.activeLayer(),
    }

    def __init__(self, iface, objects):
        self.iface = iface
        self.objects = objects
        self._processing_cache = []
        self._processing_cache_until = 0.0
        self._plugin_cache = []
        self._plugin_cache_until = 0.0
        self._api_cache = {}

    def invalidate(self):
        self._processing_cache = []
        self._processing_cache_until = 0.0
        self._plugin_cache = []
        self._plugin_cache_until = 0.0
        self._api_cache = {}
        return self.objects.refresh(force=True)

    def reindex(self):
        objects = self.invalidate()
        processing = self._processing_items()
        plugins = self._plugin_items()
        return {
            "processing_algorithms": len(processing),
            "enabled_plugins": len(plugins),
            "qgis_actions": sum(isinstance(obj, QAction) for obj in objects.values()),
        }

    def _processing_items(self):
        now = time.monotonic()
        if now < self._processing_cache_until:
            return self._processing_cache
        results = []
        registry = QgsApplication.processingRegistry()
        for algorithm in registry.algorithms():
            results.append(
                {
                    "kind": "processing",
                    "id": algorithm.id(),
                    "name": algorithm.displayName(),
                    "group": algorithm.group(),
                    "provider": algorithm.provider().id() if algorithm.provider() else None,
                }
            )
        self._processing_cache = results
        self._processing_cache_until = now + 10.0
        return results

    def _plugin_items(self):
        now = time.monotonic()
        if now < self._plugin_cache_until:
            return self._plugin_cache
        self._plugin_cache = [
            {
                "kind": "plugin",
                "id": plugin_id,
                "name": getattr(plugin, "name", plugin_id),
                "class": type(plugin).__name__,
                "module": type(plugin).__module__,
            }
            for plugin_id, plugin in qgis.utils.plugins.items()
        ]
        self._plugin_cache_until = now + 2.0
        return self._plugin_cache

    def _api_items(self):
        results = []
        for root_name, factory in self.API_ROOTS.items():
            obj = factory(self.iface)
            if obj is None:
                continue
            cache_key = (root_name, type(obj))
            cached = self._api_cache.get(cache_key)
            if cached is None:
                cached = []
                for member in dir(obj):
                    if member.startswith("_"):
                        continue
                    try:
                        attr = getattr(obj, member)
                    except Exception:
                        attr = None
                    if not callable(attr):
                        continue
                    cached.append(
                        {
                            "kind": "api",
                            "id": "{}.{}".format(root_name, member),
                            "name": member,
                            "owner": type(obj).__name__,
                        }
                    )
                self._api_cache[cache_key] = cached
            results.extend(cached)
        return results

    def search(self, query="", kinds=None, limit=30):
        wanted = set(kinds or ("processing", "plugin", "action", "widget", "api"))
        needle = _normalize(query).strip()
        query_tokens = _tokens(query)
        results = []
        if "processing" in wanted:
            for item in self._processing_items():
                _append_if_match(results, item, needle, query_tokens)
        if "plugin" in wanted:
            for item in self._plugin_items():
                _append_if_match(results, item, needle, query_tokens)
        if {"action", "widget"} & wanted:
            for runtime_id, obj in self.objects.refresh().items():
                item = self.objects.summarize(runtime_id, obj)
                if item["kind"] in wanted:
                    _append_if_match(results, item, needle, query_tokens)
        if "api" in wanted:
            for item in self._api_items():
                _append_if_match(results, item, needle, query_tokens)
        results.sort(key=lambda item: _score(item, needle))
        return {"query": query, "results": results[:limit], "truncated": len(results) > limit}

    def describe(self, kind, capability_id):
        if kind == "processing":
            algorithm = QgsApplication.processingRegistry().algorithmById(capability_id)
            if algorithm is None:
                raise KeyError("Processing algorithm not found")
            schemas = algorithm_schemas(algorithm)
            return {
                "kind": kind,
                "id": algorithm.id(),
                "name": algorithm.displayName(),
                "short_description": algorithm.shortDescription(),
                "help_url": algorithm.helpUrl(),
                "group": algorithm.group(),
                "provider": algorithm.provider().id() if algorithm.provider() else None,
                "flags": int(algorithm.flags()),
                **schemas,
                "outputs": [_output(item) for item in algorithm.outputDefinitions()],
            }
        if kind == "plugin":
            plugin = qgis.utils.plugins.get(capability_id)
            if plugin is None:
                raise KeyError("Enabled plugin not found")
            methods = []
            for name in dir(plugin):
                if name.startswith("_"):
                    continue
                try:
                    value = getattr(plugin, name)
                except Exception:
                    value = None
                if callable(value):
                    methods.append(_callable(name, value))
            return {
                "kind": kind,
                "id": capability_id,
                "class": type(plugin).__name__,
                "module": type(plugin).__module__,
                "methods": methods[:500],
            }
        if kind in {"action", "widget"}:
            obj = self.objects.get(capability_id)
            return self.objects.summarize(capability_id, obj)
        if kind == "api":
            root_name, separator, member = capability_id.partition(".")
            if not separator or root_name not in self.API_ROOTS:
                raise KeyError("Unknown API capability")
            obj = self.API_ROOTS[root_name](self.iface)
            value = getattr(obj, member)
            return {
                "kind": kind,
                "id": capability_id,
                "owner": type(obj).__name__,
                **_callable(member, value),
            }
        raise KeyError("Unknown capability kind")

    def invoke(self, kind, target, member, args=None, kwargs=None):
        if member.startswith("_"):
            raise ValueError("Private members cannot be invoked")
        if kind == "plugin":
            owner = qgis.utils.plugins.get(target)
            if owner is None:
                raise KeyError("Enabled plugin not found")
        elif kind == "api":
            factory = self.API_ROOTS.get(target)
            if factory is None:
                raise KeyError("Unknown API root")
            owner = factory(self.iface)
            if owner is None:
                raise KeyError("API root is not available in the current session")
        else:
            raise ValueError("kind must be plugin or api")
        value = getattr(owner, member, None)
        if not callable(value):
            raise KeyError("Public callable member not found")
        return value(*(args or []), **(kwargs or {}))

    def summary(self):
        registry = QgsApplication.processingRegistry()
        providers = [
            {
                "id": provider.id(),
                "name": provider.name(),
                "algorithm_count": len(provider.algorithms()),
                "active": provider.isActive(),
            }
            for provider in registry.providers()
        ]
        return {
            "processing_providers": providers,
            "processing_algorithms": sum(item["algorithm_count"] for item in providers),
            "enabled_plugins": sorted(qgis.utils.plugins),
        }


def _parameter(item):
    value = {
        "name": item.name(),
        "description": item.description(),
        "type": item.type(),
        "optional": bool(item.flags() & item.FlagOptional),
        "default": item.defaultValue(),
    }
    try:
        value["accepted_types"] = item.valueAsPythonString(item.defaultValue(), None)
    except Exception:
        value["accepted_types"] = None
    return value


def _output(item):
    return {"name": item.name(), "description": item.description(), "type": item.type()}


def _callable(name, value):
    try:
        signature = str(inspect.signature(value))
    except (TypeError, ValueError):
        signature = None
    return {
        "name": name,
        "signature": signature,
        "doc": (inspect.getdoc(value) or "")[:2000],
    }


def _clean(value):
    return str(value or "").replace("&", "").strip()


def _widget_text(obj):
    for method in ("text", "currentText", "placeholderText", "title"):
        candidate = getattr(obj, method, None)
        if callable(candidate):
            try:
                value = candidate()
            except Exception:
                value = None
            if value:
                return _clean(value)
    return None


def _normalize(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value).casefold())
        if not unicodedata.combining(character)
    )


def _tokens(value):
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _normalize(value))
        if len(token) > 2
    }


def _append_if_match(results, item, needle, query_tokens):
    haystack = _normalize(" ".join(str(value) for value in item.values()))
    if not needle:
        results.append({**item, "relevance": 0})
        return
    overlap = query_tokens & _tokens(haystack)
    phrase = needle in haystack
    if not overlap and not phrase:
        return
    name = _normalize(item.get("name", ""))
    identifier = _normalize(item.get("id", ""))
    relevance = len(overlap) * 20
    relevance += 60 if phrase else 0
    relevance += 40 if name == needle or identifier == needle else 0
    relevance += 15 if name.startswith(needle) else 0
    results.append(
        {
            **item,
            "relevance": relevance,
            "matched_terms": sorted(overlap),
        }
    )


def _score(item, needle):
    if not needle:
        return (item["kind"], str(item.get("name", item["id"])).casefold())
    name = str(item.get("name", "")).casefold()
    identifier = str(item.get("id", "")).casefold()
    return (
        -int(item.get("relevance", 0)),
        0 if name == needle else 1 if identifier == needle else 2 if name.startswith(needle) else 3,
        name,
    )
