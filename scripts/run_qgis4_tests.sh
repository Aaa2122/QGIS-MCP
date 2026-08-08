#!/usr/bin/env bash
set -uo pipefail

workspace="$(pwd)"
temporary="$(mktemp -d)"
log_file="$temporary/qgis4-tests.log"
console_file="$temporary/qgis4-console.log"
trap 'rm -rf "$temporary"' EXIT

export QGIS_PLUGINPATH="${QGIS_PLUGINPATH:-$workspace/plugin}"
export QGIS_MCP_REPO_ROOT="$workspace"
export QGIS_MCP_CONNECTION_FILE="$temporary/qgis-mcp-connection.json"
export QGIS_MCP_BENCHMARK_OUTPUT="$temporary/qgis4-benchmark.json"
export QGIS_MCP_TEST_LOG="$log_file"
export PYTHONPATH="$workspace/src:$workspace/plugin:$workspace${PYTHONPATH:+:$PYTHONPATH}"

set +e
timeout 360s xvfb-run -a qgis --nologo --noversioncheck \
  --profiles-path "$temporary/profiles" \
  --profile qgis4-mcp \
  --code "$workspace/scripts/run_qgis_windows_tests.py" \
  >"$console_file" 2>&1
qgis_status="$?"
set -e

cat "$console_file"
if [[ -f "$log_file" ]]; then
  cat "$log_file"
fi

if grep -Eq '^Ran [1-9][0-9]* tests? in ' "$log_file" \
  && grep -Eq '^OK$' "$log_file" \
  && ! grep -Eq '^(FAILED|QGIS integration suite failed)' "$log_file"; then
  echo "QGIS 4 integration suite passed (QGIS exit status: ${qgis_status})."
  exit 0
fi

echo "QGIS 4 integration suite failed (QGIS exit status: ${qgis_status})." >&2
exit "${qgis_status:-1}"
