# Contributing to QGIS Agent MCP

Thank you for helping make geospatial agent workflows safer, faster and more reliable. Contributions can include bug reports, QGIS compatibility findings, documentation, tests, tool schemas and implementation changes.

By participating, you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Choose the right channel

- Use [Q&A Discussions](https://github.com/Aaa2122/QGIS-MCP/discussions/categories/q-a) for setup or usage help.
- Use [Ideas Discussions](https://github.com/Aaa2122/QGIS-MCP/discussions/categories/ideas) to explore an early proposal.
- Open a structured Issue for a reproducible bug or an implementation-ready feature request.
- Follow [SECURITY.md](SECURITY.md) for vulnerabilities. Never publish secrets or sensitive project data.

## Development setup

QGIS Agent MCP has a standalone MCP server under `src/qgis_mcp` and a QGIS plugin under `plugin/qgis_agent_mcp`.

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the standard checks before submitting a change:

```bash
ruff check src plugin scripts tests benchmarks
python -m compileall -q src plugin scripts tests benchmarks
pytest
python scripts/build_plugin.py
python scripts/check_qgis_plugin_security.py
```

Changes that touch PyQGIS, rendering, Processing, operations or the bridge should also be tested in a real supported QGIS installation. CI runs the QGIS 3.44 LTR suite and the QGIS 4 / Qt 6 compatibility suite.

## Design expectations

- Keep project data local unless an explicitly open-world tool is being used.
- Do not expose arbitrary Python execution.
- Bound tool inputs, outputs, pagination and retained artifacts.
- Preserve typed schemas, titles, annotations and structured tool errors.
- Use revision preconditions and idempotency for mutations.
- Keep `tools/list` deterministic and avoid unnecessary model context.
- Support QGIS 3.44 LTR through QGIS 4.x using `qgis.PyQt` and compatible enum forms.
- Add regression tests for every behavioural change.

## Pull requests

1. Create a focused branch from the latest `main`.
2. Keep unrelated local files out of the commit.
3. Explain the user impact, safety implications and validation performed.
4. Update schemas, tests, documentation and version notes together when applicable.
5. Keep the PR reviewable; split unrelated features into separate PRs.

Maintainers may ask for a smaller scope, additional QGIS evidence or compatibility changes before merging. Contributions are accepted under the repository's MIT License.
