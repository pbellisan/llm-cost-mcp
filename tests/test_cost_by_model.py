import sqlite3

import pytest

import db
from server import cost_by_model


def by_model(result):
    return {m["model"]: m for m in result["models"]}


def test_seven_day_window_totals_and_order(demo_db):
    r = cost_by_model(7)
    # Window is (2026-03-23T10:00:00Z, 2026-03-30T10:00:00Z]: the row exactly
    # at the start is excluded, so gamma-flash must not appear.
    assert r["window_start"] == "2026-03-23T10:00:00Z"
    assert r["window_end"] == "2026-03-30T10:00:00Z"
    assert r["currency"] == "USD"
    assert r["total_cost_usd"] == 3.25
    assert [m["model"] for m in r["models"]] == ["alpha-large", "beta-lite"]

    alpha = by_model(r)["alpha-large"]
    assert alpha["calls"] == 2
    assert alpha["input_tokens"] == 3000
    assert alpha["output_tokens"] == 1300
    assert alpha["total_cost_usd"] == 3.0
    assert alpha["avg_cost_per_call_usd"] == 1.5
    assert alpha["share_of_cost_pct"] == 92.3


def test_failed_call_counts_as_call_but_costs_nothing(demo_db):
    beta = by_model(cost_by_model(7))["beta-lite"]
    assert beta["calls"] == 2  # one ok + one failed
    assert beta["total_cost_usd"] == 0.25
    assert beta["avg_cost_per_call_usd"] == 0.125
    assert beta["share_of_cost_pct"] == 7.7


def test_window_boundary_is_exclusive_at_start(demo_db):
    assert "gamma-flash" not in by_model(cost_by_model(7))
    assert "gamma-flash" in by_model(cost_by_model(8))


def test_default_period_is_30_days(demo_db):
    assert cost_by_model() == cost_by_model(30)
    r = cost_by_model(30)
    assert [m["model"] for m in r["models"]] == ["gamma-flash", "alpha-large", "beta-lite"]
    assert r["total_cost_usd"] == 8.75  # 5.00 + 3.00 + 0.75; the 9.00 row is too old


def test_long_period_includes_old_rows(demo_db):
    r = cost_by_model(365)
    assert by_model(r)["alpha-large"]["total_cost_usd"] == 12.0
    assert r["total_cost_usd"] == 17.75


def test_shares_sum_to_about_100(demo_db):
    r = cost_by_model(365)
    assert sum(m["share_of_cost_pct"] for m in r["models"]) == pytest.approx(100, abs=0.5)


@pytest.mark.parametrize("bad", [0, -1, 366, 10_000, "30", None, 3.5, True])
def test_invalid_period_is_rejected(demo_db, bad):
    with pytest.raises(ValueError):
        cost_by_model(bad)


def test_empty_table_gives_clear_error(empty_db):
    with pytest.raises(ValueError, match="empty"):
        cost_by_model(30)


def test_missing_database_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_COST_DB", str(tmp_path / "nope.db"))
    with pytest.raises(FileNotFoundError, match="make_sample_data"):
        cost_by_model(30)


# --- Read-only guarantees -------------------------------------------------

WRITE_STATEMENTS = [
    "INSERT INTO analyses (id, created_at, tier, agent_role, provider, model, input_tokens, "
    "output_tokens, cost_usd, latency_ms, status) "
    "VALUES (999, '2026-04-01T00:00:00Z', 'free', 'writer', 'alpha', 'alpha-large', 1, 1, 0, 1, 'ok')",
    "UPDATE analyses SET cost_usd = 0",
    "DELETE FROM analyses",
    "DROP TABLE analyses",
    "CREATE TABLE evil (x INTEGER)",
]


def row_count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("statement", WRITE_STATEMENTS)
def test_writes_are_refused(demo_db, statement):
    before = row_count(demo_db)
    with db.open_readonly() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(statement)
    assert row_count(demo_db) == before  # data is untouched


def test_writes_still_refused_if_query_only_is_switched_off(demo_db):
    # Second layer: even without PRAGMA query_only, the connection itself is read-only.
    before = row_count(demo_db)
    with db.open_readonly() as conn:
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM analyses")
    assert row_count(demo_db) == before
