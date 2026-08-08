from __future__ import annotations

import pytest

from qgis_mcp.plugin_advisor import PluginAdvisor, PluginCatalog

CATALOG_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<plugins>
  <pyqgis_plugin name="QuickOSM" version="1.0" plugin_id="1">
    <description>Download OpenStreetMap data with Overpass.</description>
    <about>Query roads, buildings and other OSM features.</about>
    <trusted>True</trusted>
    <qgis_minimum_version>3.20</qgis_minimum_version>
    <qgis_maximum_version>4.99</qgis_maximum_version>
    <file_name>QuickOSM.1.0.zip</file_name>
    <author_name>QGIS contributor</author_name>
    <experimental>False</experimental>
    <deprecated>False</deprecated>
    <tags>osm,overpass,download</tags>
    <downloads>1000000</downloads>
    <average_vote>4.8</average_vote>
    <rating_votes>500</rating_votes>
    <update_date>2026-07-01T00:00:00+00:00</update_date>
    <repository>https://example.test/quickosm</repository>
  </pyqgis_plugin>
  <pyqgis_plugin name="QuickOSM" version="2.0" plugin_id="1">
    <description>Download OpenStreetMap data with Overpass.</description>
    <about>Query roads, buildings and other OSM features.</about>
    <trusted>True</trusted>
    <qgis_minimum_version>3.20</qgis_minimum_version>
    <qgis_maximum_version>4.99</qgis_maximum_version>
    <file_name>QuickOSM.2.0.zip</file_name>
    <author_name>QGIS contributor</author_name>
    <experimental>False</experimental>
    <deprecated>False</deprecated>
    <tags>osm,overpass,download</tags>
    <downloads>2000000</downloads>
    <average_vote>4.9</average_vote>
    <rating_votes>800</rating_votes>
    <update_date>2026-08-01T00:00:00+00:00</update_date>
  </pyqgis_plugin>
  <pyqgis_plugin name="OSM Building Extractor" version="3.1" plugin_id="2">
    <description>Extract OpenStreetMap buildings for mapping.</description>
    <about>Focused OSM building downloader.</about>
    <trusted>False</trusted>
    <qgis_minimum_version>3.34</qgis_minimum_version>
    <qgis_maximum_version>3.99</qgis_maximum_version>
    <file_name>osm_buildings.3.1.zip</file_name>
    <author_name>Community author</author_name>
    <experimental>False</experimental>
    <deprecated>False</deprecated>
    <tags>osm,building,download</tags>
    <downloads>5000</downloads>
    <average_vote>4.1</average_vote>
    <rating_votes>20</rating_votes>
    <external_dependencies>requests-extra</external_dependencies>
    <plugin_dependencies>QuickMapServices&gt;=1.0</plugin_dependencies>
    <update_date>2026-06-01T00:00:00+00:00</update_date>
  </pyqgis_plugin>
  <pyqgis_plugin name="Experimental OSM" version="0.1" plugin_id="3">
    <description>Experimental OpenStreetMap downloader.</description>
    <trusted>False</trusted>
    <qgis_minimum_version>3.44</qgis_minimum_version>
    <qgis_maximum_version>4.99</qgis_maximum_version>
    <file_name>experimental_osm.0.1.zip</file_name>
    <experimental>True</experimental>
    <deprecated>False</deprecated>
    <tags>osm,download</tags>
  </pyqgis_plugin>
  <pyqgis_plugin name="Old OSM" version="0.5" plugin_id="4">
    <description>Deprecated OSM downloader.</description>
    <trusted>True</trusted>
    <qgis_minimum_version>2.0</qgis_minimum_version>
    <qgis_maximum_version>2.99</qgis_maximum_version>
    <file_name>old_osm.0.5.zip</file_name>
    <experimental>False</experimental>
    <deprecated>True</deprecated>
    <tags>osm</tags>
  </pyqgis_plugin>
