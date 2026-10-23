"""Minimal .env loader for local development.

Reads KEY=value lines from pipeline/.env into os.environ. Variables that are already set
(for example GitHub Actions secrets) are never overridden.
"""

import os
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    path = path or PIPELINE_DIR / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if name and value:
            os.environ.setdefault(name, value)
