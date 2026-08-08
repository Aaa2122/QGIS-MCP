<p align="center">
  <img src="assets/social/github-social-preview-v2.png" alt="QGIS Agent MCP — geospatial agents illustrated by a grey heron over a field atlas" width="100%">
</p>

# QGIS Agent MCP

[![CI](https://github.com/Aaa2122/QGIS-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/Aaa2122/QGIS-MCP/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Aaa2122/QGIS-MCP)](https://github.com/Aaa2122/QGIS-MCP/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Discussions](https://img.shields.io/github/discussions/Aaa2122/QGIS-MCP)](https://github.com/Aaa2122/QGIS-MCP/discussions)

Connect AI agents to a live QGIS Desktop session through a secure local MCP bridge.

## Why it is different

- **100+ specialist QGIS tools** across projects, vector, raster, Processing, databases, cartography, layouts, 3D, point clouds and more.
- **Low context cost:** only 7 core/discovery tools are loaded by default; `qgis_context` assembles the relevant project state, schemas and live capabilities under a strict byte budget.
- **Task-aware plugin advisor:** checks native QGIS and installed plugins before ranking up to three compatible candidates from the official repository; installation requires an explicit, short-lived confirmation proposal.
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
  <img src="capture_demo/wildfire-monitoring-france-overview.png" alt="Wildfire monitoring map generated from a blank QGIS project, France overview" width="49%">
  <img src="capture_demo/wildfire-monitoring-bordeaux-detail.png" alt="Wildfire monitoring map generated from a blank QGIS project, Bordeaux detail" width="49%">
</p>

<p align="center"><em>Blank project → national overview → local detection detail</em></p>

### Demo 2 — Agricultural data exploration

**Starting point:** blank QGIS project → **result:** annual agricultural parcel data combined with satellite imagery, from the national view down to a detailed parcel map with a crop legend.

<p align="center">
  <img src="capture_demo/agriculture-france-overview.png" alt="Agricultural data map generated from a blank QGIS project, France overview" width="32%">
  <img src="capture_demo/agriculture-parcels-overview.png" alt="Agricultural parcel map generated from a blank QGIS project, regional overview" width="32%">
  <img src="capture_demo/agriculture-parcel-detail.png" alt="Agricultural parcel detail and crop legend generated from a blank QGIS project" width="32%">
</p>

<p align="center"><em>Blank project → national coverage → parcel overview → detailed crop map</em></p>

## Safety

The bridge accepts only authenticated local connections and does not expose arbitrary Python execution. Agents use typed tools, QGIS Processing algorithms and guarded workflows instead.

## Community

- Read the [contribution guide](CONTRIBUTING.md) before opening a Pull Request.
- Ask setup and usage questions in [Discussions](https://github.com/Aaa2122/QGIS-MCP/discussions/categories/q-a).
- Report reproducible problems through the structured [Issue forms](https://github.com/Aaa2122/QGIS-MCP/issues/new/choose).
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md) and report vulnerabilities privately using the [Security Policy](SECURITY.md).
- See the [Changelog](CHANGELOG.md), [Support policy](SUPPORT.md) and [Governance](GOVERNANCE.md).

## Compatibility

- QGIS 3.44 LTR through QGIS 4.x (Qt 5 and Qt 6)
- OpenCode, Codex, Claude Code, Cursor, Google Antigravity and standard stdio MCP clients
- No external Python runtime dependency in the packaged plugin
- Validated on QGIS LTR 3.44.12 and QGIS 4.2/Qt 6 with live integration scenarios

### Runtime reliability notes

- The 0.4.9 package is declared for QGIS 3.44 through 4.x and is a dual-era MCP server: it supports stateless MCP 2026-07-28 requests while preserving initialization-based clients through 2025-11-25 and earlier revisions.
- MCP 0.4.9 keeps tool and result context bounded: `qgis_context` merges a revisioned snapshot, confidence-ranked schemas and live Processing matches into one 2–32 KiB context pack; adaptive discovery expands only the best one or two schemas.
- Negotiated MCP Tasks make long verification, visual review, workflow and batch calls durable and pollable. Declarative batches can reference prior outputs, project JSON Pointer slices and retain intermediates as summaries or handles without arbitrary code execution.
- Mutation diagnostics use a crash-resilient append-only journal with periodic compaction instead of rewriting the complete diagnostic state twice for every call.
- Plugin recommendations use a cached, compatibility-filtered index of the official QGIS repository. Native and already-installed capabilities are preferred; deprecated plugins are excluded, experimental plugins are opt-in, and untrusted candidates require additional confirmation.
- The bridge bounds pending work, reserves capacity for cancellation/status calls, serializes QGIS mutations on the main thread and exposes queue timing in diagnostics. Resource updates are emitted only for subscribed URIs.
- Session snapshots reuse unchanged layer summaries; `detail=summary` avoids provider feature counts and extents, while `since_revision` returns only changed live layers plus removed layer IDs. Feature queries support bbox filters, explicit ordering, FID cursors and an independent byte budget.
- LAS/LAZ downloads and project additions use `QgsPointCloudLayer` with the PDAL provider. For classification filtering, prefer the QGIS Processing algorithm `pdal:filter` and a QGIS expression such as `"Classification" = 2` instead of invoking the PDAL CLI. The data catalog also reports known CLI constraints (`--summary`/`--metadata`, `filters.range`, forward-slash JSON paths, and optional `filters.count`).
- Processing outputs requested with `add_to_project=true` are resolved and verified as QGIS layers before success is reported. Existing output files which are loaded in the project are rejected before execution to avoid Windows file-lock failures.
- QGIS 3.44 on Windows bypasses both unsafe PyQGIS paths: MCP queues the native C++ `new3DMapCanvas` slot, then discovers the initialized canvas as an individual Qt `QWindow` instead of calling the unstable `mapCanvases3D()` list wrapper. Poll `qgis_3d_views` with `action=list` after the short initialization period; the requested name remains available as an alias for list/configure/close.
- Screenshot responses include MCP image content plus bounded structural pixel checks. Structural checks can detect blank captures, but a vision-capable client is still required for aesthetic review.

Licensed under the [MIT License](LICENSE).
