"""Measure API latency against the live database.

Runs in-process through Starlette's TestClient, so the numbers describe query cost rather
than network overhead.

Usage
-----
    python scripts/benchmark_api.py [--runs 20] [--warmup 3]

Exit codes: 0 all endpoints within budget · 1 a failure occurred · 3 a budget was exceeded
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

EXIT_OK, EXIT_FAILURE, EXIT_BUDGET = 0, 1, 3

#: (label, path, params, p95 budget in ms).
#:
#: Budgets reflect what each endpoint is for. /summary backs a dashboard poll and gets a
#: tight one. /readings over a wide window is an explicit analytical request and gets a
#: looser one. A single global budget would be either meaninglessly loose or permanently
#: red.
SCENARIOS: list[tuple[str, str, dict | None, float]] = [
    ("health", "/health", None, 400),
    ("equipment list", "/equipment", None, 100),
    ("sensor list", "/sensors", None, 100),
    ("sensor by code", "/sensors/TP3", None, 100),
    ("equipment health", "/equipment/1/health", None, 300),
    ("failures", "/failures", None, 300),
    ("data quality (cached)", "/data-quality", None, 300),
    ("anomalies page", "/anomalies", {"limit": 50}, 400),
    ("anomalies filtered", "/anomalies",
     {"severity": "CRITICAL", "method": "ADAPTIVE_MAD", "limit": 50}, 400),
    ("readings raw 2h", "/readings/TP3",
     {"start": "2020-06-05T00:00:00", "end": "2020-06-05T02:00:00"}, 300),
    ("readings hourly 7d", "/readings/TP3",
     {"start": "2020-06-01T00:00:00", "end": "2020-06-08T00:00:00"}, 300),
    ("readings daily 7mo", "/readings/TP3",
     {"start": "2020-02-01T00:00:00", "end": "2020-09-01T00:00:00"}, 300),
    ("readings multi-sensor", "/readings",
     {"sensors": "TP2,TP3,OIL_TEMPERATURE", "start": "2020-06-05T00:00:00",
      "end": "2020-06-05T06:00:00"}, 500),
    ("trend state-aware 7d", "/trends/OIL_TEMPERATURE",
     {"start": "2020-06-01T00:00:00", "end": "2020-06-08T00:00:00",
      "operating_state": "LOADED"}, 400),
    ("summary (dashboard)", "/summary", None, 500),
]


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Exact for the small sample sizes used here."""
    ordered = sorted(values)
    index = min(int(round(fraction * len(ordered) + 0.5)) - 1, len(ordered) - 1)
    return ordered[max(index, 0)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=20, help="measured requests each")
    parser.add_argument("--warmup", type=int, default=3,
                        help="unmeasured requests first, so plan compilation and cache "
                             "population are not counted as latency")
    args = parser.parse_args(argv)

    logging.disable(logging.INFO)

    failures = 0
    over_budget: list[str] = []
    print(f"{'endpoint':<24} {'n':>4} {'min':>8} {'median':>8} {'p95':>8} {'max':>8} "
          f"{'budget':>8}  verdict")
    print("-" * 92)

    with TestClient(app) as client:
        for label, path, params, budget in SCENARIOS:
            for _ in range(args.warmup):
                client.get(path, params=params)

            timings: list[float] = []
            bad_status = None
            for _ in range(args.runs):
                started = time.perf_counter()
                response = client.get(path, params=params)
                timings.append((time.perf_counter() - started) * 1000)
                if response.status_code != 200:
                    bad_status = response.status_code

            if bad_status:
                failures += 1
                print(f"{label:<24} {'':>4} {'':>8} {'':>8} {'':>8} {'':>8} {'':>8}  "
                      f"HTTP {bad_status}")
                continue

            p95 = percentile(timings, 0.95)
            verdict = "ok" if p95 <= budget else "OVER BUDGET"
            if p95 > budget:
                over_budget.append(label)
            print(f"{label:<24} {len(timings):>4} {min(timings):>8.1f} "
                  f"{statistics.median(timings):>8.1f} {p95:>8.1f} {max(timings):>8.1f} "
                  f"{budget:>8.0f}  {verdict}")

    print("-" * 92)
    if failures:
        print(f"{failures} endpoint(s) returned a non-200 status.")
        return EXIT_FAILURE
    if over_budget:
        print(f"Over budget: {', '.join(over_budget)}")
        return EXIT_BUDGET
    print("All endpoints within their p95 budget.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
