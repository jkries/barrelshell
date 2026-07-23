"""Bundled skill: current conditions + 3-day forecast (Open-Meteo, no key).
US ZIP codes route through Zippopotam because Open-Meteo's name geocoder
mishandles them. Self-contained.
"""
import re

import requests

_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
        55: "heavy drizzle", 61: "light rain", 63: "rain",
        65: "heavy rain", 66: "freezing rain", 71: "light snow",
        73: "snow", 75: "heavy snow", 77: "snow grains",
        80: "rain showers", 81: "rain showers", 82: "violent showers",
        85: "snow showers", 86: "snow showers", 95: "thunderstorm",
        96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail"}


def weather(place: str, chat_id: int) -> str:
    place = place.strip()
    zip_m = re.fullmatch(r"(\d{5})(?:-\d{4})?", place)
    try:
        if zip_m:  # US ZIP — Open-Meteo's geocoder mishandles these
            z = requests.get(
                f"https://api.zippopotam.us/us/{zip_m.group(1)}",
                timeout=15).json()
            p = z["places"][0]
            loc = {"latitude": float(p["latitude"]),
                   "longitude": float(p["longitude"]),
                   "name": p["place name"],
                   "admin1": p["state abbreviation"],
                   "country_code": "US"}
        else:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 1}, timeout=15).json()
            if not geo.get("results"):
                return f"(no location found for '{place}')"
            loc = geo["results"][0]
        wx = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["latitude"], "longitude": loc["longitude"],
                "current": "temperature_2m,apparent_temperature,"
                           "weather_code,wind_speed_10m,precipitation",
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "auto", "forecast_days": 3},
            timeout=15).json()
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        return f"(weather lookup failed: {e})"

    cur = wx["current"]
    lines = [f"Weather for {loc['name']}, "
             f"{loc.get('admin1', '')} {loc.get('country_code', '')}:",
             f"Now: {cur['temperature_2m']}°F "
             f"(feels {cur['apparent_temperature']}°F), "
             f"{_WMO.get(cur['weather_code'], 'unknown')}, "
             f"wind {cur['wind_speed_10m']} mph"]
    d = wx["daily"]
    for i, day in enumerate(d["time"]):
        lines.append(f"{day}: {d['temperature_2m_min'][i]:.0f}–"
                     f"{d['temperature_2m_max'][i]:.0f}°F, "
                     f"{_WMO.get(d['weather_code'][i], '?')}, "
                     f"precip {d['precipitation_probability_max'][i]}%")
    return "\n".join(lines)


SKILL = {
    "name": "weather",
    "desc": "Get current conditions and a 3-day forecast for a city name "
            "or US ZIP code. Emit <weather>city or place name</weather>",
    "handler": weather,
}
