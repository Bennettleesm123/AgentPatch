"""Minimal .env loading and secret redaction (no third-party dependency)."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_env(*names: str) -> str:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    raise SystemExit(f"missing credential: set {' or '.join(names)} in .env (never commit it)")


def redact(text: str) -> str:
    """Remove any known secret values from text before it is printed or logged."""
    for key, value in os.environ.items():
        if value and len(value) >= 8 and any(t in key for t in ("KEY", "TOKEN", "SECRET")):
            text = text.replace(value, "[REDACTED]")
    return text
