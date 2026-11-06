"""llm-cost-mcp: a read-only MCP server reporting LLM cost, latency and failures.

Demo project running on SYNTHETIC data only (see make_sample_data.py).
"""
import json
from collections import Counter, defaultdict

import audit
import db

mcp = audit.AuditedFastMCP("llm-cost-demo")  # FastMCP plus protocol-level audit of rejected calls


@mcp.tool()
@audit.audited
def cost_by_model(period_days: int = 30) -> dict:
    """Report LLM cost per model over the most recent period of the data.

    Args:
        period_days: Window length in days, integer from 1 to 365 (default 30).
            The window ends at the newest record in the database, not at the
            current clock time.

    Returns a dict with:
        window_start, window_end: ISO 8601 UTC timestamps (start is exclusive).
        currency: always "USD". Costs come from an illustrative price table.
        total_cost_usd: sum over all models in the window.
        models: list sorted by total_cost_usd, highest first. Each item has
            model, provider, calls, input_tokens, output_tokens,
            total_cost_usd, avg_cost_per_call_usd, share_of_cost_pct.

    Notes: failed calls count toward `calls` but cost 0 (assumed not billed).
    A call served by a fallback model is attributed to the model that served it.
    Read-only; never modifies data.
    """
    period_days = db.validate_period_days(period_days)
    with db.open_readonly() as conn:
        start, end = db.time_window(conn, period_days)
        rows = conn.execute(
            """
            SELECT model, provider,
                   COUNT(*)           AS calls,
                   SUM(input_tokens)  AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_usd)      AS total_cost_usd
            FROM analyses
            WHERE created_at > ? AND created_at <= ?
            GROUP BY model, provider
            ORDER BY total_cost_usd DESC, model ASC
            """,
            (start, end),
        ).fetchall()

    total = sum(r["total_cost_usd"] for r in rows)
    models = [
        {
            "model": r["model"],
            "provider": r["provider"],
            "calls": r["calls"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "total_cost_usd": round(r["total_cost_usd"], 4),
            "avg_cost_per_call_usd": round(r["total_cost_usd"] / r["calls"], 6),
            "share_of_cost_pct": round(100 * r["total_cost_usd"] / total, 1) if total else 0.0,
        }
        for r in rows
    ]
    return {
        "window_start": start,
        "window_end": end,
        "currency": "USD",
        "total_cost_usd": round(total, 4),
        "models": models,
    }


def _r(value, digits):
    return None if value is None else round(value, digits)


def _present(stats: dict) -> dict:
    return {
        "calls": stats["calls"],
        "input_tokens": stats["input_tokens"],
        "output_tokens": stats["output_tokens"],
        "total_cost_usd": _r(stats["total_cost_usd"], 4),
        "avg_cost_per_call_usd": _r(stats["avg_cost_per_call_usd"], 6),
        "avg_output_tokens_per_call": _r(stats["avg_output_tokens_per_call"], 1),
        "avg_latency_ms": _r(stats["avg_latency_ms"], 1),
        "failed_calls": stats["failed_calls"],
        "fallback_calls": stats["fallback_calls"],
        "failure_rate_pct": _r(stats["failure_rate_pct"], 1),
        "fallback_rate_pct": _r(stats["fallback_rate_pct"], 1),
    }


def _change(previous, current, digits):
    """Absolute and relative change; both None if either side is missing, percent None if previous is 0."""
    if previous is None or current is None:
        return {"absolute": None, "percent": None}
    percent = None if previous == 0 else round(100 * (current - previous) / previous, 1)
    return {"absolute": round(current - previous, digits), "percent": percent}


CHANGE_METRICS = {
    "calls": 0,
    "total_cost_usd": 4,
    "avg_cost_per_call_usd": 6,
    "avg_output_tokens_per_call": 1,
    "avg_latency_ms": 1,
    "failure_rate_pct": 1,
    "fallback_rate_pct": 1,
}


@mcp.tool()
@audit.audited
def compare_periods(period_days: int = 30) -> dict:
    """Compare the latest period with the equally long period right before it.

    Args:
        period_days: Length of each period in days, integer from 1 to 180
            (default 30). The latest period ends at the newest record in the
            database; the previous period ends where the latest one starts.

    Returns a dict with:
        current_window, previous_window: {start, end} ISO 8601 UTC (start exclusive).
        previous_coverage_pct: how much of the previous window the data spans
            (below 100 means the comparison is partly based on missing history).
        current, previous: calls, input_tokens, output_tokens, total_cost_usd,
            avg_cost_per_call_usd, avg_output_tokens_per_call, avg_latency_ms,
            failed_calls, fallback_calls, failure_rate_pct, fallback_rate_pct
            (rates are percent of calls). Averages are null when a period has no calls.
        change: per metric {absolute, percent} = current minus previous.
            For the two *_rate_pct metrics, `absolute` is in percentage points.
            `percent` is null when the previous value is 0 or missing.

    Notes: costs are USD from an illustrative price table; failed calls count as
    calls but cost 0. Read-only; never modifies data.
    """
    period_days = db.validate_compare_days(period_days)
    with db.open_readonly() as conn:
        w = db.compare_windows(conn, period_days)
    cur, prev = _present(w["current"]), _present(w["previous"])
    return {
        "current_window": {"start": w["current_window"][0], "end": w["current_window"][1]},
        "previous_window": {"start": w["previous_window"][0], "end": w["previous_window"][1]},
        "previous_coverage_pct": w["previous_coverage_pct"],
        "current": cur,
        "previous": prev,
        "change": {
            name: _change(prev[name], cur[name], digits) for name, digits in CHANGE_METRICS.items()
        },
    }


def _savings_view(stats: dict) -> dict:
    return {
        "calls": stats["calls"],
        "total_cost_usd": _r(stats["total_cost_usd"], 4),
        "avg_cost_per_call_usd": _r(stats["avg_cost_per_call_usd"], 6),
        "avg_output_tokens_per_call": _r(stats["avg_output_tokens_per_call"], 1),
    }


@mcp.tool()
@audit.audited
def savings_summary(period_days: int = 30) -> dict:
    """Explain how total cost changed between the previous and the latest period.

    The cost difference is split into two parts that add up to the total:
      * fewer_calls: (previous_calls - current_calls) x previous avg cost per call
      * cheaper_calls: (previous avg cost per call - current avg cost per call) x current_calls
    A positive number means money saved; a negative number means cost grew.

    Args:
        period_days: Length of each period in days, integer from 1 to 180
            (default 30). Same windows as compare_periods.

    Returns a dict with:
        current_window, previous_window, previous_coverage_pct: as in compare_periods.
        previous, current: calls, total_cost_usd, avg_cost_per_call_usd,
            avg_output_tokens_per_call.
        total_savings_usd: previous total cost minus current total cost.
        savings_from_fewer_calls_usd, savings_from_cheaper_calls_usd: the split above.
        avg_output_tokens_change_pct: relative change in output tokens per call.
        note: reminder that this is a descriptive comparison on synthetic data.

    Raises an error if either period has no calls. This is a backward-looking
    comparison, not a forecast; costs use an illustrative price table.
    Read-only; never modifies data.
    """
    period_days = db.validate_compare_days(period_days)
    with db.open_readonly() as conn:
        w = db.compare_windows(conn, period_days)
    cur, prev = w["current"], w["previous"]
    if cur["calls"] == 0 or prev["calls"] == 0:
        raise ValueError(
            "Both periods need at least one call to compute savings "
            f"(previous: {prev['calls']}, current: {cur['calls']}). Try a shorter period_days."
        )
    fewer = (prev["calls"] - cur["calls"]) * prev["avg_cost_per_call_usd"]
    cheaper = (prev["avg_cost_per_call_usd"] - cur["avg_cost_per_call_usd"]) * cur["calls"]
    out_prev, out_cur = prev["avg_output_tokens_per_call"], cur["avg_output_tokens_per_call"]
    return {
        "current_window": {"start": w["current_window"][0], "end": w["current_window"][1]},
        "previous_window": {"start": w["previous_window"][0], "end": w["previous_window"][1]},
        "previous_coverage_pct": w["previous_coverage_pct"],
        "previous": _savings_view(prev),
        "current": _savings_view(cur),
        "total_savings_usd": round(prev["total_cost_usd"] - cur["total_cost_usd"], 4),
        "savings_from_fewer_calls_usd": round(fewer, 4),
        "savings_from_cheaper_calls_usd": round(cheaper, 4),
        "avg_output_tokens_change_pct": round(100 * (out_cur - out_prev) / out_prev, 1) if out_prev else None,
        "note": "Descriptive comparison on synthetic data with illustrative prices; not a forecast.",
    }


@mcp.tool()
@audit.audited
def failure_rate_by_provider(period_days: int = 30) -> dict:
    """Report how often the FIRST attempt to each provider failed, and what happened next.

    Every row in the data is one call. A call whose first attempt failed either
    ended as `failed` (no result) or was retried on another provider and ended
    as `fallback`. This tool attributes the failed first attempt to the
    provider that was tried first.

    Args:
        period_days: Window length in days, integer from 1 to 365 (default 30).
            The window ends at the newest record in the database.

    Returns a dict with:
        window_start, window_end: ISO 8601 UTC (start exclusive).
        totals: primary_attempts, primary_failures, failure_rate_pct,
            unrecovered_failures, recovered_by_fallback across all providers.
        providers: list sorted by failure_rate_pct, highest first. Each item has
            provider, primary_attempts (calls that tried this provider first),
            primary_failures, failure_rate_pct, unrecovered_failures (ended as
            failed), recovered_by_fallback (rescued by another provider),
            served_as_fallback (calls this provider took over from others; not
            counted in primary_attempts), top_errors (up to 5 of
            {error_type, count}, covering this provider's failed first attempts).

    Notes: rates are percent. Read-only; never modifies data.
    """
    period_days = db.validate_period_days(period_days)
    with db.open_readonly() as conn:
        start, end = db.time_window(conn, period_days)
        provider_of = {
            r["model"]: r["provider"]
            for r in conn.execute("SELECT DISTINCT model, provider FROM analyses")
        }
        groups = conn.execute(
            """
            SELECT provider, fallback_from, status, error_type, COUNT(*) AS n
            FROM analyses
            WHERE created_at > ? AND created_at <= ?
            GROUP BY provider, fallback_from, status, error_type
            """,
            (start, end),
        ).fetchall()

    stats = defaultdict(lambda: {
        "primary_attempts": 0, "primary_failures": 0, "unrecovered_failures": 0,
        "recovered_by_fallback": 0, "served_as_fallback": 0, "errors": Counter(),
    })
    for g in groups:
        n = g["n"]
        if g["status"] == "fallback":
            first = stats[provider_of.get(g["fallback_from"], "unknown")]
            first["primary_attempts"] += n
            first["primary_failures"] += n
            first["recovered_by_fallback"] += n
            first["errors"][g["error_type"] or "unknown"] += n
            stats[g["provider"]]["served_as_fallback"] += n
        else:
            row = stats[g["provider"]]
            row["primary_attempts"] += n
            if g["status"] == "failed":
                row["primary_failures"] += n
                row["unrecovered_failures"] += n
                row["errors"][g["error_type"] or "unknown"] += n

    def rate(failures, attempts):
        return round(100 * failures / attempts, 1) if attempts else None

    providers = [
        {
            "provider": name,
            "primary_attempts": s["primary_attempts"],
            "primary_failures": s["primary_failures"],
            "failure_rate_pct": rate(s["primary_failures"], s["primary_attempts"]),
            "unrecovered_failures": s["unrecovered_failures"],
            "recovered_by_fallback": s["recovered_by_fallback"],
            "served_as_fallback": s["served_as_fallback"],
            "top_errors": [
                {"error_type": e, "count": c}
                for e, c in sorted(s["errors"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            ],
        }
        for name, s in stats.items()
    ]
    providers.sort(key=lambda p: (-(p["failure_rate_pct"] or 0), p["provider"]))
    attempts = sum(p["primary_attempts"] for p in providers)
    failures = sum(p["primary_failures"] for p in providers)
    return {
        "window_start": start,
        "window_end": end,
        "totals": {
            "primary_attempts": attempts,
            "primary_failures": failures,
            "failure_rate_pct": rate(failures, attempts),
            "unrecovered_failures": sum(p["unrecovered_failures"] for p in providers),
            "recovered_by_fallback": sum(p["recovered_by_fallback"] for p in providers),
        },
        "providers": providers,
    }


@mcp.tool()
@audit.audited
def slowest_analyses(n: int = 10, period_days: int = 30) -> dict:
    """List the slowest individual calls in the period, slowest first.

    Args:
        n: How many rows to return, integer from 1 to 50 (default 10).
        period_days: Window length in days, integer from 1 to 365 (default 30).
            The window ends at the newest record in the database.

    Returns a dict with:
        window_start, window_end: ISO 8601 UTC (start exclusive).
        returned: number of rows returned (may be below n if the window is small).
        analyses: list ordered by latency_ms descending (ties: lower id first).
            Each item has id, created_at, tier, agent_role, provider, model,
            status, error_type, fallback_from, latency_ms (milliseconds),
            input_tokens, output_tokens, cost_usd.

    Notes: a `fallback` call's latency includes the time lost on the failed first
    attempt. A `failed` timeout shows the time spent waiting. Rows contain only
    categorical and numeric fields, no free text. Read-only; never modifies data.
    """
    n = db.validate_row_count(n)
    period_days = db.validate_period_days(period_days)
    with db.open_readonly() as conn:
        start, end = db.time_window(conn, period_days)
        rows = conn.execute(
            """
            SELECT id, created_at, tier, agent_role, provider, model, status,
                   error_type, fallback_from, latency_ms, input_tokens,
                   output_tokens, cost_usd
            FROM analyses
            WHERE created_at > ? AND created_at <= ?
            ORDER BY latency_ms DESC, id ASC
            LIMIT ?
            """,
            (start, end, n),
        ).fetchall()
    analyses = [dict(r) for r in rows]
    return {"window_start": start, "window_end": end, "returned": len(analyses), "analyses": analyses}


# --- Resource: table schema -------------------------------------------------

COLUMN_DOCS = {
    "id": "Row identifier (integer), unique per call.",
    "created_at": "When the call happened. ISO 8601 string in UTC, for example 2026-03-30T10:00:00Z.",
    "tier": "Product tier of the request: 'free' or 'paid'.",
    "agent_role": "Label of the agent role that made the call (for example planner, researcher, writer).",
    "provider": "Provider that SERVED the call. For a 'fallback' row this is the fallback provider.",
    "model": "Model that served the call.",
    "input_tokens": "Prompt tokens sent (integer).",
    "output_tokens": "Completion tokens returned (integer). 0 for failed calls.",
    "cost_usd": "Cost in US dollars from an ILLUSTRATIVE price table, not real prices. 0 for failed calls (assumed not billed).",
    "latency_ms": "Wall-clock time in milliseconds. For 'fallback' rows it includes the time lost on the failed first attempt.",
    "status": "'ok' = served by the first-choice model; 'failed' = no result; 'fallback' = the first attempt failed and another model served the call.",
    "error_type": "Why the first attempt failed (for example timeout, rate_limit_exceeded, provider_5xx, context_length_exceeded). Null when status is 'ok'.",
    "fallback_from": "First-choice model when status is 'fallback'. Null otherwise.",
}


@mcp.resource(
    "schema://analyses",
    name="analyses_schema",
    description="Column names, types, units and meanings of the `analyses` table, plus the row count and date range.",
    mime_type="application/json",
)
def analyses_schema() -> str:
    """Describe the `analyses` table so an AI client can read the data correctly.

    Columns come from the live database (PRAGMA table_info); descriptions are
    maintained in this file. A column without a description is flagged as
    undocumented rather than hidden. Contains no row-level data.
    """
    with db.open_readonly() as conn:
        columns = conn.execute("PRAGMA table_info(analyses)").fetchall()
        count, first, last = conn.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM analyses"
        ).fetchone()
    document = {
        "table": "analyses",
        "row_meaning": "One row is one LLM call made by an agent role. Calls whose first attempt failed are "
                       "either 'failed' (no result) or 'fallback' (served by another model).",
        "time_windows": "Tools measure windows back from the newest row (last_created_at), not from the clock. "
                        "A window excludes its start and includes its end.",
        "data_notice": "Synthetic demo data with illustrative prices. Nothing here comes from a real system.",
        "row_count": count,
        "data_range": {"first_created_at": first, "last_created_at": last},
        "columns": [
            {
                "name": c["name"],
                "type": c["type"],
                "nullable": not c["notnull"] and not c["pk"],
                "primary_key": bool(c["pk"]),
                "description": COLUMN_DOCS.get(c["name"], "undocumented column"),
            }
            for c in columns
        ],
    }
    return json.dumps(document, indent=2)


# --- Prompt: monthly cost review ---------------------------------------------

@mcp.prompt(
    name="monthly_cost_review",
    description="Guide an AI through a monthly LLM cost, latency and reliability review using this server's tools.",
)
def monthly_cost_review(period_days: int = 30) -> str:
    """Return instructions for writing a cost review for the latest `period_days` days (1 to 180)."""
    period_days = db.validate_compare_days(period_days)
    return f"""You are preparing a cost, latency and reliability review of an LLM-backed application \
for the latest {period_days} days, compared with the {period_days} days before. Use only this \
server's read-only tools. The data is synthetic and prices are illustrative.

Work in this order:
1. Read the resource schema://analyses to learn what each column means and what date range the data covers.
2. Call compare_periods with period_days={period_days}. Check previous_coverage_pct; if it is below 100, \
say the comparison rests on incomplete history.
3. Call cost_by_model with period_days={period_days} to see where the money goes.
4. Call failure_rate_by_provider with period_days={period_days} to see reliability per provider, \
including how many failures were rescued by fallback.
5. Call slowest_analyses with n=10 and period_days={period_days} to look at latency outliers.
6. Call savings_summary with period_days={period_days} to explain the change in total cost.

Write the review with these sections: Summary (three sentences at most), Cost, Latency, Reliability, \
Savings, Open questions.

Rules:
- Quote only numbers returned by the tools, with units (USD, ms, percent) and the window dates.
- Keep what the data shows separate from your interpretation, and label interpretation as such.
- Do not forecast or extrapolate to future months; this is a backward-looking comparison.
- Do not claim real-world savings or production use; say the figures are synthetic.
- If a tool returns an error or an empty period, report that instead of guessing.
- Suggest at most three follow-up checks, each tied to a specific number you cited."""


if __name__ == "__main__":
    mcp.run()  # stdio transport
