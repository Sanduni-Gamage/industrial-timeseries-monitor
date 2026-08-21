# Screenshots

The six images embedded in the root README, captured from a live run against the full
22.7-million-reading archive.

## Regenerating them

Start both servers:

```powershell
.venv\Scripts\python.exe -m api
npm --prefix dashboard run dev
```

Then capture:

```powershell
.venv\Scripts\python.exe -m pip install playwright
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe scripts\capture_screenshots.py
```

Playwright is not a project dependency. It is only needed to regenerate these images after
a UI change, so it is installed on demand rather than carried in `pyproject.toml`.

Everything is captured at 1280 px wide, dark theme, 2x device scale, full page.

## What each one shows

| Filename | URL | Content |
|---|---|---|
| `overview.png` | `http://localhost:5173/` | Equipment status with its written reason, the four stat tiles, "What needs attention", and the data-quality bars |
| `sensor-explorer.png` | `http://localhost:5173/sensors?sensor=TP3` | Sensor and machine-state selectors, the 7-day preset active, and the readings chart |
| `anomalies.png` | `http://localhost:5173/anomalies` | The paginated table with plain-English machine states and the expected-range column |
| `data-quality.png` | `http://localhost:5173/quality` | The "complete is not the same as correct" panel and the disposition table |
| `quality-report.png` | `reports/data_quality_report.html` | The generated HTML report |
| `swagger.png` | `http://localhost:8000/docs` | The 14 endpoints grouped by tag |

## Two things the script gets right on purpose

The sensor explorer uses TP3 over the default 7-day preset, which lands on a window
containing three sharp pressure drops. A flat line would make the whole project look
inert, so it is worth checking the chart shows movement before committing the image.

The anomalies page is captured with filters cleared, so the count reads "1-50 of 15,196".
That number is what tells a reader the pagination and the data volume are both real.

## Known cosmetic issue

At 1280 px the anomalies table truncates its rightmost column, so the rule names read
"Drifted from the refere" and "Unusual for recent beh". Widening the capture viewport or
shortening those labels would fix it.
