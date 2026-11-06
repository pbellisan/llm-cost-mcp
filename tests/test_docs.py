"""Keep the documentation in step with the code."""
import asyncio
from pathlib import Path

import pytest

import db
import server

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
TOOLS = asyncio.run(server.mcp.list_tools())


@pytest.mark.parametrize("doc_name,doc", [("README.md", README), ("AGENTS.md", AGENTS)])
def test_every_tool_is_documented(doc_name, doc):
    for tool in TOOLS:
        assert f"`{tool.name}`" in doc, f"{tool.name} missing from {doc_name}"


def test_every_tool_argument_is_documented():
    for tool in TOOLS:
        for argument in tool.inputSchema.get("properties", {}):
            assert f"`{argument}`" in README, f"{tool.name}.{argument} missing from README.md"
            assert f"`{argument}`" in AGENTS, f"{tool.name}.{argument} missing from AGENTS.md"


def test_documented_bounds_match_the_code():
    for value in (db.MAX_PERIOD_DAYS, db.MAX_COMPARE_DAYS, db.MAX_ROWS):
        assert f"1-{value}" in README, f"bound 1-{value} missing from README.md"
        assert f"1-{value}" in AGENTS, f"bound 1-{value} missing from AGENTS.md"


@pytest.mark.parametrize("doc_name,doc", [("README.md", README), ("AGENTS.md", AGENTS)])
def test_docs_state_the_synthetic_data_and_read_only_rules(doc_name, doc):
    assert "synthetic" in doc.lower(), doc_name
    assert "read-only" in doc.lower(), doc_name
    assert "schema://analyses" in doc and "monthly_cost_review" in doc, doc_name


def test_readme_names_the_environment_variables():
    assert "LLM_COST_DB" in README and "LLM_COST_AUDIT" in README


def test_requirements_pin_mcp_below_2():
    text = (ROOT / "requirements.txt").read_text()
    assert "mcp[cli]" in text and "<2" in text
