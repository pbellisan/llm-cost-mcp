import pytest

from server import failure_rate_by_provider, slowest_analyses

# See INCIDENT_ROWS in conftest.py. Within the 30-day window there are 9 calls.


def by_provider(result):
    return {p["provider"]: p for p in result["providers"]}


def test_failure_attribution_to_first_provider(incident_db):
    r = failure_rate_by_provider(30)
    p = by_provider(r)

    # alpha: 2 ok + 1 failed + 2 calls that tried alpha first and were rescued elsewhere
    assert p["alpha"]["primary_attempts"] == 5
    assert p["alpha"]["primary_failures"] == 3
    assert p["alpha"]["failure_rate_pct"] == 60.0
    assert p["alpha"]["unrecovered_failures"] == 1
    assert p["alpha"]["recovered_by_fallback"] == 2
    assert p["alpha"]["served_as_fallback"] == 0
    assert p["alpha"]["top_errors"] == [
        {"error_type": "provider_5xx", "count": 2},
        {"error_type": "rate_limit_exceeded", "count": 1},
    ]

    # beta took over two calls but they are not beta's own attempts
    assert p["beta"]["primary_attempts"] == 2
    assert p["beta"]["failure_rate_pct"] == 0.0
    assert p["beta"]["served_as_fallback"] == 2
    assert p["beta"]["top_errors"] == []

    assert p["gamma"]["failure_rate_pct"] == 50.0
    assert p["gamma"]["top_errors"] == [{"error_type": "timeout", "count": 1}]


def test_failure_totals_and_ordering(incident_db):
    r = failure_rate_by_provider(30)
    assert [p["provider"] for p in r["providers"]] == ["alpha", "gamma", "beta"]
    assert r["totals"] == {
        "primary_attempts": 9,
        "primary_failures": 4,
        "failure_rate_pct": 44.4,
        "unrecovered_failures": 2,
        "recovered_by_fallback": 2,
    }


def test_failure_window_excludes_old_rows_but_long_period_includes_them(incident_db):
    assert by_provider(failure_rate_by_provider(30))["alpha"]["primary_attempts"] == 5
    assert by_provider(failure_rate_by_provider(365))["alpha"]["primary_attempts"] == 6


def test_slowest_order_and_window(incident_db):
    r = slowest_analyses(3, 30)
    assert r["returned"] == 3
    assert [(a["id"], a["latency_ms"]) for a in r["analyses"]] == [(8, 30000), (4, 9000), (5, 8000)]
    # The 99,999 ms row is older than 30 days and must not appear.
    assert all(a["latency_ms"] != 99999 for a in slowest_analyses(50, 30)["analyses"])
    assert slowest_analyses(1, 365)["analyses"][0]["latency_ms"] == 99999


def test_slowest_returns_expected_fields_only(incident_db):
    row = slowest_analyses(1)["analyses"][0]
    assert set(row) == {
        "id", "created_at", "tier", "agent_role", "provider", "model", "status",
        "error_type", "fallback_from", "latency_ms", "input_tokens", "output_tokens", "cost_usd",
    }
    assert row["status"] == "failed" and row["error_type"] == "timeout"


def test_slowest_returns_fewer_rows_when_window_is_small(incident_db):
    # All 9 in-window rows fall inside the last day; asking for 50 returns just those 9.
    assert slowest_analyses(50, 1)["returned"] == 9


@pytest.mark.parametrize("bad", [0, -1, 51, 1000, "5", None, 2.5, True])
def test_slowest_rejects_bad_n(incident_db, bad):
    with pytest.raises(ValueError):
        slowest_analyses(bad)


@pytest.mark.parametrize("bad", [0, 366, "30", None, True])
def test_new_tools_reject_bad_period(incident_db, bad):
    with pytest.raises(ValueError):
        failure_rate_by_provider(bad)
    with pytest.raises(ValueError):
        slowest_analyses(5, bad)


def test_max_n_is_accepted(incident_db):
    assert slowest_analyses(50)["returned"] == 9
