"""Minimal .env loader (no python-dotenv dependency).

Loads the first .env found (DOTENV_PATH, then CWD, then the repo root) into
os.environ without overriding anything already set, so a value passed by Docker,
an MCP client env block, or the shell always wins. Called from :mod:`config`
before it reads any variable, so every import path picks the file up.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_loaded = False


def _candidates() -> list[Path]:
    out = []
    env_path = os.environ.get("DOTENV_PATH")
    if env_path:
        out.append(Path(env_path))
    out.append(Path.cwd() / ".env")
    out.append(Path(__file__).resolve().parents[2] / ".env")  # repo root
    return out


def load_dotenv() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for path in _candidates():
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.split(" #", 1)[0].strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception as exc:
            print(f"WARN could not read {path}: {exc}", file=sys.stderr, flush=True)
        return  # first file wins
