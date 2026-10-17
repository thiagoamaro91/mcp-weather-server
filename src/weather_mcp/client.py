"""Thin async client for the Open-Meteo REST API.

This is the only module that knows about HTTP. The MCP tools in `server.py`
talk to it through three methods (`geocode`, `current`, `forecast`), so
pointing the server at a different API means rewriting this file and keeping
the tool contracts and tests unchanged.

Open-Meteo is free and needs no API key: https://open-meteo.com/en/docs
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from weather_mcp.cache import TTLCache
from weather_mcp.models import CurrentWeather, DailyForecast, Forecast, Location
from weather_mcp.weather_codes import describe

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
USER_AGENT = "mcp-weather-server/0.1 (+https://open-meteo.com)"

CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "precipitation,weather_code,wind_speed_10m"
)
DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "precipitation_probability_max,wind_speed_10m_max"
)

MAX_FORECAST_DAYS = 16  # Open-Meteo's limit
MAX_CITY_LENGTH = 100

# "Springfield, US" -> name "Springfield", country code "US"
_COUNTRY_SUFFIX = re.compile(r"^(?P<name>.+?),\s*(?P<cc>[A-Za-z]{2})$")


class WeatherError(Exception):
    """Base class for errors the tools turn into a readable message for Claude."""


class CityNotFoundError(WeatherError):
    pass


class InvalidInputError(WeatherError):
    pass


class UpstreamError(WeatherError):
    """Open-Meteo was unreachable, slow, or returned something unusable."""


class OpenMeteoClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        geocode_ttl: float = 24 * 3600,
        weather_ttl: float = 10 * 60,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._http = http or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        clock_kw = {"clock": clock} if clock else {}
        # Place names barely change, so cache them for a day; weather for 10 minutes
        # (Open-Meteo refreshes current conditions every 15).
        self._geo_cache = TTLCache(geocode_ttl, **clock_kw)
        self._weather_cache = TTLCache(weather_ttl, **clock_kw)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- public API used by the MCP tools -------------------------------

    async def geocode(self, city: str) -> Location:
        name, country_code = parse_city(city)
        key = (name.lower(), country_code)
        cached = self._geo_cache.get(key)
        if cached is not None:
            return cached

        params: dict[str, Any] = {"name": name, "count": 1, "language": "en", "format": "json"}
        if country_code:
            params["countryCode"] = country_code
        data = await self._get_json(GEOCODING_URL, params)

        results = data.get("results") or []
        if not results:
            where = f" in country {country_code}" if country_code else ""
            raise CityNotFoundError(
                f"No place called '{name}' found{where}. Check the spelling, or add a "
                "two-letter country code, for example 'Springfield, US'."
            )
        top = results[0]
        location = Location(
            name=top["name"],
            country=top.get("country"),
            region=top.get("admin1"),
            latitude=top["latitude"],
            longitude=top["longitude"],
            timezone=top.get("timezone"),
        )
        self._geo_cache.set(key, location)
        return location

    async def current(self, city: str) -> CurrentWeather:
        location = await self.geocode(city)
        data = await self._weather(location, {"current": CURRENT_FIELDS})
        cur = data.get("current")
        if not isinstance(cur, dict) or cur.get("temperature_2m") is None:
            raise UpstreamError("Open-Meteo returned no current conditions for this place.")
        return CurrentWeather(
            location=location,
            observed_at=cur.get("time", ""),
            temperature_c=cur["temperature_2m"],
            feels_like_c=cur.get("apparent_temperature"),
            humidity_pct=cur.get("relative_humidity_2m"),
            precipitation_mm=cur.get("precipitation"),
            wind_speed_kmh=cur.get("wind_speed_10m"),
            condition=describe(cur.get("weather_code")),
        )

    async def forecast(self, city: str, days: int) -> Forecast:
        if not 1 <= days <= MAX_FORECAST_DAYS:
            raise InvalidInputError(f"days must be between 1 and {MAX_FORECAST_DAYS}, got {days}.")
        location = await self.geocode(city)
        data = await self._weather(location, {"daily": DAILY_FIELDS, "forecast_days": days})
        daily = data.get("daily")
        if not isinstance(daily, dict) or not daily.get("time"):
            raise UpstreamError("Open-Meteo returned no daily forecast for this place.")

        def col(name: str, i: int) -> Any:
            values = daily.get(name) or []
            return values[i] if i < len(values) else None

        rows = [
            DailyForecast(
                date=date,
                condition=describe(col("weather_code", i)),
                temp_min_c=col("temperature_2m_min", i),
                temp_max_c=col("temperature_2m_max", i),
                precipitation_mm=col("precipitation_sum", i),
                precipitation_chance_pct=col("precipitation_probability_max", i),
                wind_max_kmh=col("wind_speed_10m_max", i),
            )
            for i, date in enumerate(daily["time"])
        ]
        return Forecast(location=location, days=rows)

    # ---- internals ------------------------------------------------------

    async def _weather(self, location: Location, extra: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": "auto",
            **extra,
        }
        key = tuple(sorted(params.items()))
        cached = self._weather_cache.get(key)
        if cached is not None:
            return cached
        data = await self._get_json(FORECAST_URL, params)
        self._weather_cache.set(key, data)
        return data

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise UpstreamError("Open-Meteo did not answer in time. Try again in a moment.") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Could not reach Open-Meteo ({type(exc).__name__}).") from exc

        if response.status_code >= 500:
            raise UpstreamError(
                f"Open-Meteo is having trouble (HTTP {response.status_code}). Try again shortly."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError("Open-Meteo returned a response that is not JSON.") from exc
        if response.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
            reason = data.get("reason") if isinstance(data, dict) else None
            raise UpstreamError(
                f"Open-Meteo rejected the request (HTTP {response.status_code})"
                + (f": {reason}" if reason else ".")
            )
        if not isinstance(data, dict):
            raise UpstreamError("Open-Meteo returned an unexpected payload.")
        return data


def parse_city(raw: str) -> tuple[str, str | None]:
    """Validate a city argument and split off an optional ', CC' country code."""
    city = (raw or "").strip()
    if not city:
        raise InvalidInputError("city must not be empty.")
    if len(city) > MAX_CITY_LENGTH:
        raise InvalidInputError(f"city is too long (max {MAX_CITY_LENGTH} characters).")
    match = _COUNTRY_SUFFIX.match(city)
    if match:
        return match["name"].strip(), match["cc"].upper()
    return city, None
