# services/weather.py
"""Live weather via Open-Meteo (free, no API key required).

Resolves a configurable location name to coordinates, then fetches current
conditions plus a short daily forecast. Results are cached briefly so repeated
widget opens / briefings don't hammer the API.
"""
import os
import time
import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Where to report weather for. Override with GRACE_WEATHER_LOCATION.
DEFAULT_LOCATION = os.environ.get("GRACE_WEATHER_LOCATION", "Lagos")

# Cache the resolved forecast for this many seconds.
_CACHE_TTL = 600  # 10 minutes
_cache = {}        # location -> (timestamp, payload)
_geocode_cache = {}  # location -> (lat, lon, label)

# WMO weather codes -> (human text, emoji). Grouped to the meaningful buckets.
_WMO = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Dense drizzle", "🌦️"),
    56: ("Freezing drizzle", "🌧️"),
    57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌦️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"),
    67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "🌨️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Rain showers", "🌦️"),
    81: ("Rain showers", "🌧️"),
    82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"),
    86: ("Snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm, hail", "⛈️"),
    99: ("Thunderstorm, hail", "⛈️"),
}


def _describe(code):
    return _WMO.get(int(code), ("Unknown", "🌡️"))


async def _geocode(location, client):
    """Resolve a place name to (lat, lon, 'City, CC'). Cached for the process."""
    if location in _geocode_cache:
        return _geocode_cache[location]
    resp = await client.get(
        GEOCODE_URL, params={"name": location, "count": 1, "language": "en"}
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    if not results:
        raise ValueError(f"Could not find location '{location}'.")
    r = results[0]
    lat, lon = r["latitude"], r["longitude"]
    label = r.get("name", location)
    country = r.get("country_code", "")
    full = f"{label}, {country}" if country else label
    resolved = (lat, lon, full)
    _geocode_cache[location] = resolved
    return resolved


async def get_weather(location=None):
    """Return a weather payload dict, or {'error': msg} on failure.

    Payload keys: location, temp_c, condition, icon, humidity, wind, feels_like,
    forecast (list of {day, icon, temp}).
    """
    location = (location or DEFAULT_LOCATION).strip() or DEFAULT_LOCATION

    cached = _cache.get(location)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            lat, lon, label = await _geocode(location, client)
            resp = await client.get(
                FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max",
                    "timezone": "auto",
                    "forecast_days": 6,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.error(f"Weather API error: {e}", exc_info=True)
        return {"error": "Weather service is unreachable right now."}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Weather unexpected error: {e}", exc_info=True)
        return {"error": "Weather lookup failed unexpectedly."}

    cur = data.get("current", {})
    condition, icon = _describe(cur.get("weather_code", 0))

    # Build the next-days forecast (skip index 0 = today).
    forecast = []
    daily = data.get("daily", {})
    dtimes = daily.get("time", []) or []
    dcodes = daily.get("weather_code", []) or []
    dtemps = daily.get("temperature_2m_max", []) or []
    for i in range(1, min(6, len(dtimes))):
        try:
            day_name = datetime.strptime(dtimes[i], "%Y-%m-%d").strftime("%a").upper()
        except (ValueError, TypeError):
            day_name = ""
        _, fic = _describe(dcodes[i] if i < len(dcodes) else 0)
        temp = round(dtemps[i]) if i < len(dtemps) and dtemps[i] is not None else "--"
        forecast.append({"day": day_name, "icon": fic, "temp": temp})

    payload = {
        "location": label,
        "temp_c": round(cur.get("temperature_2m", 0)),
        "condition": condition,
        "icon": icon,
        "humidity": round(cur.get("relative_humidity_2m", 0)),
        "wind": round(cur.get("wind_speed_10m", 0) / 3.6, 1),  # km/h -> m/s
        "feels_like": round(cur.get("apparent_temperature", 0)),
        "forecast": forecast,
    }
    _cache[location] = (time.time(), payload)
    return payload


def summarize(payload) -> str:
    """A one-line spoken summary for chat/briefing."""
    if not payload or "error" in payload:
        return "I couldn't pull the weather right now."
    return (
        f"It's {payload['temp_c']}°C and {payload['condition'].lower()} "
        f"in {payload['location']}, feeling like {payload['feels_like']}°C."
    )
