"""Read-only SQLite helpers for the llm-cost-mcp demo.

Every connection is opened with SQLite's read-only URI mode (mode=ro) and
PRAGMA query_only = ON, so a write fails even if a bug slips a statement in.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MIN_PERIOD_DAYS = 1
MAX_PERIOD_DAYS = 365
MAX_ROWS = 50  # upper bound for tools that return individual rows
MAX_COMPARE_DAYS = 180  # compare/savings look at two back-to-back windows
DEFAULT_DB = Path(__file__).with_name("sample.db")


def get_db_path() -> Path:
    """Database location: $LLM_COST_DB if set, otherwise ./sample.db next to this file."""
    return Path(os.environ.get("LLM_COST_DB", str(DEFAULT_DB))).expanduser().resolve()


@contextmanager
def open_readonly(path=None):
    """Yield a read-only sqlite3 connection (rows behave like dicts)."""
    db_path = Path(path).expanduser().resolve() if path else get_db_path()
    if not db_path.is_file():
        raise FileNotFoundError(
            f"Database not found: {db_path}. Run `python make_sample_data.py` first."
        )
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def validate_int(name: str, value, low: int, high: int) -> int:
    """Return value if it is an int within [low, high], otherwise raise ValueError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__}.")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}.")
    return value


def validate_period_days(value) -> int:
    return validate_int("period_days", value, MIN_PERIOD_DAYS, MAX_PERIOD_DAYS)


def time_window(conn, period_days: int):
    """Return (start, end) ISO strings for the last `period_days` days of the data.

    The window ends at the newest row in the table (not at the wall clock), so
    results are stable and reproducible for a static demo database.
    Rows match when start < created_at <= end.
    """
    row = conn.execute("SELECT MAX(created_at) FROM analyses").fetchone()
    if row[0] is None:
        raise ValueError("The analyses table is empty.")
    end = datetime.strptime(row[0], TS_FORMAT)
    start = end - timedelta(days=period_days)
    return start.strftime(TS_FORMAT), end.strftime(TS_FORMAT)


def validate_compare_days(value) -> int:
    return validate_int("period_days", value, MIN_PERIOD_DAYS, MAX_COMPARE_DAYS)


def period_stats(conn, start: str, end: str) -> dict:
    """Aggregate raw (unrounded) metrics for rows with start < created_at <= end."""
    row = conn.execute(
        """
        SELECT COUNT(*)                          AS calls,
               COALESCE(SUM(input_tokens), 0)    AS input_tokens,
               COALESCE(SUM(output_tokens), 0)   AS output_tokens,
               COALESCE(SUM(cost_usd), 0.0)      AS total_cost_usd,
               AVG(latency_ms)                   AS avg_latency_ms,
               COALESCE(SUM(status = 'failed'), 0)   AS failed_calls,
               COALESCE(SUM(status = 'fallback'), 0) AS fallback_calls
        FROM analyses
        WHERE created_at > ? AND created_at <= ?
        """,
        (start, end),
    ).fetchone()
    calls = row["calls"]
    return {
        "calls": calls,
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "total_cost_usd": row["total_cost_usd"],
        "avg_cost_per_call_usd": row["total_cost_usd"] / calls if calls else None,
        "avg_output_tokens_per_call": row["output_tokens"] / calls if calls else None,
        "avg_latency_ms": row["avg_latency_ms"],
        "failed_calls": row["failed_calls"],
        "fallback_calls": row["fallback_calls"],
        "failure_rate_pct": 100 * row["failed_calls"] / calls if calls else None,
        "fallback_rate_pct": 100 * row["fallback_calls"] / calls if calls else None,
    }


def compare_windows(conn, period_days: int) -> dict:
    """Stats for the latest `period_days` and the equally long period just before it.

    Windows are measured back from the newest row:
      current  = (end - period_days,     end]
      previous = (end - 2*period_days,   end - period_days]
    `previous_coverage_pct` says how much of the previous window the data
    actually spans (100 = fully covered, 0 = no data before the current window).
    """
    first, last = conn.execute("SELECT MIN(created_at), MAX(created_at) FROM analyses").fetchone()
    if last is None:
        raise ValueError("The analyses table is empty.")
    end = datetime.strptime(last, TS_FORMAT)
    mid = end - timedelta(days=period_days)
    start = end - timedelta(days=2 * period_days)
    first_dt = datetime.strptime(first, TS_FORMAT)
    overlap_days = (mid - max(start, first_dt)).total_seconds() / 86400
    coverage = max(0.0, min(100.0, 100 * overlap_days / period_days))
    f = lambda d: d.strftime(TS_FORMAT)
    return {
        "current_window": (f(mid), f(end)),
        "previous_window": (f(start), f(mid)),
        "current": period_stats(conn, f(mid), f(end)),
        "previous": period_stats(conn, f(start), f(mid)),
        "previous_coverage_pct": round(coverage, 1),
    }


def validate_row_count(value) -> int:
    return validate_int("n", value, 1, MAX_ROWS)
