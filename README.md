<p align="center">
  <img src="assets/social/github-social-preview-v2.png" alt="QGIS Agent MCP — geospatial agents illustrated by a grey heron over a field atlas" width="100%">
</p>

# QGIS Agent MCP

Connect AI agents to a live QGIS Desktop session through a secure local MCP bridge.

## Why it is different

- **100+ specialist QGIS tools** across projects, vector, raster, Processing, databases, cartography, layouts, 3D, point clouds and more.
- **Low context cost:** only 14 core tools are loaded by default; agents discover and activate specialist tools when needed.
- **One-click connection:** safely configure open-source OpenCode, Codex, Claude Code, Cursor or Google Antigravity directly from QGIS, with a universal MCP configuration for other clients.
- **Safe autonomy:** revision guards, idempotency, checkpoints, atomic workflows and recovery after a QGIS restart.
- **Visual review:** agents can inspect rendered maps, apply bounded corrections and review the result again.
- **Local by design:** authenticated loopback transport; project data stays inside QGIS.

## Install

1. Download the latest `qgis_agent_mcp-*.zip` from [Releases](https://github.com/Aaa2122/QGIS-MCP/releases).
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Enable **QGIS Agent MCP** and click **Connect an AI client**.
4. Select **Connect / Repair** for your AI client, follow the displayed restart guidance, then reopen the client.

No port, token or configuration file needs to be copied manually.

## Try it

> Inspect this QGIS project, fix invalid layers, improve the cartography, create a print layout and visually review the final map.

The agent can also search the complete specialist catalog without loading every tool schema into its context.

## Demos: from a blank project to a finished QGIS map

Both demos start from the same clean state: a **new, empty QGIS project** with no prepared layers, styles or layout. The user connects an AI client through QGIS Agent MCP, describes the task, and the agent builds the project in the live QGIS session.

### Demo 1 — Wildfire monitoring

**Starting point:** blank QGIS project → **result:** a France-wide monitoring map with prioritized detections, age categories, labels and a focused Bordeaux view.

<p align="center">
  <img src="capture%20demo/wildfire-monitoring-france-overview.png" alt="Wildfire monitoring map generated from a blank QGIS project, France overview" width="49%">
  <img src="capture%20demo/wildfire-monitoring-bordeaux-detail.png" alt="Wildfire monitoring map generated from a blank QGIS project, Bordeaux detail" width="49%">
</p>

<p align="center"><em>Blank project → national overview → local detection detail</em></p>

### Demo 2 — Agricultural data exploration

**Starting point:** blank QGIS project → **result:** annual agricultural parcel data combined with satellite imagery, from the national view down to a detailed parcel map with a crop legend.

<p align="center">
  <img src="capture%20demo/agriculture-france-overview.png" alt="Agricultural data map generated from a blank QGIS project, France overview" width="32%">
  <img src="capture%20demo/agriculture-parcels-overview.png" alt="Agricultural parcel map generated from a blank QGIS project, regional overview" width="32%">
  <img src="capture%20demo/agriculture-parcel-detail.png" alt="Agricultural parcel detail and crop legend generated from a blank QGIS project" width="32%">
</p>

<p align="center"><em>Blank project → national coverage → parcel overview → detailed crop map</em></p>

## Safety

The bridge accepts only authenticated local connections and does not expose arbitrary Python execution. Agents use typed tools, QGIS Processing algorithms and guarded workflows instead.

## Compatibility

- QGIS 3.44 LTR or newer in the 3.x series
- OpenCode, Codex, Claude Code, Cursor, Google Antigravity and standard stdio MCP clients
- No external Python runtime dependency in the packaged plugin
- Validated on QGIS LTR 3.44.12 with 19 live integration scenarios

Licensed under the [MIT License](LICENSE).
