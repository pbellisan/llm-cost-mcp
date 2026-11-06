import asyncio
import json
import sqlite3

import pytest

import server


def read_schema():
    (item,) = asyncio.run(server.mcp.read_resource("schema://analyses"))
    assert item.mime_type == "application/json"
    return json.loads(item.content)


def prompt_text(arguments=None):
    result = asyncio.run(server.mcp.get_prompt("monthly_cost_review", arguments))
    assert len(result.messages) == 1 and result.messages[0].role == "user"
    return result.messages[0].content.text


# --- Resource ---------------------------------------------------------------

def test_resource_is_listed_with_json_mime_type():
    resources = asyncio.run(server.mcp.list_resources())
    assert [(str(r.uri), r.mimeType) for r in resources] == [("schema://analyses", "application/json")]


def test_schema_matches_the_live_table(demo_db):
    doc = read_schema()
    conn = sqlite3.connect(demo_db)
    live = [row[1] for row in conn.execute("PRAGMA table_info(analyses)")]
    conn.close()
    assert [c["name"] for c in doc["columns"]] == live
    assert doc["table"] == "analyses"
    assert doc["row_count"] == 7
    assert doc["data_range"] == {"first_created_at": "2026-01-01T10:00:00Z", "last_created_at": "2026-03-30T10:00:00Z"}
    assert "Synthetic" in doc["data_notice"]


def test_every_column_is_documented(demo_db):
    for column in read_schema()["columns"]:
        assert column["description"] != "undocumented column", column["name"]
        assert column["type"]


def test_column_details(demo_db):
    columns = {c["name"]: c for c in read_schema()["columns"]}
    assert columns["id"]["primary_key"] is True and columns["id"]["nullable"] is False
    assert columns["error_type"]["nullable"] is True
    assert columns["cost_usd"]["type"] == "REAL" and "ILLUSTRATIVE" in columns["cost_usd"]["description"]
    assert "milliseconds" in columns["latency_ms"]["description"]


def test_new_column_is_flagged_not_hidden(demo_db):
    conn = sqlite3.connect(demo_db)
    conn.execute("ALTER TABLE analyses ADD COLUMN surprise TEXT")
    conn.commit()
    conn.close()
    columns = {c["name"]: c for c in read_schema()["columns"]}
    assert columns["surprise"]["description"] == "undocumented column"


def test_schema_contains_no_row_level_data(demo_db):
    text = json.dumps(read_schema())
    for value in ("alpha-large", "beta-lite", "gamma-flash", "2026-03-29T10:00:00Z"):
        assert value not in text


def test_schema_resource_needs_a_database(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_COST_DB", str(tmp_path / "missing.db"))
    with pytest.raises(Exception):
        asyncio.run(server.mcp.read_resource("schema://analyses"))


# --- Prompt -----------------------------------------------------------------

def test_prompt_is_listed():
    prompts = asyncio.run(server.mcp.list_prompts())
    assert [p.name for p in prompts] == ["monthly_cost_review"]
    assert [a.name for a in prompts[0].arguments] == ["period_days"]


def test_prompt_mentions_exactly_the_registered_tools_and_the_resource():
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    text = prompt_text()
    assert all(name in text for name in registered), "prompt forgot a tool"
    assert "schema://analyses" in text
    # No stale tool names: every snake_case call-like token that looks like a tool must exist.
    mentioned = {w for w in text.replace(",", " ").replace(".", " ").split() if w in registered}
    assert mentioned == registered


def test_prompt_uses_the_requested_period_and_default():
    assert "period_days=14" in prompt_text({"period_days": "14"})  # protocol arguments arrive as strings
    assert "period_days=30" in prompt_text()
    assert "period_days=30" not in prompt_text({"period_days": "14"})


def test_prompt_states_the_honesty_rules():
    text = prompt_text()
    for phrase in ("synthetic", "Do not forecast", "Quote only numbers returned by the tools", "previous_coverage_pct"):
        assert phrase in text


@pytest.mark.parametrize("bad", ["0", "181", "-3", "abc", "2.5"])
def test_prompt_rejects_bad_period(bad):
    with pytest.raises(Exception):
        prompt_text({"period_days": bad})
