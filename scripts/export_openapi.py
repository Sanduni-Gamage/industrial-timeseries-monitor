"""Export the OpenAPI schema to a file.

Produced from the FastAPI app object, so it cannot drift from the code, and written to
disk so type generation works offline and without a database.

Usage
-----
    python scripts/export_openapi.py [--out dashboard/src/api/openapi.json]

Exit codes: 0 written · 1 failure
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.config import PROJECT_ROOT  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "dashboard" / "src" / "api" / "openapi.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    try:
        # Imported here so a missing database or bad configuration surfaces as a clear
        # message rather than an import-time traceback.
        from api.main import app
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Could not import the API application: {exc}")
        return 1

    spec = app.openapi()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    print(f"[INFO] Wrote {args.out}")
    print(f"[INFO] {len(spec['paths'])} paths, "
          f"{len(spec.get('components', {}).get('schemas', {}))} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
