"""Write the core OpenAPI document to the admin UI, which generates its typed client from it.

Usage (repo root): uv run python services/core/scripts/export_openapi.py
"""

import json
from pathlib import Path

from mes_core.main import create_app

OUT = Path(__file__).resolve().parents[3] / "frontend" / "packages" / "api-client" / "src" / "core-openapi.json"

if __name__ == "__main__":
    OUT.write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
    print(f"wrote {OUT}")
