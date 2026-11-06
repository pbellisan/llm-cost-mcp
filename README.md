# llm-cost-mcp

A small, **read-only** [MCP](https://modelcontextprotocol.io) server that reports LLM **cost, latency and failures per model**. It is a portfolio demo built on **synthetic data**, and a pattern you can adapt for any multi-model LLM application.

> **Demo notice.** Every row comes from `make_sample_data.py`. The model names (`alpha-large`, `beta-pro`, ...) are fictional, the prices are illustrative, and nothing comes from a real system. The improvement visible in the second half of the data (fewer calls, shorter outputs, fewer timeouts) is written into the generator on purpose; it is not a finding. This project makes no claims about users, production use or real savings.

## What it does

An AI client connected to this server can ask:

- Where does the money go? (`cost_by_model`)
- How did this period compare with the one before? (`compare_periods`)
- How much of the change came from fewer calls and how much from cheaper calls? (`savings_summary`)
- Which provider fails most often, and how many failures did a fallback rescue? (`failure_rate_by_provider`)
- Which individual calls were slowest? (`slowest_analyses`)

It also offers the table description as a resource (`schema://analyses`) and a ready-made prompt (`monthly_cost_review`) that walks an AI through a monthly review.

## Quick start

Requires Python 3.10+ (tested on Python 3.13 with `mcp` 1.30.0, on Linux).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python make_sample_data.py      # writes sample.db (synthetic)
python -m pytest -q             # run the tests
python server.py                # MCP server over stdio; waits for a client
```

The server is pinned to `mcp<2`: version 2 of the Python SDK renamed `FastMCP`, and this demo uses the 1.x API.

To use it from an MCP client, register the server with absolute paths. A typical client configuration looks like this (not verified here against a specific desktop client; the automated tests use the official Python client over stdio, see `tests/test_stdio_client.py`):

```json
{
  "mcpServers": {
    "llm-cost-demo": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

Environment variables (both optional): `LLM_COST_DB` (database path, default `./sample.db`) and `LLM_COST_AUDIT` (audit log path, default `./audit.jsonl`).

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `cost_by_model` | `period_days` (1-365, default 30) | Per model: calls, tokens, total cost, average cost per call, share of total cost |
| `compare_periods` | `period_days` (1-180, default 30) | Latest period vs the equally long period before it: calls, tokens, cost, latency, failure and fallback rates, plus absolute and percent change and `previous_coverage_pct` |
| `savings_summary` | `period_days` (1-180, default 30) | Change in total cost split into "fewer calls" and "cheaper calls" (can be negative) |
| `failure_rate_by_provider` | `period_days` (1-365, default 30) | Per provider: failed first attempts, how many were rescued by fallback, how many ended as failures, top error types |
| `slowest_analyses` | `n` (1-50, default 10), `period_days` (1-365, default 30) | The slowest calls, with categorical and numeric fields only |

Every docstring is written for an AI reader: what it returns, units and limits. Costs are USD from an illustrative price table.

**Resource** `schema://analyses`: column names, types, units and meanings, row count and date range. It contains no row-level data, and a column without a description is flagged as `undocumented column` rather than hidden.

**Prompt** `monthly_cost_review(period_days=30)`: instructions for an AI to read the schema, call the tools in a sensible order and write a review. It tells the AI to quote only returned numbers, keep observation and interpretation apart, not to forecast, and not to claim real-world savings.

## Why read-only, and how

1. The SQLite connection is opened with `mode=ro` **and** `PRAGMA query_only = ON`. Tests prove that INSERT, UPDATE, DELETE, DROP and CREATE are refused, and refused even when `query_only` is switched off again.
2. No tool accepts SQL. Every query is fixed in the code and parameterised.
3. Inputs are validated with bounds: integers only (booleans and strings are rejected), within the ranges above.
4. There are no write tools.
5. The one file the server writes is the audit log, which is separate from the data.

## Audit log

Every tool call appends one JSON line to `audit.jsonl`:

```json
{"ts":"2026-11-06T13:33:05.147Z","tool":"cost_by_model","args":{"period_days":7},"status":"ok","error_type":null,"row_count":5,"duration_ms":1.91}
```

- `status` is `ok`, `error` (the tool ran and raised) or `rejected` (the call never reached the tool: schema validation failed or the tool name is unknown).
- **Result contents are never logged**, only a row count. Error messages are never logged, only the exception class name.
- Arguments are sanitised: strings are cut to 50 characters with control characters removed, other types are replaced by their type name, and rejected calls keep at most 10 arguments.
- The file is created owner-only (`0600`). If writing fails, the tool still works and a short note goes to stderr.

## Design notes

- **Windows are measured back from the newest row**, not from the clock, so results are reproducible for a static database. A window excludes its start and includes its end.
- **Failures are attributed to the provider tried first.** A `fallback` row is a call whose first attempt failed and was served by another provider; the failure counts against the first provider, and the rescuing provider sees it as `served_as_fallback`.
- **Failed calls cost 0** in this demo (assumed not billed) but still count as calls.
- `compare_periods` and `savings_summary` report how much of the earlier period the data actually covers (`previous_coverage_pct`).

## Limits and caveats

- This is a demo, not a hardened service. It is meant for local stdio use: there is no authentication, rate limiting or multi-user separation.
- The audit log is a plain local file. It is not tamper-evident, and resource reads and prompt requests are not logged (only tool calls and rejected tool calls are).
- The protocol layer (FastMCP) ignores unknown tool arguments and converts numeric strings such as `"7"` to integers before this server's validation runs.
- Tested on one platform and one SDK version (see Quick start). There are no performance claims; the sample has about 2,000 rows.
- All figures in the sample are synthetic.

## Using the pattern with your own application

1. Export your usage into a **copy** (a SQLite file) with the same columns as `schema://analyses`. Do not point this demo at a live production database.
2. Keep the columns categorical or numeric. Do not store prompts, completions, user identifiers or other free text in them.
3. Set `LLM_COST_DB` to your file and read the schema resource to check the columns.
4. Review the privacy and security of your own data before exposing any tool to an AI client; this demo has not been reviewed for that.

## Project layout

```
server.py               tools, resource and prompt
db.py                   read-only connection, input validation, period maths
audit.py                audit log and the protocol-level hook for rejected calls
make_sample_data.py     generates the synthetic sample.db
tests/                  pytest suite, including an end-to-end test with the MCP Python client
AGENTS.md               instructions for an AI agent using the server
```

## Tests

```bash
python -m pytest -q
```

The suite covers the tool maths on a small hand-built database, input validation, the read-only guarantees, the audit log (including that results are never written), and a real client talking to the server over stdio. A test also checks that these docs mention every tool and its arguments.

## License

MIT (see `LICENSE`).
