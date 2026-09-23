"""Local outdoor temperature via Open-Meteo (https://open-meteo.com) -- a
free, keyless weather API, so unlike pihole_stats there's no credential to
manage. Coordinates are resolved once via Open-Meteo's own geocoding API
and hardcoded here rather than looked up on every request.
"""
import os

import httpx

# Croix, 59170, France (Hauts-de-France / Nord), resolved via
# https://geocoding-api.open-meteo.com/v1/search?name=Croix&country=FR
LATITUDE = os.environ.get("SPECTRUM_WEATHER_LAT", "50.67846")
LONGITUDE = os.environ.get("SPECTRUM_WEATHER_LON", "3.1493")
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


async def fetch_temperature(client: httpx.AsyncClient) -> dict | None:
    resp = await client.get(
        FORECAST_URL,
        params={
            "latitude": LATITUDE,
            "longitude": LONGITUDE,
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "Europe/Paris",
        },
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    current = data.get("current", {})
    daily = data.get("daily", {})
    temp = current.get("temperature_2m")
    if temp is None:
        return None
    daily_max = daily.get("temperature_2m_max", [])
    daily_min = daily.get("temperature_2m_min", [])
    return {
        "temp_c": round(temp, 1),
        "weather_code": current.get("weather_code"),
        "wind_kmh": round(current["wind_speed_10m"]) if current.get("wind_speed_10m") is not None else None,
        "humidity_pct": current.get("relative_humidity_2m"),
        # today's forecast is always index 0 -- Open-Meteo's daily arrays
        # start with the current day, not tomorrow.
        "temp_max_c": round(daily_max[0], 1) if daily_max else None,
        "temp_min_c": round(daily_min[0], 1) if daily_min else None,
    }
