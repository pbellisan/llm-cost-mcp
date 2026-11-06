# AGENTS.md

Instructions for an AI agent that uses the `llm-cost-demo` MCP server.

## What this server is

A **read-only** report on LLM cost, latency and failures per model and provider. The data is **synthetic** and prices are **illustrative**. Treat every number as demo data, never as a real system's figures.

## How to work

1. Read the resource `schema://analyses` first. It explains each column and the date range.
2. For a full review, use the prompt `monthly_cost_review`, or follow its order: `compare_periods`, then `cost_by_model`, `failure_rate_by_provider`, `slowest_analyses`, `savings_summary`.
3. Quote numbers exactly as returned, with units (USD, ms, percent) and the window dates. Do not recompute or round them differently.
4. Keep what the data shows separate from your interpretation, and label interpretation as such.

## Things that are easy to get wrong

- **Windows** end at the newest record in the data, not at today's date. A window excludes its start and includes its end.
- `compare_periods` and `savings_summary` compare the latest period with the equally long one before it. If `previous_coverage_pct` is below 100, say the earlier period is only partly covered.
- **Failed calls** count as calls but cost 0. Costs are USD from an illustrative price table.
- A `fallback` call was served by a different provider after the first attempt failed. `failure_rate_by_provider` charges the failure to the provider tried first.
- Changes in `*_rate_pct` metrics are in percentage points. `percent` is null when the earlier value is 0.
- `latency_ms` is milliseconds. For `fallback` calls it includes the time lost on the failed attempt.
- `savings_summary` can report negative savings (cost grew). That is valid.

## Argument limits

- `cost_by_model`, `failure_rate_by_provider`: `period_days` 1-365 (default 30).
- `compare_periods`, `savings_summary`: `period_days` 1-180 (default 30).
- `slowest_analyses`: `n` 1-50 (default 10), `period_days` 1-365.

An out-of-range or non-integer value returns an error that names the allowed range. Fix the value once; do not retry in a loop.

## Do not

- Do not try to write, change or delete data. No tool takes SQL, and the database is opened read-only.
- Do not forecast future months or claim real-world savings, users or production use.
- Do not use `audit.jsonl` as a data source. It logs calls, never results.
- Do not guess when a tool returns an error or an empty period. Report it.

## If you are editing this repository

- Run `python -m pytest -q` before and after a change.
- Keep every tool read-only. A new tool needs the `@audit.audited` decorator, input validation with bounds, tests, and entries in `README.md` and this file (a test checks that every tool and argument is documented).
- Never add real data, keys, identifiers, emails or logs from any real system. Keep the data synthetic.
