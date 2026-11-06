"""Shared fixtures: a tiny, hand-made SQLite database with known totals.

The tests never touch sample.db, so their expected values are exact and do not
depend on the random generator.
"""
import sqlite3

import pytest

from make_sample_data import SCHEMA

# (created_at, model, provider, input_tokens, output_tokens, cost_usd, status, error_type)
FIXTURE_ROWS = [
    ("2026-03-30T10:00:00Z", "alpha-large", "alpha", 1000, 500, 1.00, "ok", None),      # newest row = window end
    ("2026-03-29T10:00:00Z", "alpha-large", "alpha", 2000, 800, 2.00, "ok", None),
    ("2026-03-28T10:00:00Z", "beta-lite", "beta", 500, 100, 0.25, "ok", None),
    ("2026-03-28T11:00:00Z", "beta-lite", "beta", 300, 0, 0.00, "failed", "timeout"),   # failed: costs 0
    ("2026-03-23T10:00:00Z", "gamma-flash", "gamma", 700, 300, 5.00, "ok", None),       # exactly 7 days before end
    ("2026-03-01T10:00:00Z", "beta-lite", "beta", 400, 200, 0.50, "ok", None),          # inside 30 days only
    ("2026-01-01T10:00:00Z", "alpha-large", "alpha", 100, 50, 9.00, "ok", None),        # outside 30 days
]


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for i, r in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO analyses (id, created_at, tier, agent_role, provider, model, "
            "input_tokens, output_tokens, cost_usd, latency_ms, status, error_type, fallback_from) "
            "VALUES (?, ?, 'free', 'writer', ?, ?, ?, ?, ?, 1000, ?, ?, NULL)",
            (i, r[0], r[2], r[1], r[3], r[4], r[5], r[6], r[7]),
        )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    """Keep tests from writing to the real ./audit.jsonl; tests may override this path."""
    monkeypatch.setenv("LLM_COST_AUDIT", str(tmp_path / "audit.jsonl"))


@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    path = tmp_path / "demo.db"
    _make_db(path, FIXTURE_ROWS)
    monkeypatch.setenv("LLM_COST_DB", str(path))
    return path


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    _make_db(path, [])
    monkeypatch.setenv("LLM_COST_DB", str(path))
    return path


# Second fixture for failure/latency tests. End of data = 2026-04-10T12:00:00Z.
# (created_at, model, provider, status, error_type, fallback_from, latency_ms)
INCIDENT_ROWS = [
    ("2026-04-10T09:00:00Z", "alpha-large", "alpha", "ok", None, None, 2000),
    ("2026-04-10T09:05:00Z", "alpha-large", "alpha", "ok", None, None, 3000),
    ("2026-04-10T09:10:00Z", "alpha-large", "alpha", "failed", "provider_5xx", None, 500),
    ("2026-04-10T09:15:00Z", "beta-pro", "beta", "fallback", "provider_5xx", "alpha-large", 9000),
    ("2026-04-10T09:20:00Z", "beta-pro", "beta", "fallback", "rate_limit_exceeded", "alpha-large", 8000),
    ("2026-04-10T09:25:00Z", "beta-pro", "beta", "ok", None, None, 4000),
    ("2026-04-10T09:30:00Z", "gamma-flash", "gamma", "ok", None, None, 1000),
    ("2026-04-10T09:35:00Z", "gamma-flash", "gamma", "failed", "timeout", None, 30000),
    ("2026-04-10T12:00:00Z", "beta-pro", "beta", "ok", None, None, 5000),               # newest = window end
    ("2026-01-01T09:00:00Z", "alpha-large", "alpha", "failed", "provider_5xx", None, 99999),  # outside 30 days
]


@pytest.fixture
def incident_db(tmp_path, monkeypatch):
    path = tmp_path / "incident.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for i, (ts, model, provider, status, err, fb, lat) in enumerate(INCIDENT_ROWS, start=1):
        conn.execute(
            "INSERT INTO analyses (id, created_at, tier, agent_role, provider, model, input_tokens, "
            "output_tokens, cost_usd, latency_ms, status, error_type, fallback_from) "
            "VALUES (?, ?, 'paid', 'writer', ?, ?, 100, 50, 0.01, ?, ?, ?, ?)",
            (i, ts, provider, model, lat, status, err, fb),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("LLM_COST_DB", str(path))
    return path
