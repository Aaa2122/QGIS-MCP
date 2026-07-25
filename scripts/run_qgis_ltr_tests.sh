#!/usr/bin/env bash
set -uo pipefail

log_file="$(mktemp)"
trap 'rm -f "$log_file"' EXIT

set +e
timeout 180s xvfb-run -a -s '-screen 0 1920x1080x24' \
  qgis --version-migration --nologo --code /usr/bin/qgis_testrunner.py \
  tests.qgis.run_all 2>&1 | tee "$log_file"
qgis_status="${PIPESTATUS[0]}"
set -e

if grep -Eq '^Ran [1-9][0-9]* tests? in ' "$log_file" \
  && grep -Eq '^OK$' "$log_file" \
  && ! grep -Eq '^(FAILED|QGIS Test Runner Inside - \[FAILED\])' "$log_file"; then
  echo "QGIS integration suite passed (QGIS exit status: ${qgis_status})."
  exit 0
fi

echo "QGIS integration suite failed (QGIS exit status: ${qgis_status})." >&2
exit "${qgis_status:-1}"
