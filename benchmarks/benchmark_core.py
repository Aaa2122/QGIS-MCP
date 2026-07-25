from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from qgis_agent_mcp.revisions import ResourceRevisionIndex, layer_uri  # noqa: E402


def main():
    index = ResourceRevisionIndex()
    samples = []
    for run in range(20):
        started = time.perf_counter()
        for layer_number in range(10000):
            uri = layer_uri("layer-{}".format(layer_number), "selection")
            index.bump(("qgis://session", uri), run * 10000 + layer_number)
        samples.append((time.perf_counter() - started) * 1000)
    print(
        json.dumps(
            {
                "operation": "20000 resource revision updates",
                "samples_ms": samples,
                "median_ms": statistics.median(samples),
                "max_ms": max(samples),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