</plugins>
"""


def _catalog(tmp_path):
    calls = []

    def fetcher(url, headers):
        calls.append((url, headers))
        return 200, {"etag": '"fixture"'}, CATALOG_XML

    return PluginCatalog(tmp_path, fetcher=fetcher), calls


def test_official_catalog_is_cached_deduplicated_and_compatibility_filtered(tmp_path):
    catalog, calls = _catalog(tmp_path)
    first = catalog.load("3.44.12")
    second = catalog.load("3.44.12")

    assert len(calls) == 1
    assert calls[0][0].endswith("?qgis=3.44")
    assert first["catalog"]["cache_hit"] is False
    assert second["catalog"]["cache_hit"] is True
    by_package = {item["package"]: item for item in first["plugins"]}
    assert by_package["QuickOSM"]["version"] == "2.0"
    assert "old_osm" not in by_package


def test_advisor_prefers_installed_capabilities_and_limits_new_recommendations(tmp_path):
    catalog, _ = _catalog(tmp_path)
    advisor = PluginAdvisor(catalog)
    installed = [
        {
            "package": "QuickOSM",
            "loaded": True,
            "active": True,
            "metadata": {
                "name": "QuickOSM",
                "description": "Download OpenStreetMap data with Overpass",
                "tags": "osm,overpass,download",
            },
        }
    ]
    result = advisor.recommend(
        "download OpenStreetMap buildings",
        "3.44.12",
        installed,
        [{"name": "qgis_data_fetch", "relevance": 40}],
        limit=3,
    )

    assert result["priority"] == ["native_qgis", "installed_plugins", "new_plugins"]
    assert result["installed_matches"][0]["package"] == "QuickOSM"
    assert [item["package"] for item in result["recommendations"]] == ["osm_buildings"]
    installation = result["recommendations"][0]["installation"]
    assert installation["confirmation_required"] is True
    assert "Requires other QGIS plugins." in result["recommendations"][0]["risks"]

    with pytest.raises(ValueError, match="confirmation"):
        advisor.validate_proposal(
            installation["proposal_id"],
            "osm_buildings",
            confirm_installation=False,
            confirm_untrusted=False,
        )
    with pytest.raises(ValueError, match="not marked trusted"):
        advisor.validate_proposal(
            installation["proposal_id"],
            "osm_buildings",
            confirm_installation=True,
            confirm_untrusted=False,
        )
    proposal = advisor.validate_proposal(
        installation["proposal_id"],
        "osm_buildings",
        confirm_installation=True,
        confirm_untrusted=True,
        idempotency_key="install-osm-buildings",
    )
    assert proposal["version"] == "3.1"
    advisor.complete_proposal(
        installation["proposal_id"], idempotency_key="install-osm-buildings"
    )
    replay = advisor.validate_proposal(
        installation["proposal_id"],
        "osm_buildings",
        confirm_installation=True,
        confirm_untrusted=True,
        idempotency_key="install-osm-buildings",
    )
    assert replay["version"] == "3.1"
    with pytest.raises(ValueError, match="already been used"):
        advisor.validate_proposal(
            installation["proposal_id"],
            "osm_buildings",
            confirm_installation=True,
            confirm_untrusted=True,
            idempotency_key="another-installation",
        )


def test_catalog_search_hides_experimental_plugins_by_default(tmp_path):
    catalog, _ = _catalog(tmp_path)
    advisor = PluginAdvisor(catalog)
    stable = advisor.search("OSM download", "3.44.12", [], limit=10)
    assert "experimental_osm" not in {item["package"] for item in stable["matches"]}

    all_matches = advisor.search(
        "OSM download",
        "3.44.12",
        [],
        limit=10,
        include_experimental=True,
    )
    assert "experimental_osm" in {item["package"] for item in all_matches["matches"]}


def test_plugin_description_is_compact_and_reports_local_state(tmp_path):
    catalog, _ = _catalog(tmp_path)
    advisor = PluginAdvisor(catalog)
    result = advisor.describe(
        "QuickOSM",
        "3.44.12",
        [{"package": "QuickOSM", "active": False}],
    )
    assert result["plugin"]["version"] == "2.0"
    assert result["installed"] is True
    assert result["active"] is False
