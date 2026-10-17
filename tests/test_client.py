"""Unit tests for the HTTP client: parsing, errors, validation and caching."""

from __future__ import annotations

import httpx
import pytest

from weather_mcp.cache import TTLCache
from weather_mcp.client import (
    CityNotFoundError,
    InvalidInputError,
    OpenMeteoClient,
    UpstreamError,
    parse_city,
)

pytestmark = pytest.mark.anyio

GEO = "geocoding-api.open-meteo.com"
FORECAST = "api.open-meteo.com"


async def test_geocode_returns_location(weather_client):
    loc = await weather_client.geocode("  Lisbon ")
    assert (loc.name, loc.country, loc.timezone) == ("Lisbon", "Portugal", "Europe/Lisbon")


async def test_country_code_suffix_is_sent_upstream(weather_client, fake_api):
    loc = await weather_client.geocode("Springfield, us")
    assert loc.region == "Illinois"
    assert fake_api.requests[-1].url.params["countryCode"] == "US"


async def test_unknown_city_raises_clear_error(weather_client):
    with pytest.raises(CityNotFoundError, match="No place called 'Atlantis'"):
        await weather_client.geocode("Atlantis")


async def test_current_weather_is_parsed(weather_client):
    cur = await weather_client.current("Oslo")
    assert cur.temperature_c == 4.2
    assert cur.condition == "Partly cloudy"
    assert cur.location.name == "Oslo"


async def test_forecast_returns_requested_days(weather_client, fake_api):
    fc = await weather_client.forecast("Cairo", 5)
    assert len(fc.days) == 5
    assert fc.days[0].date == "2026-10-17"
    assert fc.days[0].condition == "Slight rain"
    assert fc.days[0].precipitation_chance_pct == 70
    assert fake_api.requests[-1].url.params["forecast_days"] == "5"


@pytest.mark.parametrize("days", [0, 17, -3])
async def test_forecast_rejects_out_of_range_days(weather_client, fake_api, days):
    with pytest.raises(InvalidInputError, match="between 1 and 16"):
        await weather_client.forecast("Cairo", days)
    assert fake_api.requests == []  # rejected before any HTTP call


@pytest.mark.parametrize("bad", ["", "   ", "x" * 101])
def test_parse_city_rejects_bad_input(bad):
    with pytest.raises(InvalidInputError):
        parse_city(bad)


def test_parse_city_keeps_multiword_names():
    assert parse_city("Rio de Janeiro") == ("Rio de Janeiro", None)
    assert parse_city("Porto Alegre, br") == ("Porto Alegre", "BR")


async def test_upstream_5xx_becomes_upstream_error(weather_client, fake_api):
    fake_api.fail_with = lambda req: httpx.Response(503, text="maintenance")
    with pytest.raises(UpstreamError, match="HTTP 503"):
        await weather_client.current("Lisbon")


async def test_upstream_400_reason_is_surfaced(weather_client, fake_api):
    fake_api.fail_with = lambda req: httpx.Response(
        400, json={"error": True, "reason": "Latitude must be in range of -90 to 90°."}
    )
    with pytest.raises(UpstreamError, match="Latitude must be in range"):
        await weather_client.current("Lisbon")


async def test_timeout_becomes_upstream_error(weather_client, fake_api):
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    fake_api.fail_with = timeout
    with pytest.raises(UpstreamError, match="did not answer in time"):
        await weather_client.current("Lisbon")


async def test_non_json_body_becomes_upstream_error(weather_client, fake_api):
    fake_api.fail_with = lambda req: httpx.Response(200, text="<html>oops</html>")
    with pytest.raises(UpstreamError, match="not JSON"):
        await weather_client.geocode("Lisbon")


async def test_repeat_calls_are_served_from_cache(weather_client, fake_api):
    await weather_client.current("Lisbon")
    await weather_client.current("lisbon")
    await weather_client.current("LISBON")
    assert fake_api.count(GEO) == 1
    assert fake_api.count(FORECAST) == 1


async def test_weather_cache_expires(weather_client, fake_api, clock):
    await weather_client.current("Lisbon")
    clock.advance(11 * 60)  # weather TTL is 10 minutes, geocode TTL is 24 hours
    await weather_client.current("Lisbon")
    assert fake_api.count(GEO) == 1
    assert fake_api.count(FORECAST) == 2


def test_ttl_cache_evicts_oldest_when_full():
    cache = TTLCache(60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # touch "a" so "b" is now the oldest
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1 and cache.get("c") == 3


async def test_default_client_sets_timeouts():
    client = OpenMeteoClient()
    try:
        assert client._http.timeout.connect == 5.0
        assert client._http.timeout.read == 10.0
    finally:
        await client.aclose()
