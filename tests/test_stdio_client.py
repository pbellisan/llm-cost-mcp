"""End-to-end: start the real server as a subprocess and talk to it with the MCP Python client."""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def _session_run(env, calls):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=env, cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            results = [await session.call_tool(name, args) for name, args in calls]
            return tools, results


def run(demo_db, tmp_path, calls):
    audit = tmp_path / "audit.jsonl"
    env = {"LLM_COST_DB": str(demo_db), "LLM_COST_AUDIT": str(audit)}
    tools, results = asyncio.run(_session_run(env, calls))
    entries = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
    return tools, results, entries


def test_real_protocol_round_trip_and_audit(demo_db, tmp_path):
    tools, (result,), entries = run(demo_db, tmp_path, [("cost_by_model", {"period_days": 7})])
    assert sorted(tools) == [
        "compare_periods", "cost_by_model", "failure_rate_by_provider", "savings_summary", "slowest_analyses",
    ]
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["total_cost_usd"] == 3.25
    assert [m["model"] for m in payload["models"]] == ["alpha-large", "beta-lite"]
    assert len(entries) == 1
    assert entries[0]["tool"] == "cost_by_model" and entries[0]["args"] == {"period_days": 7}
    assert entries[0]["row_count"] == 2 and entries[0]["status"] == "ok"


def test_invalid_argument_is_reported_as_tool_error_and_audited(demo_db, tmp_path):
    _, (result,), entries = run(demo_db, tmp_path, [("cost_by_model", {"period_days": 0})])
    assert result.isError
    assert len(entries) == 1
    assert entries[0]["status"] == "error" and entries[0]["error_type"] == "ValueError"


def test_schema_violation_over_the_wire_is_audited_as_rejected(demo_db, tmp_path):
    _, (result,), entries = run(demo_db, tmp_path, [("cost_by_model", {"period_days": "abc"})])
    assert result.isError
    assert len(entries) == 1
    assert entries[0]["status"] == "rejected" and entries[0]["error_type"] == "ValidationError"
    assert entries[0]["args"] == {"period_days": "abc"}


def test_resource_and_prompt_over_the_wire(demo_db, tmp_path):
    async def go():
        env = {"LLM_COST_DB": str(demo_db), "LLM_COST_AUDIT": str(tmp_path / "audit.jsonl")}
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=env, cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resources = await session.list_resources()
                schema = await session.read_resource("schema://analyses")
                prompts = await session.list_prompts()
                prompt = await session.get_prompt("monthly_cost_review", {"period_days": "14"})
                return resources, schema, prompts, prompt

    resources, schema, prompts, prompt = asyncio.run(go())
    assert [str(r.uri) for r in resources.resources] == ["schema://analyses"]
    doc = json.loads(schema.contents[0].text)
    assert doc["table"] == "analyses" and doc["row_count"] == 7
    assert [p.name for p in prompts.prompts] == ["monthly_cost_review"]
    assert "period_days=14" in prompt.messages[0].content.text
