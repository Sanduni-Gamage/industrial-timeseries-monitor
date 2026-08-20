"""Capture the README screenshots from the running dashboard and API.

Needs both servers up and Playwright installed. Playwright is not a project dependency,
because it is only used to regenerate images after a UI change:

    pip install playwright && playwright install chromium
    python -m api
    npm --prefix dashboard run dev
    python scripts/capture_screenshots.py

Exit codes: 0 all captured · 1 at least one failed · 2 Playwright is not installed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXIT_OK, EXIT_FAILURE, EXIT_MISSING_DEP = 0, 1, 2

UI = "http://localhost:5173"
API = "http://127.0.0.1:8000"

#: (filename, url template, selector to wait for, extra settle time in ms).
#: The settle time is the chart animation; without it Recharts is captured mid-draw.
SHOTS = [
    ("overview.png", f"{UI}/", "main", 2500),
    ("sensor-explorer.png", f"{UI}/sensors?sensor=TP3", "main", 4000),
    ("anomalies.png", f"{UI}/anomalies", "main", 3500),
    ("data-quality.png", f"{UI}/quality", "main", 3000),
    ("quality-report.png", "{report}", "body", 1200),
    ("swagger.png", f"{API}/docs", ".swagger-ui", 3000),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "screenshots")
    parser.add_argument("--report", type=Path,
                        default=PROJECT_ROOT / "reports" / "data_quality_report.html")
    parser.add_argument("--width", type=int, default=1280)
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. See this file's docstring.", file=sys.stderr)
        return EXIT_MISSING_DEP

    args.out.mkdir(parents=True, exist_ok=True)
    report_uri = args.report.resolve().as_uri()
    failures = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": args.width, "height": 900},
            device_scale_factor=2,
            # The dashboard follows prefers-color-scheme; Swagger UI ignores it.
            color_scheme="dark",
        )
        page = context.new_page()

        for name, url, selector, settle in SHOTS:
            url = url.format(report=report_uri)
            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
                page.wait_for_selector(selector, timeout=20_000)
                page.wait_for_timeout(settle)
                page.screenshot(path=str(args.out / name), full_page=True)
                print(f"  {name:<22} <- {url}")
            except Exception as exc:
                failures += 1
                print(f"  {name:<22} FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)

        browser.close()

    if failures:
        print(f"{failures} screenshot(s) failed. Are both servers running?", file=sys.stderr)
        return EXIT_FAILURE
    print(f"Captured {len(SHOTS)} screenshots to {args.out}.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
