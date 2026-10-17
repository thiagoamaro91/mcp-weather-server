"""The MCP server: three read-only weather tools for Claude.

Each tool validates its input, calls `OpenMeteoClient`, and turns any expected
failure (unknown city, bad input, upstream outage) into a `ToolError`, which
Claude receives as a readable error message instead of a crash.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from weather_mcp import __version__
from weather_mcp.client import MAX_FORECAST_DAYS, OpenMeteoClient, WeatherError
from weather_mcp.models import CityComparison, CurrentWeather, Forecast

INSTRUCTIONS = (
    "Live weather from Open-Meteo. Pass a city name such as 'Lisbon'; add a two-letter "
    "country code to disambiguate ('Springfield, US'). Temperatures are Celsius, wind km/h, "
    "precipitation mm."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)

City = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        description="City name, optionally with a country code, e.g. 'Paris' or 'Paris, US'",
    ),
]


def build_server(weather: OpenMeteoClient | None = None) -> MCPServer:
    """Create the MCP server. Tests pass a client wired to a mock HTTP transport."""
    api = weather or OpenMeteoClient()
    mcp = MCPServer("weather", version=__version__, instructions=INSTRUCTIONS)

    @mcp.tool(title="Current weather", annotations=READ_ONLY)
    async def get_current(city: City) -> CurrentWeather:
        """Current conditions for a city: temperature, feels-like, humidity, wind, precipitation."""
        try:
            return await api.current(city)
        except WeatherError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(title="Daily forecast", annotations=READ_ONLY)
    async def get_forecast(
        city: City,
        days: Annotated[
            int, Field(ge=1, le=MAX_FORECAST_DAYS, description="Number of days, 1 to 16")
        ] = 3,
    ) -> Forecast:
        """Daily forecast for a city: min/max temperature, rain chance and amount, max wind."""
        try:
            return await api.forecast(city, days)
        except WeatherError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(title="Compare cities", annotations=READ_ONLY)
    async def compare_cities(
        cities: Annotated[
            list[str],
            Field(min_length=2, max_length=5, description="Two to five city names"),
        ],
    ) -> CityComparison:
        """Current weather side by side for 2-5 cities, with the warmest and coldest named."""
        try:
            results = await asyncio.gather(*(api.current(c) for c in cities))
        except WeatherError as exc:
            raise ToolError(str(exc)) from exc
        warmest = max(results, key=lambda r: r.temperature_c)
        coldest = min(results, key=lambda r: r.temperature_c)
        return CityComparison(
            cities=list(results),
            warmest=warmest.location.name,
            coldest=coldest.location.name,
            spread_c=round(warmest.temperature_c - coldest.temperature_c, 1),
        )

    return mcp


def main() -> None:
    """Entry point: serve over stdio, which is what Claude Desktop and Claude Code expect."""
    # stdout carries the protocol; keep per-request httpx logging out of stderr noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
