"""End-to-end tool tests through a real MCP client session.

`mcp.Client(server)` connects in-process, so these go through the same tool
listing, argument validation and result serialisation Claude would hit, just
without a subprocess or any network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from weather_mcp.server import build_server

pytestmark = pytest.mark.anyio

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def server(weather_client):
    return build_server(weather_client)


def error_text(result) -> str:
    assert result.is_error is True
    return " ".join(c.text for c in result.content if getattr(c, "text", None))


async def test_lists_three_read_only_tools(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_current", "get_forecast", "compare_cities"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.output_schema is not None
    days = tools["get_forecast"].input_schema["properties"]["days"]
    assert (days["minimum"], days["maximum"], days["default"]) == (1, 16, 3)


async def test_get_current(server):
    async with Client(server) as client:
        result = await client.call_tool("get_current", {"city": "Lisbon"})
    assert result.is_error is False
    data = result.structured_content
    assert data["location"]["country"] == "Portugal"
    assert data["temperature_c"] == 21.4
    assert data["condition"] == "Partly cloudy"


async def test_get_forecast_default_days(server):
    async with Client(server) as client:
        result = await client.call_tool("get_forecast", {"city": "Oslo"})
    assert result.is_error is False
    assert len(result.structured_content["days"]) == 3


async def test_get_forecast_custom_days(server):
    async with Client(server) as client:
        result = await client.call_tool("get_forecast", {"city": "Cairo", "days": 7})
    assert [d["date"] for d in result.structured_content["days"]][-1] == "2026-10-23"


async def test_compare_cities(server):
    async with Client(server) as client:
        result = await client.call_tool("compare_cities", {"cities": ["Oslo", "Cairo", "Lisbon"]})
    data = result.structured_content
    assert (data["warmest"], data["coldest"], data["spread_c"]) == ("Cairo", "Oslo", 25.6)
    assert len(data["cities"]) == 3


async def test_unknown_city_is_a_readable_tool_error(server):
    async with Client(server) as client:
        result = await client.call_tool("get_current", {"city": "Atlantis"})
    assert "No place called 'Atlantis'" in error_text(result)


async def test_compare_with_one_unknown_city_names_it(server):
    async with Client(server) as client:
        result = await client.call_tool("compare_cities", {"cities": ["Oslo", "Atlantis"]})
    assert "Atlantis" in error_text(result)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_forecast", {"city": "Oslo", "days": 0}),
        ("get_forecast", {"city": "Oslo", "days": 30}),
        ("get_current", {"city": ""}),
        ("get_current", {"city": "   "}),
        ("compare_cities", {"cities": ["Oslo"]}),
        ("compare_cities", {"cities": ["a", "b", "c", "d", "e", "f"]}),
    ],
)
async def test_bad_arguments_are_rejected(server, fake_api, tool, args):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    assert error_text(result)
    assert fake_api.requests == []  # validation happens before any HTTP call


async def test_upstream_outage_is_a_tool_error_not_a_crash(server, fake_api):
    import httpx

    fake_api.fail_with = lambda req: httpx.Response(502, text="bad gateway")
    async with Client(server) as client:
        result = await client.call_tool("get_current", {"city": "Lisbon"})
    assert "HTTP 502" in error_text(result)


async def test_server_boots_over_stdio():
    """Launch the real entry point as a subprocess and list tools over stdio (no HTTP made)."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "weather_mcp"],
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    async with Client(params) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {"get_current", "get_forecast", "compare_cities"}
