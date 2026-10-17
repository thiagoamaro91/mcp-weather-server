# mcp-weather-server

A small, tested [Model Context Protocol](https://modelcontextprotocol.io) server that connects Claude to a public REST API, here [Open-Meteo](https://open-meteo.com) (free, no API key). Ask Claude "Do I need an umbrella in Lisbon this weekend?" and it calls these tools instead of guessing.

It is deliberately small: one HTTP client module, one server module, typed results, and a test suite that runs offline in about a second. The same shape works for any REST API, database or SaaS tool.

## Tools

| Tool | Arguments | Returns |
|------|-----------|---------|
| `get_current` | `city` | Temperature, feels-like, humidity, wind, precipitation, condition |
| `get_forecast` | `city`, `days` (1-16, default 3) | Daily min/max, rain chance and amount, max wind, condition |
| `compare_cities` | `cities` (2-5 names) | Current weather for each, plus warmest, coldest and the spread |

- City names can carry a country code to disambiguate: `"Springfield, US"`.
- Every tool is marked read-only and returns structured JSON with an output schema, so Claude (or any MCP client) gets typed data, not just text.
- Units: Celsius, km/h, mm.

## What makes it production-shaped

- **Input validation** in the tool schema (Claude sees the limits) and again in code (empty or oversized city, `days` out of range). Bad input is rejected before any HTTP call.
- **Clear errors.** Unknown city, upstream 4xx/5xx, timeouts and non-JSON bodies all come back to Claude as a readable tool error ("No place called 'Atlantis' found. Check the spelling, or add a two-letter country code..."), never a stack trace.
- **Timeouts** on every request (5 s connect, 10 s read).
- **Response cache**: place lookups for 24 hours, weather for 10 minutes, so repeated questions do not hammer the API.
- **Tests with no network and no key.** HTTP is faked with `httpx.MockTransport`, a guard fails any test that tries to open a real socket, and the tools are exercised through a real MCP client session, plus one test that boots the actual stdio entry point.

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> mcp-weather-server
cd mcp-weather-server
uv sync
uv run weather-mcp        # starts the stdio server; Ctrl+C to stop
```

Without uv: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`, then `.venv/bin/weather-mcp`.

### Use it from Claude Code

```bash
claude mcp add weather -- uv --directory /absolute/path/to/mcp-weather-server run weather-mcp
```

Then ask: *"What's the 5-day forecast for Oslo?"* (`claude mcp list` shows it as connected.)

### Use it from Claude Desktop

Add this to `claude_desktop_config.json` (Settings > Developer > Edit Config) and restart Claude Desktop:

```json
{
  "mcpServers": {
    "weather": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-weather-server", "run", "weather-mcp"]
    }
  }
}
```

## Run the tests

```bash
uv run pytest -q
```

or, in any environment with the dependencies installed, `python -m pytest -q`. The pytest config puts `src/` on the path, so no install step is needed for the tests themselves. No network access or API key is used.

## Layout

```
src/weather_mcp/
  client.py         # the only file that knows about HTTP: Open-Meteo calls, errors, cache use
  server.py         # MCP tool definitions, validation, error mapping, stdio entry point
  models.py         # typed results (pydantic) that become the tools' output schemas
  cache.py          # tiny TTL cache with an injectable clock
  weather_codes.py  # WMO weather code -> plain-language condition
tests/
  conftest.py       # fake Open-Meteo, fake clock, no-network guard
  test_client.py    # parsing, errors, validation, caching
  test_server.py    # every tool through a real MCP client session, plus a stdio boot test
```

## How I'd adapt this to your API

The design keeps the API-specific part in one file so the rest carries over unchanged:

1. **Swap `client.py`** for a client of your API (REST, GraphQL, a database driver, a SaaS SDK). Keep the same pattern: async calls, timeouts, auth from an environment variable (never hard-coded), and a small set of named exceptions for "not found", "bad input" and "upstream failed".
2. **Define the tools around what your users ask**, not around your endpoints. Typically three to six tools with tight argument schemas and typed results in `models.py`. Write actions (create, update) get explicit confirmation-friendly arguments and are marked as such.
3. **Keep the tests' shape**: a fake of your API behind `httpx.MockTransport` (or a throwaway SQLite database), unit tests for the client, and end-to-end tests that call each tool through an MCP client session. They run in CI without credentials.
4. **Ship the config snippet** for Claude Desktop and Claude Code, as above, with the environment variables your API needs.

## Built with

Python, the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp` 2.x, `MCPServer`), httpx and pydantic. Built with Claude Code.

## License

MIT, see [LICENSE](LICENSE).
