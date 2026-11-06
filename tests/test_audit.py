import asyncio
import inspect
import json
import math

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import server
from server import cost_by_model, slowest_analyses


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("LLM_COST_AUDIT", str(path))
    return path


def read_entries(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


ENTRY_KEYS = {"ts", "tool", "args", "status", "error_type", "row_count", "duration_ms"}


def test_successful_call_is_logged(demo_db, audit_file):
    cost_by_model(7)
    (entry,) = read_entries(audit_file)
    assert set(entry) == ENTRY_KEYS
    assert entry["tool"] == "cost_by_model"
    assert entry["args"] == {"period_days": 7}
    assert entry["status"] == "ok" and entry["error_type"] is None
    assert entry["row_count"] == 2  # alpha-large and beta-lite in the 7-day window
    assert entry["duration_ms"] >= 0
    assert entry["ts"].endswith("Z") and "T" in entry["ts"]


def test_default_arguments_are_recorded(demo_db, audit_file):
    cost_by_model()
    assert read_entries(audit_file)[0]["args"] == {"period_days": 30}


def test_tools_without_row_lists_log_null_row_count(demo_db, audit_file):
    server.compare_periods(7)
    assert read_entries(audit_file)[0]["row_count"] is None


def test_failed_call_logs_error_type_but_not_message(demo_db, audit_file):
    with pytest.raises(ValueError):
        cost_by_model(0)
    (entry,) = read_entries(audit_file)
    assert entry["status"] == "error"
    assert entry["error_type"] == "ValueError"
    assert entry["row_count"] is None
    assert entry["args"] == {"period_days": 0}
    assert "between" not in json.dumps(entry)  # the message text is not logged


def test_result_contents_are_never_logged(incident_db, audit_file):
    result = slowest_analyses(5)
    assert result["analyses"]  # there was real content to leak
    text = audit_file.read_text(encoding="utf-8")
    for leaked in ("alpha-large", "beta-pro", "gamma-flash", "timeout", "provider_5xx", "0.01"):
        assert leaked not in text


def test_arguments_are_sanitised_and_lines_stay_valid_json(demo_db, audit_file):
    hostile = ["x" * 5000, "line1\nline2\x00", float("nan"), float("inf"), 10**400, object(), [1, 2], {"a": 1}]
    for value in hostile:
        with pytest.raises(ValueError):
            cost_by_model(value)
    entries = read_entries(audit_file)  # json.loads on every line: all valid
    assert len(entries) == len(hostile)
    logged = [e["args"]["period_days"] for e in entries]
    assert logged[0] == "x" * 50 + "..."
    assert logged[1] == "line1?line2?"
    assert logged[2] == "nan" and logged[3] == "inf"
    assert logged[4] == "<int out of range>"
    assert logged[5] == "<object>" and logged[6] == "<list>" and logged[7] == "<dict>"
    assert max(len(line) for line in audit_file.read_text().splitlines()) < 300


def test_calls_append_in_order(demo_db, audit_file):
    cost_by_model(7)
    server.compare_periods(7)
    slowest_analyses(2, 7)
    assert [e["tool"] for e in read_entries(audit_file)] == ["cost_by_model", "compare_periods", "slowest_analyses"]


def test_unwritable_audit_path_does_not_break_the_tool(demo_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_COST_AUDIT", str(tmp_path / "no_such_dir" / "audit.jsonl"))
    result = cost_by_model(7)
    assert result["total_cost_usd"] == 3.25
    err = capsys.readouterr().err
    assert "audit log write failed" in err
    assert "no_such_dir" not in err  # no paths or contents in the note


def test_audit_file_is_owner_only(demo_db, audit_file):
    cost_by_model(7)
    assert audit_file.stat().st_mode & 0o777 == 0o600


def test_decorator_keeps_signature_docstring_and_schema():
    assert list(inspect.signature(cost_by_model).parameters) == ["period_days"]
    assert cost_by_model.__name__ == "cost_by_model"
    assert "Report LLM cost per model" in cost_by_model.__doc__
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {
        "cost_by_model", "compare_periods", "savings_summary",
        "failure_rate_by_provider", "slowest_analyses",
    }
    assert tools["slowest_analyses"].inputSchema["properties"]["n"]["default"] == 10
    assert "Report LLM cost per model" in tools["cost_by_model"].description


# --- Calls that go through the protocol layer (mcp.call_tool) ---------------

def call_via_protocol(name, arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_schema_rejected_call_is_logged_as_rejected(demo_db, audit_file):
    with pytest.raises(ToolError):
        call_via_protocol("cost_by_model", {"period_days": "abc"})
    (entry,) = read_entries(audit_file)  # exactly one line
    assert entry["tool"] == "cost_by_model"
    assert entry["args"] == {"period_days": "abc"}
    assert entry["status"] == "rejected"
    assert entry["error_type"] == "ValidationError"
    assert entry["row_count"] is None


def test_unknown_tool_is_logged_as_rejected_with_bounded_arguments(demo_db, audit_file):
    huge = {f"key{i}": "y" * 1000 for i in range(100)}
    with pytest.raises(ToolError):
        call_via_protocol("drop_everything\n" + "z" * 500, huge)
    (entry,) = read_entries(audit_file)
    assert entry["status"] == "rejected" and entry["error_type"] == "ToolError"
    assert len(entry["args"]) == 10
    assert len(entry["tool"]) <= 53
    assert max(len(line) for line in audit_file.read_text().splitlines()) < 1500


def test_error_inside_a_tool_is_logged_once_not_twice(demo_db, audit_file):
    with pytest.raises(ToolError):
        call_via_protocol("cost_by_model", {"period_days": 0})
    (entry,) = read_entries(audit_file)
    assert entry["status"] == "error" and entry["error_type"] == "ValueError"


def test_success_via_protocol_is_logged_once(demo_db, audit_file):
    call_via_protocol("cost_by_model", {"period_days": 7})
    (entry,) = read_entries(audit_file)
    assert entry["status"] == "ok" and entry["row_count"] == 2
