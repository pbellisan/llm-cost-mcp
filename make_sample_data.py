#!/usr/bin/env python3
"""Generate a SYNTHETIC SQLite database for the llm-cost-mcp demo.

Everything in the output is invented: model names, prices, token counts,
latencies and failures. Nothing comes from a real system.

The data tells a small story over 60 days:
  * days 0-29  ("before"): more calls per day, longer outputs, more timeouts
  * days 30-59 ("after"):  fewer calls, ~28% fewer output tokens, fewer timeouts
  * day 12-13: a rate-limit burst on one provider (traffic falls back to another)
  * day 41:    a provider outage in the working day (fallbacks to a different provider)
  * a steady trickle of context-length errors on the smallest model

Usage:
    python make_sample_data.py                # writes ./sample.db
    python make_sample_data.py --out x.db --seed 7 --end-date 2026-06-30
"""
import argparse
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAYS = 60
SPLIT = 30  # days 0..29 = "before", days 30..59 = "after"

# ILLUSTRATIVE prices in USD per 1M tokens (input, output).
# These are made-up numbers for a demo. They are NOT real vendor prices.
PRICES = {
    "alpha-large": ("alpha", 3.00, 15.00),
    "alpha-small": ("alpha", 0.50, 2.50),
    "beta-pro": ("beta", 2.00, 8.00),
    "beta-lite": ("beta", 0.25, 1.00),
    "gamma-flash": ("gamma", 0.10, 0.40),
}

# (base latency ms, extra ms per output token) - also invented.
LATENCY = {
    "alpha-large": (700, 14),
    "alpha-small": (250, 6),
    "beta-pro": (600, 12),
    "beta-lite": (200, 5),
    "gamma-flash": (150, 3.5),
}

TIER_WEIGHTS = [("free", 0.6), ("paid", 0.4)]
TIER_MODELS = {
    "free": [("alpha-small", 0.40), ("beta-lite", 0.35), ("gamma-flash", 0.25)],
    "paid": [("alpha-large", 0.40), ("beta-pro", 0.40), ("gamma-flash", 0.10), ("alpha-small", 0.10)],
}
FALLBACK_PROB = {"free": 0.4, "paid": 0.8}
FALLBACK_MODEL = {
    "alpha-large": "beta-pro",
    "beta-pro": "alpha-large",
    "alpha-small": "beta-lite",
    "beta-lite": "alpha-small",
    "gamma-flash": "beta-lite",
}

# role -> (weight, typical input tokens, typical output tokens)
ROLES = {
    "planner": (0.15, 1200, 400),
    "researcher": (0.30, 3500, 900),
    "summarizer": (0.20, 4000, 600),
    "critic": (0.15, 2500, 500),
    "writer": (0.20, 2000, 1500),
}

HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 3, 5, 8, 10, 10, 9, 8, 9, 10, 10, 9, 8, 6, 5, 4, 3, 2, 1]


def pick(rng, pairs):
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def lognormal_int(rng, typical, sigma, floor=20):
    return max(floor, int(rng.lognormvariate(math.log(typical), sigma)))


def cost_usd(model, tokens_in, tokens_out):
    _, p_in, p_out = PRICES[model]
    return round(tokens_in / 1e6 * p_in + tokens_out / 1e6 * p_out, 6)


def latency_ms(rng, model, tokens_out):
    base, per_tok = LATENCY[model]
    value = base + per_tok * tokens_out * rng.uniform(0.7, 1.4) + rng.gauss(0, 60)
    if rng.random() < 0.02:  # occasional slow tail
        value *= 3
    return max(80, int(value))


def failure_for(rng, model, role, day, hour, tokens_in):
    """Return an error_type string if this attempt fails, else None."""
    # Provider outage on one provider (day 41, 08:00-14:59 UTC).
    if day == 41 and 8 <= hour <= 14 and PRICES[model][0] == "alpha":
        if rng.random() < 0.95:
            return "provider_5xx"
    # Rate-limit burst on one provider (days 12-13).
    if day in (12, 13) and PRICES[model][0] == "beta" and rng.random() < 0.6:
        return "rate_limit_exceeded"
    # Occasional context-length errors on the smallest model for long inputs.
    if model == "alpha-small" and role in ("researcher", "summarizer") and rng.random() < 0.03:
        return "context_length_exceeded"
    # One model that times out more often, improving in the second half.
    if model == "gamma-flash":
        if rng.random() < (0.05 if day < SPLIT else 0.02):
            return "timeout"
    # Small background error rate.
    if rng.random() < 0.008:
        return "provider_5xx"
    return None


