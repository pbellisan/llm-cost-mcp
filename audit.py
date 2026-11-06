"""Audit log for the llm-cost-mcp demo: one JSON line per tool call.

Each line records WHAT was asked and HOW it went, never what came back:
    {"ts": ..., "tool": ..., "args": {...}, "status": "ok"|"error"|"rejected",
     "error_type": null|"ValueError", "row_count": int|null, "duration_ms": float}

status:
    ok        the tool ran and returned a result
    error     the tool ran and raised (for example an out-of-range argument)
    rejected  the call never reached the tool: schema validation failed or the
              tool name is unknown

* Result contents are never logged. Only a row count is kept.
* Error messages are never logged, only the exception class name.
* Arguments are sanitised: numbers/bools/None as is, strings cut to 50 chars
  with control characters removed, any other type replaced by its type name.
  For rejected calls at most 10 arguments are kept.
* A failure to write the log never breaks a tool call (a short note goes to
  stderr; stdout belongs to the MCP protocol).

This is the only file the server writes. It is separate from the data
database, which is always opened read-only. The file is created owner-only.
"""
import contextvars
import functools
import inspect
import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DEFAULT_AUDIT = Path(__file__).with_name("audit.jsonl")
MAX_STR = 50
MAX_REJECTED_ARGS = 10
_lock = threading.Lock()
# Per-request list the tool wrapper appends to, so the protocol-level hook can
# tell whether a failed call ever reached a tool (and was therefore logged).
_seen = contextvars.ContextVar("audit_seen", default=None)


def get_audit_path() -> Path:
    """Audit file: $LLM_COST_AUDIT if set, otherwise ./audit.jsonl next to this file."""
    return Path(os.environ.get("LLM_COST_AUDIT", str(DEFAULT_AUDIT))).expanduser()


def clean_value(value):
    """Make one argument value safe and small enough to log."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -(2**63) <= value < 2**63 else "<int out of range>"
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        text = "".join(ch if ch.isprintable() else "?" for ch in value[:MAX_STR])
        return text + ("..." if len(value) > MAX_STR else "")
    return f"<{type(value).__name__}>"


def count_rows(result):
    """Total length of the top-level lists in a dict result, or None if there are none."""
    if isinstance(result, dict):
        lists = [v for v in result.values() if isinstance(v, list)]
        if lists:
            return sum(len(v) for v in lists)
    return None


def _write_line(entry: dict) -> None:
    line = json.dumps(entry, separators=(",", ":"), allow_nan=False, ensure_ascii=True)
    path = get_audit_path()
    with _lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)  # owner-only file
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _record(tool, args, status, error_type, row_count, started) -> None:
    """Write one entry. Never raises."""
    try:
        _write_line({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "tool": tool,
            "args": args,
            "status": status,
            "error_type": error_type,
            "row_count": row_count,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        })
    except Exception as audit_exc:  # never let logging break a tool call
        print(f"audit log write failed: {type(audit_exc).__name__}", file=sys.stderr)


def audited(func):
    """Decorator: log every call that reaches a tool. Keeps its name, signature and docstring."""
    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        seen = _seen.get()
        if seen is not None:
            seen.append(func.__name__)
        status, error_type, result = "ok", None, None
        try:
            result = func(*args, **kwargs)
            return result
        except BaseException as exc:
            status, error_type = "error", type(exc).__name__
            raise
        finally:
            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                logged_args = {k: clean_value(v) for k, v in bound.arguments.items()}
            except TypeError:
                logged_args = {}
            _record(func.__name__, logged_args, status, error_type,
                    count_rows(result) if status == "ok" else None, started)

    return wrapper


class AuditedFastMCP(FastMCP):
    """FastMCP that also logs calls rejected before they reach a tool."""

    async def call_tool(self, name, arguments):
        started = time.perf_counter()
        seen = []
        token = _seen.set(seen)
        try:
            return await super().call_tool(name, arguments)
        except Exception as exc:
            if not seen:  # never reached a tool, so the wrapper did not log it
                raw = arguments if isinstance(arguments, dict) else {}
                logged_args = {
                    clean_value(str(k)): clean_value(v)
                    for k, v in list(raw.items())[:MAX_REJECTED_ARGS]
                }
                _record(clean_value(str(name)), logged_args, "rejected",
                        type(exc.__cause__ or exc).__name__, None, started)
            raise
        finally:
            _seen.reset(token)
