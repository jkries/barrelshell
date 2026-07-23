"""Bundled skill: current conditions + 3-day forecast (Open-Meteo, no key).

Location handling is the fiddly part. Open-Meteo's geocoder matches
place NAMES only — "Milford, NJ" finds nothing, because that is not a
name. So this skill parses what people (and models) actually write:
a bare ZIP, a ZIP buried in a longer string, or "City, Qualifier"
where the qualifier is a state, abbreviation, or country.

Self-contained — no core imports.
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

_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut",
    "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii",
    "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine",
    "md": "maryland", "ma": "massachusetts", "mi": "michigan",
    "mn": "minnesota", "ms": "mississippi", "mo": "missouri",
    "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico",
    "ny": "new york", "nc": "north carolina", "nd": "north dakota",
    "oh": "ohio", "ok": "oklahoma", "or": "oregon",
    "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia",
    "wa": "washington", "wv": "west virginia", "wi": "wisconsin",
    "wy": "wyoming", "dc": "district of columbia",
}

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _from_zip(zipcode: str) -> dict:
    z = requests.get(f"https://api.zippopotam.us/us/{zipcode}",
                     timeout=15).json()
    p = z["places"][0]
    return {"latitude": float(p["latitude"]),
            "longitude": float(p["longitude"]),
            "name": p["place name"],
            "admin1": p["state abbreviation"],
            "country_code": "US"}


def _from_name(place: str) -> dict:
    """Split 'City, Qualifier' and match the qualifier against the
    geocoder's state/country fields, since the API only searches names."""
    parts = [p.strip() for p in re.split(r"[,/]", place) if p.strip()]
    city = parts[0] if parts else place.strip()
    quals = [q.lower() for q in parts[1:]]
    quals += [_STATES[q] for q in quals if q in _STATES]

    geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                       params={"name": city, "count": 10},
                       timeout=15).json()
    results = geo.get("results") or []
    if not results:
        return {}
    if not quals:
        return results[0]
    for r in results:
        fields = {str(r.get(k, "")).lower() for k in
                  ("admin1", "admin2", "country", "country_code")}
        if any(q in fields for q in quals):
            return r
    return results[0]   # qualifier didn't match; best name match wins


def weather(place: str, chat_id: int) -> str:
    place = place.strip()
    try:
        # A ZIP anywhere in the string wins — it's unambiguous, and
        # models often write "Milford, NJ 08848".
        zip_m = ZIP_RE.search(place)
        loc = _from_zip(zip_m.group(1)) if zip_m else _from_name(place)
        if not loc:
            return (f"(no location found for '{place}' — try just the "
                    f"city name, or a US ZIP code)")
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

    where = ", ".join(str(p) for p in
                      (loc.get("name"), loc.get("admin1"),
                       loc.get("country_code")) if p)
    cur = wx["current"]
    lines = [f"Weather for {where}:",
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
    "desc": "Get current conditions and a 3-day forecast. Accepts a city "
            "name, 'City, State', 'City, Country', or a US ZIP code. "
            "Emit <weather>Milford, NJ</weather>",
    "handler": weather,
}