def failed_latency(rng, error_type):
    if error_type == "timeout":
        return 30000 + rng.randint(-300, 300)
    return rng.randint(150, 1500)


def build_rows(seed, end_date):
    rng = random.Random(seed)
    start = end_date - timedelta(days=DAYS - 1)
    rows = []
    for day in range(DAYS):
        mean_calls = 37 if day < SPLIT else 30  # fewer calls in the second half
        n_calls = max(5, int(rng.gauss(mean_calls, 5)))
        date = start + timedelta(days=day)
        for _ in range(n_calls):
            hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
            ts = date.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59))
            tier = pick(rng, TIER_WEIGHTS)
            model = pick(rng, TIER_MODELS[tier])
            role = pick(rng, [(r, v[0]) for r, v in ROLES.items()])
            _, typ_in, typ_out = ROLES[role]
            tokens_in = lognormal_int(rng, typ_in, 0.35)
            tokens_out = lognormal_int(rng, typ_out, 0.40)
            if day >= SPLIT:
                tokens_out = max(20, int(tokens_out * 0.72))  # shorter outputs

            error = failure_for(rng, model, role, day, hour, tokens_in)
            provider = PRICES[model][0]
            status, err_col, fb_from = "ok", None, None

            if error is None:
                lat = latency_ms(rng, model, tokens_out)
                cost = cost_usd(model, tokens_in, tokens_out)
                out_col = tokens_out
            elif rng.random() < FALLBACK_PROB[tier]:
                # Primary failed; the call was served by a fallback model.
                fb_from, err_col, status = model, error, "fallback"
                lost = failed_latency(rng, error)
                model = FALLBACK_MODEL[model]
                provider = PRICES[model][0]
                lat = lost + latency_ms(rng, model, tokens_out)
                cost = cost_usd(model, tokens_in, tokens_out)
                out_col = tokens_out
            else:
                # Failed with no result: assumed not billed in this demo.
                status, err_col = "failed", error
                lat = failed_latency(rng, error)
                cost, out_col = 0.0, 0

            rows.append((
                ts.strftime("%Y-%m-%dT%H:%M:%SZ"), tier, role, provider, model,
                tokens_in, out_col, cost, lat, status, err_col, fb_from,
            ))
    rows.sort(key=lambda r: r[0])
    return [(i + 1, *r) for i, r in enumerate(rows)]


SCHEMA = """
CREATE TABLE analyses (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT    NOT NULL,   -- ISO 8601, UTC
    tier          TEXT    NOT NULL CHECK (tier IN ('free', 'paid')),
    agent_role    TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL    NOT NULL,   -- computed from an ILLUSTRATIVE price table
    latency_ms    INTEGER NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('ok', 'failed', 'fallback')),
    error_type    TEXT,               -- why the primary attempt failed, if it did
    fallback_from TEXT                -- primary model when status = 'fallback'
);
CREATE INDEX idx_analyses_created_at ON analyses (created_at);
CREATE INDEX idx_analyses_model ON analyses (model);
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(Path(__file__).with_name("sample.db")))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD (UTC); default: today")
    args = parser.parse_args()

    if args.end_date:
        end = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    out = Path(args.out)
    if out.exists():
        out.unlink()  # regenerate from scratch; this file is always generated

    rows = build_rows(args.seed, end)
    with sqlite3.connect(out) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO analyses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    with sqlite3.connect(out) as conn:
        total, first, last = conn.execute(
            "SELECT count(*), min(created_at), max(created_at) FROM analyses").fetchone()
        print(f"Wrote {total} synthetic rows to {out}")
        print(f"Range: {first} .. {last}")
        cut = (end - timedelta(days=DAYS - SPLIT - 1)).strftime("%Y-%m-%dT00:00:00Z")
        for label, cond in (("first 30 days", "created_at < ?"), ("last 30 days ", "created_at >= ?")):
            n, avg_out, cost = conn.execute(
                f"SELECT count(*), avg(output_tokens), sum(cost_usd) FROM analyses WHERE {cond}", (cut,)).fetchone()
            print(f"{label}: rows={n}  avg_output_tokens={avg_out:.0f}  total_cost_usd={cost:.2f}")
        print("Status counts:", dict(conn.execute("SELECT status, count(*) FROM analyses GROUP BY status").fetchall()))
        print("Top errors:", conn.execute(
            "SELECT error_type, count(*) FROM analyses WHERE error_type IS NOT NULL "
            "GROUP BY 1 ORDER BY 2 DESC").fetchall())


if __name__ == "__main__":
    main()
