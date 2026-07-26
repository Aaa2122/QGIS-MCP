from __future__ import annotations

import os
import re

from qgis.core import QgsProject

FIRMS_SOURCES = {
    "MODIS_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "VIIRS_SNPP_NRT",
}
DEFAULT_FRANCE_BOUNDS = [-5.5, 41.0, 10.0, 51.5]
DEFAULT_SATELLITE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)


class FireMapManager:
    def __init__(
        self,
        iface,
        data,
        layer_manager,
        cartography,
        layouts,
        verifier,
        state,
    ):
        self.iface = iface
        self.data = data
        self.layer_manager = layer_manager
        self.cartography = cartography
        self.layouts = layouts
        self.verifier = verifier
        self.state = state

    @staticmethod
    def catalog():
        return {
            "preset": "active_fires",
            "provider": "NASA LANCE FIRMS",
            "sources": sorted(FIRMS_SOURCES),
            "default_area": {"name": "France", "bounds": DEFAULT_FRANCE_BOUNDS},
            "day_range": {"minimum": 1, "maximum": 5},
            "authentication": {
                "type": "environment_variable",
                "default": "NASA_FIRMS_MAP_KEY",
                "registration": "https://firms.modaps.eosdis.nasa.gov/api/map_key/",
            },
            "default_basemap": "Esri World Imagery",
        }

    def build(
        self,
        bounds=None,
        days=1,
        source="VIIRS_SNPP_NRT",
        date=None,
        map_key_env="NASA_FIRMS_MAP_KEY",
        layer_name="Incendies actifs — NASA FIRMS",
        add_satellite=True,
        satellite_url=DEFAULT_SATELLITE_URL,
        layout_name="Incendies actifs en France",
        output_path=None,
        output_format=None,
    ):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(map_key_env)):
            raise ValueError("map_key_env must be an uppercase environment variable name")
        map_key = os.environ.get(map_key_env)
        if not map_key:
            raise ValueError(
                "NASA FIRMS requires a free MAP_KEY in the {} environment variable".format(
                    map_key_env
                )
            )
        if source not in FIRMS_SOURCES:
            raise ValueError("Unsupported FIRMS source")
        day_range = int(days)
        if not 1 <= day_range <= 5:
            raise ValueError("days must be between 1 and 5")
        area = [float(value) for value in (bounds or DEFAULT_FRANCE_BOUNDS)]
        if len(area) != 4:
            raise ValueError("bounds must contain west, south, east, north")
        west, south, east, north = area
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bounds are invalid")
        bbox = ",".join("{:g}".format(value) for value in area)
        url = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{}/{}/{}/{}".format(
            map_key, source, bbox, day_range
        )
        if date:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
                raise ValueError("date must use YYYY-MM-DD")
            url += "/{}".format(date)
        acquired = self.data.fetch(
            url=url,
            name=layer_name,
            cache_mode="refresh",
            max_age_seconds=0,
            max_bytes=64 * 1024 * 1024,
            add_to_project=True,
            x_field="longitude",
            y_field="latitude",
            crs="EPSG:4326",
            secret_values=[map_key],
        )
        fire_layer = QgsProject.instance().mapLayer(acquired["layers"][0]["id"])
        fire_layer.setCustomProperty("qgis_mcp/provenance/connector", "nasa_firms")
        fire_layer.setCustomProperty("qgis_mcp/provenance/source", source)
        fire_layer.setCustomProperty("qgis_mcp/provenance/day_range", day_range)
        self.cartography.style(
            fire_layer,
            mode="simple",
            color="#ff3b1f",
            opacity=0.9,
            size=3.2,
            width=0.5,
        )
        self.layer_manager.execute(
            "move_layer", layer=fire_layer.id(), group="Incendies", index=0
        )
        layers = {"fires": acquired["layers"][0]}
        if add_satellite:
            satellite = self.data.add_service(
                "xyz",
                satellite_url,
                "Satellite — Esri World Imagery",
                zmin=0,
                zmax=23,
            )
            self.layer_manager.execute(
                "move_layer", layer=satellite["id"], group="Fonds de carte", index=0
            )
            layers["satellite"] = satellite
        extent = fire_layer.extent()
        if extent.width() == 0 or extent.height() == 0:
            extent.grow(1)
        else:
            extent.grow(max(extent.width(), extent.height()) * 0.1)
        self.iface.mapCanvas().setExtent(extent)
        self.iface.mapCanvas().refresh()
        layout_result = None
        if layout_name:
            existing = QgsProject.instance().layoutManager().layoutByName(layout_name)
            if existing is None:
                layout_result = self.layouts.execute(
                    "create",
                    name=layout_name,
                    title="Incendies actifs détectés par satellite",
                    subtitle="France — source {} — {} jour(s)".format(source, day_range),
                    source_text="Données : NASA LANCE FIRMS · Fond : Esri World Imagery",
                )
            else:
                layout_result = {"name": existing.name(), "reused": True}
        export = None
        if output_path:
            if not layout_name:
                raise ValueError("layout_name is required when output_path is set")
            export = self.layouts.execute(
                "export",
                name=layout_name,
                path=output_path,
                format=output_format,
            )
        self.state.touch(
            "connector.fire_map",
            {"layer_id": fire_layer.id(), "source": source, "days": day_range},
        )
        return {
            "preset": "active_fires",
            "area": {"bounds": area},
            "source": source,
            "layers": layers,
            "layout": layout_result,
            "export": export,
            "verification": self.verifier.verify(require_layout=bool(layout_name)),
        }
