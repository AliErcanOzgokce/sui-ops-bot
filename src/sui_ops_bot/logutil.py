"""Logging and the audit trail.

Two sinks in one place so both entrypoints share them:

* The auto-tracker (:mod:`sui_ops_bot.slackbot`) is a long-running process, so
  ``log`` goes to stdout and ``audit`` appends JSONL to ``AUDIT_LOG_PATH``.
* The MCP server (:mod:`sui_ops_bot.mcpserver`) speaks JSON-RPC over stdout, so
  it calls :func:`use_stderr` first and everything is rerouted to stderr. Nothing
  but protocol frames may touch stdout there.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from . import config

_lock = threading.Lock()
_stderr_only = False


def use_stderr() -> None:
    """Route every subsequent log/audit line to stderr (call before anything logs
    when stdout is reserved, e.g. the MCP stdio transport)."""
    global _stderr_only
    _stderr_only = True


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.TZ)
    except Exception:
        return ZoneInfo("UTC")


def today_str() -> str:
    return datetime.now(tz()).date().isoformat()


def log(msg: str) -> None:
    stream = sys.stderr if _stderr_only else sys.stdout
    print(f"{datetime.now(UTC).isoformat()} {msg}", file=stream, flush=True)


def audit(kind: str, **fields) -> None:
    rec = {"ts": datetime.now(UTC).isoformat(), "kind": kind, **fields}
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _lock:
        if _stderr_only:
            print("AUDIT " + line, file=sys.stderr, flush=True)
            return
        print("AUDIT " + line, flush=True)
        try:
            with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # never let audit failure crash a handler
            print(f"WARN could not write audit file: {exc}", flush=True)
