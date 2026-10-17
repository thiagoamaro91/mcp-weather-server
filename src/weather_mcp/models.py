"""Typed tool results.

Returning pydantic models gives every tool an output schema, so Claude gets
structured JSON plus a text rendering, and clients can validate the result.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Location(BaseModel):
    name: str
    country: str | None = None
    region: str | None = Field(default=None, description="State, province or similar")
    latitude: float
    longitude: float
    timezone: str | None = None


class CurrentWeather(BaseModel):
    location: Location
    observed_at: str = Field(description="Local time of the observation (ISO 8601)")
    temperature_c: float
    feels_like_c: float | None = None
    humidity_pct: float | None = None
    precipitation_mm: float | None = None
    wind_speed_kmh: float | None = None
    condition: str = Field(description="Plain-language description of the WMO weather code")


class DailyForecast(BaseModel):
    date: str
    condition: str
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    precipitation_mm: float | None = None
    precipitation_chance_pct: float | None = None
    wind_max_kmh: float | None = None


class Forecast(BaseModel):
    location: Location
    days: list[DailyForecast]


class CityComparison(BaseModel):
    cities: list[CurrentWeather]
    warmest: str
    coldest: str
    spread_c: float = Field(description="Warmest minus coldest current temperature")
