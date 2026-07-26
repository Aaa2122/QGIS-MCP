from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import quote, urlsplit

from qgis.core import (
    QgsBlockingNetworkRequest,
    QgsNetworkReplyContent,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .autonomy import DataCache, NetworkPolicy, PolicyError, redact_url, safe_extract_zip
from .serialize import layer_summary

DEFAULT_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
VECTOR_EXTENSIONS = {".csv", ".geojson", ".gpkg", ".json", ".kml", ".shp", ".gml"}
RASTER_EXTENSIONS = {".tif", ".tiff", ".vrt", ".jp2", ".asc", ".img"}


class DataAcquisitionManager:
    def __init__(self, state, log, network_policy=None, cache=None):
        self.state = state
        self.log = log
        self.policy = network_policy or NetworkPolicy()
        self.cache = cache or DataCache()

    def fetch(
        self,
        url,
        name=None,
        authcfg=None,
        cache_mode="reuse",
        max_age_seconds=3600,
        max_bytes=DEFAULT_MAX_DOWNLOAD_BYTES,
        expected_sha256=None,
        add_to_project=True,
        provider=None,
        x_field=None,
        y_field=None,
        delimiter=",",
        crs="EPSG:4326",
        secret_values=None,
    ):
        if cache_mode not in {"reuse", "refresh"}:
            raise ValueError("cache_mode must be reuse or refresh")
        max_bytes = int(max_bytes)
        if not 1 <= max_bytes <= 256 * 1024 * 1024:
            raise PolicyError("max_bytes must be between 1 and 268435456")
        validated = self.policy.validate(url)
        redacted = redact_url(validated, secret_values)
        cached = None
        if cache_mode == "reuse":
            cached = self.cache.lookup(validated, int(max_age_seconds))
        if cached is None:
            cached = self._download(
                validated,
                redacted,
                authcfg,
                max_bytes,
                expected_sha256,
                secret_values,
            )
        result = {"download": cached, "source_url": redacted}
        if add_to_project:
            layers = self._load(
                cached["path"],
                name,
                provider,
                x_field,
                y_field,
                delimiter,
                crs,
                redacted,
                cached,
            )
            result["layers"] = [layer_summary(layer) for layer in layers]
        self.log.add(
            "data.fetch",
            "Fetched {}".format(redacted),
            data={
                "cache_hit": cached.get("cache_hit", False),
                "size": cached["size"],
                "layers": len(result.get("layers", [])),
            },
        )
        return result

    def add_service(
        self,
        kind,
        url,
        name,
        authcfg=None,
        layer=None,
        crs=None,
        format="image/png",
        zmin=0,
        zmax=20,
    ):
        kind = str(kind).casefold()
        validated = self.policy.validate(url)
        if kind == "xyz":
            source = "type=xyz&url={}&zmin={}&zmax={}".format(
                quote(validated, safe=":/{x}{y}{z}[]@!$'()*+,;=%"), int(zmin), int(zmax)
            )
            created = QgsRasterLayer(source, name, "wms")
        elif kind in {"wms", "wmts"}:
            source = "url={}&format={}&crs={}".format(
                quote(validated, safe=":/?&=%"), format, crs or "EPSG:3857"
            )
            if layer:
                source += "&layers={}".format(quote(str(layer), safe=".:_-"))
            if authcfg:
                source += "&authcfg={}".format(quote(str(authcfg), safe=""))
            created = QgsRasterLayer(source, name, "wms")
        elif kind == "wfs":
            source = validated
            if layer:
                separator = "&" if "?" in source else "?"
                source += "{}typename={}".format(separator, quote(str(layer), safe=":_-"))
            if authcfg:
                separator = "&" if "?" in source else "?"
                source += "{}authcfg={}".format(separator, quote(str(authcfg), safe=""))
            created = QgsVectorLayer(source, name, "WFS")
        elif kind == "arcgis_featureserver":
            created = QgsVectorLayer("url='{}'".format(validated), name, "arcgisfeatureserver")
        elif kind == "arcgis_mapserver":
            created = QgsRasterLayer("url='{}'".format(validated), name, "arcgismapserver")
        else:
            raise ValueError("Unsupported service kind")
        if not created.isValid():
            raise ValueError("QGIS could not create a valid {} layer".format(kind))
        QgsProject.instance().addMapLayer(created)
        self._set_provenance(
            created,
            {
                "kind": kind,
                "url": redact_url(validated),
                "authcfg": authcfg or None,
            },
        )
        self.state.touch("layer.added_service", {"layer_id": created.id(), "kind": kind})
        return layer_summary(created)

    def refresh(self, layer):
        layer.reload()
        layer.triggerRepaint()
        self.state.touch("layer.refreshed", {"layer_id": layer.id()})
        return layer_summary(layer)

    @staticmethod
    def provenance(layer):
        return {
            key.removeprefix("qgis_mcp/provenance/"): layer.customProperty(key)
            for key in layer.customPropertyKeys()
            if key.startswith("qgis_mcp/provenance/")
        }

    @staticmethod
    def catalog():
        return {
            "downloads": sorted(VECTOR_EXTENSIONS | RASTER_EXTENSIONS | {".zip"}),
            "services": ["xyz", "wms", "wmts", "wfs", "arcgis_featureserver", "arcgis_mapserver"],
            "authentication": "Use a QGIS authcfg ID; credentials are never returned.",
            "limits": {"default_max_download_bytes": DEFAULT_MAX_DOWNLOAD_BYTES},
        }

    def _download(
        self, url, redacted, authcfg, max_bytes, expected_sha256, secret_values=None
    ):
        network = QgsBlockingNetworkRequest()
        if authcfg:
            network.setAuthCfg(str(authcfg))
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(QByteArray(b"Accept"), QByteArray(b"*/*"))
        request.setRawHeader(
            QByteArray(b"User-Agent"), QByteArray(b"QGIS-Agent-MCP/0.4")
        )
        error = network.get(request, True)
        if error != QgsBlockingNetworkRequest.NoError:
            message = network.errorMessage()
            message = message.replace(url, redacted)
            for secret in secret_values or ():
                if secret:
                    message = message.replace(str(secret), "***")
            raise RuntimeError("Data download failed: {}".format(message))
        reply = network.reply()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if status is not None and int(status) >= 400:
            raise RuntimeError("Data source returned HTTP {}".format(status))
        payload = bytes(reply.content())
        if len(payload) > max_bytes:
            raise PolicyError("Downloaded data exceeds the configured size limit")
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 and digest.casefold() != str(expected_sha256).casefold():
            raise PolicyError("Downloaded data does not match expected SHA-256")
        content_type = bytes(reply.rawHeader(QByteArray(b"Content-Type"))).decode(
            "latin-1", "replace"
        ).split(";", 1)[0]
        disposition = bytes(reply.rawHeader(QByteArray(b"Content-Disposition"))).decode(
            "latin-1", "replace"
        )
        filename = QgsNetworkReplyContent.extractFileNameFromContentDispositionHeader(disposition)
        if not filename:
            filename = Path(urlsplit(url).path).name
        if not Path(filename).suffix:
            filename += mimetypes.guess_extension(content_type) or ".bin"
        return self.cache.put(
            url,
            payload,
            filename,
            {
                "source_url": redacted,
                "content_type": content_type,
                "http_status": int(status) if status is not None else None,
            },
        )

    def _load(
        self,
        path,
        name,
        provider,
        x_field,
        y_field,
        delimiter,
        crs,
        source_url,
        metadata,
    ):
        path = Path(path)
        candidates = [path]
        if path.suffix.casefold() == ".zip":
            candidates = safe_extract_zip(path, path.parent / "extracted")
        loadable = [
            candidate
            for candidate in candidates
            if candidate.suffix.casefold() in VECTOR_EXTENSIONS | RASTER_EXTENSIONS
        ]
        if not loadable:
            raise ValueError("Download contains no supported spatial dataset")
        layers = []
        for index, candidate in enumerate(loadable):
            suffix = candidate.suffix.casefold()
            layer_name = name or candidate.stem
            if len(loadable) > 1 and name:
                layer_name = "{} {}".format(name, index + 1)
            if suffix == ".csv":
                uri = QUrl.fromLocalFile(str(candidate))
                query = "delimiter={}".format(quote(str(delimiter), safe=""))
                if x_field and y_field:
                    query += "&xField={}&yField={}&crs={}".format(
                        quote(str(x_field), safe=""),
                        quote(str(y_field), safe=""),
                        quote(str(crs), safe=":"),
                    )
                uri.setQuery(query)
                created = QgsVectorLayer(uri.toString(), layer_name, "delimitedtext")
            elif suffix in RASTER_EXTENSIONS:
                created = QgsRasterLayer(str(candidate), layer_name, provider or "gdal")
            else:
                created = QgsVectorLayer(str(candidate), layer_name, provider or "ogr")
            if not created.isValid():
                continue
            QgsProject.instance().addMapLayer(created)
            self._set_provenance(
                created,
                {
                    "kind": "download",
                    "url": source_url,
                    "sha256": metadata["sha256"],
                    "fetched_at": metadata["fetched_at"],
                    "cache_path": str(candidate),
                },
            )
            self.state.touch("layer.added_download", {"layer_id": created.id()})
            layers.append(created)
        if not layers:
            raise ValueError("QGIS could not load the downloaded dataset")
        return layers

    @staticmethod
    def _set_provenance(layer, values):
        for key, value in values.items():
            if value is not None:
                layer.setCustomProperty("qgis_mcp/provenance/" + key, value)
