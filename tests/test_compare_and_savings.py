import pytest

from server import compare_periods, savings_summary

# Fixture facts (see conftest.py), newest row = 2026-03-30T10:00:00Z.
# period_days = 7:
#   current  (03-23 10:00, 03-30 10:00]: 4 rows, cost 3.25, 1 failed
#   previous (03-16 10:00, 03-23 10:00]: 1 row (gamma-flash, exactly at the end), cost 5.00


def test_compare_periods_windows_and_metrics(demo_db):
    r = compare_periods(7)
    assert r["current_window"] == {"start": "2026-03-23T10:00:00Z", "end": "2026-03-30T10:00:00Z"}
    assert r["previous_window"] == {"start": "2026-03-16T10:00:00Z", "end": "2026-03-23T10:00:00Z"}
    assert r["previous_coverage_pct"] == 100.0

    cur, prev = r["current"], r["previous"]
    assert (cur["calls"], prev["calls"]) == (4, 1)
    assert cur["input_tokens"] == 3800 and cur["output_tokens"] == 1400
    assert cur["total_cost_usd"] == 3.25 and prev["total_cost_usd"] == 5.0
    assert cur["avg_cost_per_call_usd"] == 0.8125
    assert cur["avg_output_tokens_per_call"] == 350.0
    assert cur["avg_latency_ms"] == 1000.0
    assert cur["failed_calls"] == 1 and cur["failure_rate_pct"] == 25.0
    assert prev["failed_calls"] == 0 and prev["failure_rate_pct"] == 0.0


def test_compare_periods_changes(demo_db):
    change = compare_periods(7)["change"]
    assert change["calls"] == {"absolute": 3, "percent": 300.0}
    assert change["total_cost_usd"] == {"absolute": -1.75, "percent": -35.0}
    # Previous failure rate is 0, so a relative change is undefined.
    assert change["failure_rate_pct"] == {"absolute": 25.0, "percent": None}


def test_empty_previous_period_gives_nulls_not_errors(demo_db):
    # period 3: current (03-27, 03-30] has rows, previous (03-24, 03-27] is empty.
    r = compare_periods(3)
    assert r["previous"]["calls"] == 0
    assert r["previous"]["avg_cost_per_call_usd"] is None
    assert r["change"]["avg_cost_per_call_usd"] == {"absolute": None, "percent": None}
    assert r["change"]["calls"]["percent"] is None  # previous is 0


def test_coverage_reports_missing_history(demo_db):
    # period 80: the previous window starts long before the first row (2026-01-01),
    # and only the first 8 of its 80 days contain data.
    assert compare_periods(80)["previous_coverage_pct"] == 10.0


def test_savings_split_adds_up_and_can_be_negative(demo_db):
    r = savings_summary(7)
    assert r["total_savings_usd"] == 1.75
    # More calls than before -> "fewer_calls" saving is negative; cheaper calls make up for it.
    assert r["savings_from_fewer_calls_usd"] == -15.0
    assert r["savings_from_cheaper_calls_usd"] == 16.75
    assert r["savings_from_fewer_calls_usd"] + r["savings_from_cheaper_calls_usd"] == pytest.approx(
        r["total_savings_usd"], abs=1e-3
    )
    assert r["avg_output_tokens_change_pct"] == pytest.approx(16.7, abs=0.05)
    assert "not a forecast" in r["note"]


def test_savings_requires_calls_in_both_periods(demo_db):
    with pytest.raises(ValueError, match="at least one call"):
        savings_summary(3)


@pytest.mark.parametrize("tool", [compare_periods, savings_summary])
@pytest.mark.parametrize("bad", [0, -5, 181, 365, "7", None, 2.5, True])
def test_period_bounds_for_two_window_tools(demo_db, tool, bad):
    with pytest.raises(ValueError):
        tool(bad)


def test_upper_bound_180_passes_validation(demo_db):
    # 180 is allowed. compare_periods returns (previous window has little data) ...
    r = compare_periods(180)
    assert r["previous"]["calls"] == 0 and r["previous_coverage_pct"] == 0.0
    # ... and savings_summary fails for lack of data, not because of the range check.
    with pytest.raises(ValueError, match="at least one call"):
        savings_summary(180)
