"""Shared fixtures: a fake Open-Meteo behind httpx.MockTransport, and a network guard.

No test touches the internet and none needs an API key. The guard below makes
any accidental real connection fail loudly instead of silently passing.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import date, timedelta

import httpx
import pytest

from weather_mcp.client import OpenMeteoClient

CITIES = {
    "lisbon": {"name": "Lisbon", "country": "Portugal", "country_code": "PT", "admin1": "Lisbon",
               "latitude": 38.72, "longitude": -9.13, "timezone": "Europe/Lisbon"},
    "oslo": {"name": "Oslo", "country": "Norway", "country_code": "NO", "admin1": "Oslo",
             "latitude": 59.91, "longitude": 10.75, "timezone": "Europe/Oslo"},
    "cairo": {"name": "Cairo", "country": "Egypt", "country_code": "EG", "admin1": "Cairo",
              "latitude": 30.06, "longitude": 31.25, "timezone": "Africa/Cairo"},
    "springfield": {"name": "Springfield", "country": "United States", "country_code": "US",
                    "admin1": "Illinois", "latitude": 39.80, "longitude": -89.64,
                    "timezone": "America/Chicago"},
}
# Current temperature per latitude, so each city gets distinct weather.
TEMPS = {38.72: 21.4, 59.91: 4.2, 30.06: 29.8, 39.80: 12.0}


class FakeOpenMeteo:
    """Answers the two Open-Meteo endpoints the server uses and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_with: Callable[[httpx.Request], httpx.Response] | None = None

    def count(self, host: str) -> int:
        return sum(r.url.host == host for r in self.requests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            return self.fail_with(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            return self._geocode(request)
        if request.url.host == "api.open-meteo.com":
            return self._forecast(request)
        return httpx.Response(404, json={"error": True, "reason": "unknown host in fake"})

    def _geocode(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params["name"].lower()
        cc = request.url.params.get("countryCode")
        hit = CITIES.get(name)
        if hit is None or (cc and hit["country_code"] != cc):
            # Real Open-Meteo answers 200 with no "results" key when nothing matches.
            return httpx.Response(200, json={"generationtime_ms": 0.4})
        return httpx.Response(200, json={"results": [hit], "generationtime_ms": 0.5})

    def _forecast(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        lat = float(params["latitude"])
        body: dict = {"latitude": lat, "longitude": float(params["longitude"])}
        if "current" in params:
            body["current"] = {
                "time": "2026-10-17T12:00",
                "interval": 900,
                "temperature_2m": TEMPS[lat],
                "apparent_temperature": TEMPS[lat] - 1.5,
                "relative_humidity_2m": 60,
                "precipitation": 0.0,
                "weather_code": 2,
                "wind_speed_10m": 14.3,
            }
        if "daily" in params:
            n = int(params["forecast_days"])
            start = date(2026, 10, 17)
            body["daily"] = {
                "time": [(start + timedelta(days=i)).isoformat() for i in range(n)],
                "weather_code": [61] * n,
                "temperature_2m_max": [TEMPS[lat] + 3] * n,
                "temperature_2m_min": [TEMPS[lat] - 5] * n,
                "precipitation_sum": [2.5] * n,
                "precipitation_probability_max": [70] * n,
                "wind_speed_10m_max": [22.0] * n,
            }
        return httpx.Response(200, json=body)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise RuntimeError("Network access is disabled in tests")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def fake_api() -> FakeOpenMeteo:
    return FakeOpenMeteo()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def weather_client(fake_api: FakeOpenMeteo, clock: FakeClock) -> OpenMeteoClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_api))
    return OpenMeteoClient(http, clock=clock)
