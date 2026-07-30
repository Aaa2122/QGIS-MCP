<p align="center">
  <img src="assets/social/github-social-preview-v2.png" alt="QGIS Agent MCP — geospatial agents illustrated by a grey heron over a field atlas" width="100%">
</p>

# QGIS Agent MCP

Connect AI agents to a live QGIS Desktop session through a secure local MCP bridge.

## Why it is different

- **100+ specialist QGIS tools** across projects, vector, raster, Processing, databases, cartography, layouts, 3D, point clouds and more.
- **Low context cost:** only 14 core tools are loaded by default; agents discover and activate specialist tools when needed.
- **One-click connection:** configure Codex or Claude Code directly from QGIS, with a universal MCP configuration for other clients.
- **Safe autonomy:** revision guards, idempotency, checkpoints, atomic workflows and recovery after a QGIS restart.
- **Visual review:** agents can inspect rendered maps, apply bounded corrections and review the result again.
- **Local by design:** authenticated loopback transport; project data stays inside QGIS.

## Install

1. Download the latest `qgis_agent_mcp-*.zip` from [Releases](https://github.com/Aaa2122/QGIS-MCP/releases).
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Enable **QGIS Agent MCP** and click **Connect an AI client**.
4. Select **Connect / Repair** for Codex or Claude Code, then restart the AI client.

No port, token or configuration file needs to be copied manually.

## Try it

> Inspect this QGIS project, fix invalid layers, improve the cartography, create a print layout and visually review the final map.

The agent can also search the complete specialist catalog without loading every tool schema into its context.

## Safety

The bridge accepts only authenticated local connections and does not expose arbitrary Python execution. Agents use typed tools, QGIS Processing algorithms and guarded workflows instead.

## Compatibility

- QGIS 3.28 or newer
- Codex, Claude Code and standard stdio MCP clients
- No external Python runtime dependency in the packaged plugin
- Validated on QGIS LTR 3.44.12 with 19 live integration scenarios

Experimental release. Licensed under the [MIT License](LICENSE).
