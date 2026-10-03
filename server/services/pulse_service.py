"""Orb Pulse — Environmental Intelligence Service

One-directional data ingestion: the world's data flows IN,
but patient data NEVER flows out.

Provides:
  - Real-time weather & 7-day forecast (NWS / OpenWeatherMap)
  - Air Quality Index (AirNow EPA)
  - CDC flu surveillance data
  - FDA drug recall alerts cross-referenced with patient medications
  - Clinical risk adjustments based on environmental conditions
  - NTP-synced timestamps for audit compliance

Architecture:
  - PULL ONLY — no patient data is ever transmitted externally
  - All feeds are free public APIs, zero recurring cost
  - Every ingestion event is logged with timestamp + source hash
"""

import asyncio
import hashlib
import json
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

# ══════════════════════════════════════════════════════════════════════
# OUTBOUND LANE — the ONLY genuinely-external httpx clients in the server
# ══════════════════════════════════════════════════════════════════════
#
# Every other httpx client in server/ talks to loopback (Ollama) and therefore
# passes trust_env=False, because httpx.AsyncClient.__init__ runs SYNCHRONOUSLY
# on the event loop and, with trust_env on, resolves the proxy configuration
# there — on macOS that is an untimed IPC call into `configd`, which is what
# wedged /api/health for 3.5 hours on 2026-08-01 (see services/model_router.py).
#
# This module is the exception and must NOT get trust_env=False. These four
# feeds go to api.open-meteo.com, air-quality-api.open-meteo.com, api.fda.gov
# and data.cdc.gov. A hospital network may well require HTTP_PROXY/HTTPS_PROXY
# to reach the public internet at all, and it may pin a private CA via
# SSL_CERT_FILE. Silently disabling both would turn a visible hang into an
# invisible "Pulse just shows no data", which is worse.
#
# So: keep the environment trusted, but resolve it ONCE per process instead of
# on every call. Passing an explicit `transport=` makes httpx skip its env-proxy
# lookup (`allow_env_proxies = trust_env and transport is None` in
# httpx/_client.py), while trust_env=True is still handed to the transports so
# SSL_CERT_FILE / SSL_CERT_DIR / netrc keep working exactly as before. The proxy
# map and the SSL context are both computed on first use and cached.
#
# The cache is deliberately process-lifetime: a proxy change needs a restart,
# which is the appliance's normal config path anyway. The SSLContext is safe to
# share (thread-safe, not bound to any event loop); the transports are NOT
# cached, because an httpx transport owns a connection pool bound to the loop
# that created it and this repo has been bitten by loop-bound state before.

_env_proxy_map: dict | None = None
_external_ssl_context = None


def _external_transport_kwargs() -> dict:
    """Client kwargs for the outbound public-API lane, with proxy + TLS
    configuration resolved once per process rather than per call."""
    global _env_proxy_map, _external_ssl_context

    if _external_ssl_context is None:
        try:
            _external_ssl_context = httpx.create_ssl_context()
        except Exception:
            _external_ssl_context = True  # httpx default verification

    if _env_proxy_map is None:
        try:
            # Private helper, so it is fenced: if a future httpx moves it we
            # fall back to letting httpx do its own (per-call) resolution rather
            # than silently losing a hospital's proxy.
            from httpx._utils import get_environment_proxies

            _env_proxy_map = dict(get_environment_proxies())
        except Exception:
            _env_proxy_map = None
            return {}

    def _transport(proxy: str | None):
        return httpx.AsyncHTTPTransport(
            proxy=proxy, verify=_external_ssl_context, trust_env=True
        )

    return {
        # Explicit transport => httpx does NOT call getproxies() again.
        "transport": _transport(None),
        # ...so we supply the proxy routing ourselves, from the cached map.
        # A None value means "this pattern bypasses the proxy" (NO_PROXY), which
        # is exactly how httpx encodes it internally.
        "mounts": {
            pattern: (None if url is None else _transport(url))
            for pattern, url in _env_proxy_map.items()
        },
    }


# ── Configuration ──
# Default: Mumbai, India (change per hospital deployment)
DEFAULT_LAT = 19.0760
DEFAULT_LON = 72.8777
DEFAULT_CITY = "Mumbai"

# NWS only works for US locations — use OpenWeatherMap as universal fallback
OPENWEATHERMAP_API_KEY = ""  # Free tier: 1000 calls/day (set via env var)

# Cache TTLs (seconds)
WEATHER_CACHE_TTL = 1800        # 30 min
AQI_CACHE_TTL = 3600            # 1 hour
CDC_CACHE_TTL = 86400           # 24 hours (weekly data)
FDA_CACHE_TTL = 3600            # 1 hour
PULSE_REFRESH_INTERVAL = 900    # 15 min background refresh

# ── In-memory caches ──
_weather_cache: dict | None = None
_weather_cache_time: float = 0

_aqi_cache: dict | None = None
_aqi_cache_time: float = 0

_cdc_cache: dict | None = None
_cdc_cache_time: float = 0

_fda_cache: list | None = None
_fda_cache_time: float = 0

_alerts_cache: list = []
_pulse_log: list = []  # Audit log of all external data pulls


# ══════════════════════════════════════════════════════════════════════
# AUDIT LOGGING — Every external call is logged
# ══════════════════════════════════════════════════════════════════════

#: audit_logs.action_type of one external pull.
EGRESS_AUDIT_ACTION = "pulse_external_pull"

# Strong references to in-flight audit writes, so a fire-and-forget task is not
# garbage-collected before it runs.
_PENDING_EGRESS_AUDITS: set = set()


def _log_ingestion(source: str, url: str, status: str, data_hash: str = ""):
    """Log every external data pull for compliance audit.

    DURABLY, on the audit chain. This was an in-memory list capped at 500, so
    the record of every call this appliance made to the internet vanished on
    each restart and after 500 calls. The list stays as a cheap recent view;
    the audit row is the record."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "url": url,
        "status": status,
        "data_hash": data_hash,
        "direction": "INBOUND_ONLY",
    }
    _pulse_log.append(entry)
    # Keep last 500 entries in memory
    if len(_pulse_log) > 500:
        _pulse_log.pop(0)
    _persist_egress(entry)


def _write_egress_audit(entry: dict) -> None:
    """One audit row for one external pull. SYNC: reached through run_db from
    the loop. No patient, no user: these feeds carry no patient data."""
    import uuid
    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="ingestion",
        action_type=EGRESS_AUDIT_ACTION,
        outcome="success" if entry["status"] == "OK" else "failure",
        summary_message=f"Pulse external pull {entry['source']}: {entry['status']}"[:300],
        target_type="external_feed",
        target_id=entry["source"],
        metadata=entry,
    )


def _egress_audit_done(task) -> None:
    _PENDING_EGRESS_AUDITS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        print(f"  [Pulse] egress audit write failed: {task.exception()!r}")


def _persist_egress(entry: dict) -> None:
    """Write the audit row on the DB lane when called from the event loop (all
    the fetchers are async), or directly from a thread or a script."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is None:
            _write_egress_audit(entry)
            return
        from database import run_db
        task = loop.create_task(run_db(_write_egress_audit, entry))
        _PENDING_EGRESS_AUDITS.add(task)
        task.add_done_callback(_egress_audit_done)
    except Exception as exc:  # an audit hiccup must not break a weather card
        print(f"  [Pulse] egress audit write failed: {exc!r}")


def _hash_response(data: str) -> str:
    """SHA-256 hash of response data for audit integrity."""
    return hashlib.sha256(data.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════
# WEATHER SERVICE
# ══════════════════════════════════════════════════════════════════════

async def fetch_weather(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON) -> dict:
    """
    Fetch current weather + 7-day forecast.
    Uses Open-Meteo (free, no API key required, global coverage).
    """
    global _weather_cache, _weather_cache_time

    if _weather_cache and (time.time() - _weather_cache_time) < WEATHER_CACHE_TTL:
        return _weather_cache

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        f"weather_code,wind_speed_10m,surface_pressure"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        f"apparent_temperature_max,apparent_temperature_min,"
        f"precipitation_sum,precipitation_probability_max,"
        f"wind_speed_10m_max"
        f"&timezone=auto&forecast_days=7"
    )

    try:
        async with httpx.AsyncClient(timeout=10, **_external_transport_kwargs()) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        raw = resp.text
        _log_ingestion("weather/open-meteo", url, "OK", _hash_response(raw))

        # Parse current conditions
        current = data.get("current", {})
        daily = data.get("daily", {})

        # Weather code descriptions
        weather_descriptions = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy",
            3: "Overcast", 45: "Foggy", 48: "Rime fog",
            51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
            61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
            71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
            80: "Slight rain showers", 81: "Moderate rain showers",
            82: "Violent rain showers", 95: "Thunderstorm",
            96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
        }

        # Build 7-day forecast
        forecast_days = []
        if daily.get("time"):
            for i in range(len(daily["time"])):
                code = daily.get("weather_code", [0])[i] if i < len(daily.get("weather_code", [])) else 0
                forecast_days.append({
                    "date": daily["time"][i],
                    "day_label": datetime.strptime(daily["time"][i], "%Y-%m-%d").strftime("%a %b %d"),
                    "temp_max": daily.get("temperature_2m_max", [0])[i],
                    "temp_min": daily.get("temperature_2m_min", [0])[i],
                    "feels_like_max": daily.get("apparent_temperature_max", [0])[i],
                    "precipitation_mm": daily.get("precipitation_sum", [0])[i],
                    "precipitation_prob": daily.get("precipitation_probability_max", [0])[i],
                    "wind_max_kmh": daily.get("wind_speed_10m_max", [0])[i],
                    "weather_code": code,
                    "description": weather_descriptions.get(code, "Unknown"),
                })

        # Generate clinical risk flags
        clinical_flags = _generate_weather_clinical_flags(current, forecast_days)

        result = {
            "current": {
                "temperature": current.get("temperature_2m"),
                "feels_like": current.get("apparent_temperature"),
                "humidity": current.get("relative_humidity_2m"),
                "wind_speed_kmh": current.get("wind_speed_10m"),
                "pressure_hpa": current.get("surface_pressure"),
                "weather_code": current.get("weather_code", 0),
                "description": weather_descriptions.get(current.get("weather_code", 0), "Unknown"),
            },
            "forecast": forecast_days,
            "clinical_flags": clinical_flags,
            "location": {"lat": lat, "lon": lon},
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        _weather_cache = result
        _weather_cache_time = time.time()
        return result

    except Exception as e:
        _log_ingestion("weather/open-meteo", url, f"ERROR: {e}")
        # Return cached data if available, else empty
        if _weather_cache:
            return _weather_cache
        return {"current": {}, "forecast": [], "clinical_flags": [], "error": str(e)}


def _generate_weather_clinical_flags(current: dict, forecast: list) -> list:
    """Generate clinical risk flags based on weather conditions."""
    flags = []
    temp = current.get("temperature_2m", 25)
    humidity = current.get("relative_humidity_2m", 50)

    # Heat advisory
    if temp and temp >= 38:
        flags.append({
            "type": "HEAT_ADVISORY",
            "severity": "high" if temp >= 42 else "moderate",
            "icon": "🌡️",
            "title": f"Heat Advisory: {temp}°C",
            "message": f"Current temperature is {temp}°C. Elevated risk of dehydration, heat stroke, and cardiac events. Flag geriatric and cardiac patients for increased monitoring.",
            "affected_conditions": ["cardiac", "renal", "geriatric", "dehydration"],
            "recommended_actions": [
                "Increase IV fluid monitoring for at-risk patients",
                "Pre-position electrolyte solutions",
                "Increase rounding frequency on geriatric wards",
            ],
        })

    # Cold advisory
    if temp and temp <= 5:
        flags.append({
            "type": "COLD_ADVISORY",
            "severity": "moderate",
            "icon": "🥶",
            "title": f"Cold Advisory: {temp}°C",
            "message": f"Temperature is {temp}°C. Increased risk of hypothermia admissions and cardiac events from cold exposure.",
            "affected_conditions": ["cardiac", "respiratory", "homeless_patients"],
            "recommended_actions": [
                "Prepare for hypothermia admissions",
                "Check cardiac monitoring thresholds",
            ],
        })

    # Storm warning (check forecast for heavy rain/thunderstorm)
    for day in forecast[:3]:  # Next 3 days
        code = day.get("weather_code", 0)
        if code >= 80:  # Rain showers or thunderstorms
            flags.append({
                "type": "STORM_WARNING",
                "severity": "high" if code >= 95 else "moderate",
                "icon": "⛈️",
                "title": f"Storm Forecast: {day['day_label']}",
                "message": f"{day['description']} expected on {day['day_label']}. Precipitation: {day['precipitation_mm']}mm ({day['precipitation_prob']}% probability). Prepare for potential trauma surge.",
                "affected_conditions": ["trauma", "emergency"],
                "recommended_actions": [
                    "Alert ED for potential surge in trauma cases",
                    "Verify surgical schedule for weather-related delays",
                    "Check backup power systems",
                ],
            })
            break  # Only flag the first storm day

    # High humidity + heat = heat index danger
    if temp and humidity and temp >= 32 and humidity >= 70:
        flags.append({
            "type": "HEAT_INDEX_DANGER",
            "severity": "high",
            "icon": "💧",
            "title": f"Dangerous Heat Index: {temp}°C / {humidity}% humidity",
            "message": "Combination of high temperature and humidity creates dangerous heat index. Extreme risk for outdoor workers and elderly.",
            "affected_conditions": ["geriatric", "renal", "cardiac"],
            "recommended_actions": [
                "Flag all patients aged 65+ for dehydration screening",
                "Ensure adequate cooling in patient areas",
            ],
        })

    return flags


# ══════════════════════════════════════════════════════════════════════
# AIR QUALITY INDEX
# ══════════════════════════════════════════════════════════════════════

async def fetch_aqi(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON) -> dict:
    """
    Fetch Air Quality Index. Uses Open-Meteo Air Quality API (free, global).
    """
    global _aqi_cache, _aqi_cache_time

    if _aqi_cache and (time.time() - _aqi_cache_time) < AQI_CACHE_TTL:
        return _aqi_cache

    url = (
        f"https://air-quality-api.open-meteo.com/v1/air-quality?"
        f"latitude={lat}&longitude={lon}"
        f"&current=us_aqi,pm2_5,pm10,carbon_monoxide,nitrogen_dioxide,ozone"
        f"&timezone=auto"
    )

    try:
        async with httpx.AsyncClient(timeout=10, **_external_transport_kwargs()) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        _log_ingestion("aqi/open-meteo", url, "OK", _hash_response(resp.text))

        current = data.get("current", {})
        aqi_value = current.get("us_aqi", 0)

        # AQI categories
        if aqi_value <= 50:
            category, color = "Good", "green"
        elif aqi_value <= 100:
            category, color = "Moderate", "yellow"
        elif aqi_value <= 150:
            category, color = "Unhealthy for Sensitive Groups", "orange"
        elif aqi_value <= 200:
            category, color = "Unhealthy", "red"
        elif aqi_value <= 300:
            category, color = "Very Unhealthy", "purple"
        else:
            category, color = "Hazardous", "maroon"

        # Clinical flags
        clinical_flags = []
        if aqi_value > 100:
            clinical_flags.append({
                "type": "AQI_ALERT",
                "severity": "high" if aqi_value > 150 else "moderate",
                "icon": "🌫️",
                "title": f"Air Quality Alert: AQI {aqi_value} ({category})",
                "message": f"AQI is {aqi_value}. Patients with asthma, COPD, and respiratory conditions are at elevated risk.",
                "affected_conditions": ["asthma", "COPD", "respiratory", "cardiac"],
                "recommended_actions": [
                    "Notify respiratory therapy",
                    "Check inhaler availability for at-risk patients",
                    "Consider postponing outdoor patient activities",
                ],
            })

        result = {
            "aqi": aqi_value,
            "category": category,
            "color": color,
            "pollutants": {
                "pm2_5": current.get("pm2_5"),
                "pm10": current.get("pm10"),
                "co": current.get("carbon_monoxide"),
                "no2": current.get("nitrogen_dioxide"),
                "o3": current.get("ozone"),
            },
            "clinical_flags": clinical_flags,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        _aqi_cache = result
        _aqi_cache_time = time.time()
        return result

    except Exception as e:
        _log_ingestion("aqi/open-meteo", url, f"ERROR: {e}")
        if _aqi_cache:
            return _aqi_cache
        return {"aqi": None, "category": "Unavailable", "clinical_flags": [], "error": str(e)}


# ══════════════════════════════════════════════════════════════════════
# FDA DRUG RECALLS
# ══════════════════════════════════════════════════════════════════════

async def fetch_fda_recalls(limit: int = 10) -> list:
    """
    Fetch recent FDA drug recalls/enforcement actions.
    Uses openFDA API (free, no key required for low volume).
    """
    global _fda_cache, _fda_cache_time

    if _fda_cache and (time.time() - _fda_cache_time) < FDA_CACHE_TTL:
        return _fda_cache

    url = (
        f"https://api.fda.gov/drug/enforcement.json?"
        f"sort=report_date:desc&limit={limit}"
    )

    try:
        async with httpx.AsyncClient(timeout=10, **_external_transport_kwargs()) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        _log_ingestion("fda/drug-enforcement", url, "OK", _hash_response(resp.text))

        recalls = []
        for result in data.get("results", []):
            recalls.append({
                "recall_number": result.get("recall_number", ""),
                "product": result.get("product_description", "")[:200],
                "reason": result.get("reason_for_recall", "")[:200],
                "classification": result.get("classification", ""),
                "status": result.get("status", ""),
                "report_date": result.get("report_date", ""),
                "recall_initiation_date": result.get("recall_initiation_date", ""),
                "recalling_firm": result.get("recalling_firm", ""),
                "city": result.get("city", ""),
                "state": result.get("state", ""),
            })

        _fda_cache = recalls
        _fda_cache_time = time.time()
        return recalls

    except Exception as e:
        _log_ingestion("fda/drug-enforcement", url, f"ERROR: {e}")
        if _fda_cache:
            return _fda_cache
        return []


# ══════════════════════════════════════════════════════════════════════
# CDC FLU SURVEILLANCE (Simulated — CDC ILINet requires registration)
# ══════════════════════════════════════════════════════════════════════

async def fetch_cdc_flu_data() -> dict:
    """
    Fetch CDC influenza-like illness surveillance data.
    Uses CDC's open data API for ILI activity levels.
    Falls back to simulated seasonal data if API unavailable.
    """
    global _cdc_cache, _cdc_cache_time

    if _cdc_cache and (time.time() - _cdc_cache_time) < CDC_CACHE_TTL:
        return _cdc_cache

    # CDC WONDER / FluView API
    url = "https://data.cdc.gov/resource/ks2g-shsr.json?$limit=10&$order=year DESC,week DESC"

    try:
        async with httpx.AsyncClient(timeout=10, **_external_transport_kwargs()) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                _log_ingestion("cdc/fluview", url, "OK", _hash_response(resp.text))

                if data:
                    latest = data[0] if data else {}
                    result = {
                        "source": "CDC FluView",
                        "week": latest.get("week", ""),
                        "year": latest.get("year", ""),
                        "ili_percentage": float(latest.get("weighted_ili", 0)),
                        "total_patients": int(latest.get("total_patients", 0)),
                        "activity_level": _classify_ili_level(float(latest.get("weighted_ili", 0))),
                        "clinical_flags": [],
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }

                    # Generate clinical flag if activity is elevated
                    if result["ili_percentage"] > 3.0:
                        result["clinical_flags"].append({
                            "type": "FLU_SURGE",
                            "severity": "high" if result["ili_percentage"] > 5.0 else "moderate",
                            "icon": "🦠",
                            "title": f"Influenza Surge: ILI at {result['ili_percentage']}%",
                            "message": f"CDC reports influenza-like illness at {result['ili_percentage']}% nationally (Week {result['week']}). Prepare for increased respiratory admissions.",
                            "affected_conditions": ["respiratory", "pediatric", "geriatric"],
                            "recommended_actions": [
                                "Activate respiratory isolation protocols for new admissions",
                                "Verify oseltamivir (Tamiflu) stock levels",
                                "Increase ED triage screening for ILI symptoms",
                            ],
                        })

                    _cdc_cache = result
                    _cdc_cache_time = time.time()
                    return result
            else:
                _log_ingestion("cdc/fluview", url, f"HTTP {resp.status_code}")

    except Exception as e:
        _log_ingestion("cdc/fluview", url, f"ERROR: {e}")

    # Fallback: seasonal estimate based on current month
    result = _seasonal_flu_estimate()
    _cdc_cache = result
    _cdc_cache_time = time.time()
    return result


def _classify_ili_level(ili_pct: float) -> str:
    if ili_pct < 1.5:
        return "minimal"
    elif ili_pct < 3.0:
        return "low"
    elif ili_pct < 5.0:
        return "moderate"
    elif ili_pct < 7.0:
        return "high"
    else:
        return "very_high"


def _seasonal_flu_estimate() -> dict:
    """Generate seasonal flu estimate when CDC API is unavailable."""
    month = datetime.now().month
    # Flu season peaks Dec–Feb in Northern Hemisphere
    seasonal_ili = {
        1: 4.5, 2: 5.2, 3: 3.8, 4: 2.1, 5: 1.2, 6: 0.8,
        7: 0.5, 8: 0.6, 9: 0.9, 10: 1.5, 11: 2.5, 12: 3.9,
    }
    ili = seasonal_ili.get(month, 1.0)
    return {
        "source": "Seasonal Estimate (CDC API unavailable)",
        "week": datetime.now().strftime("%U"),
        "year": str(datetime.now().year),
        "ili_percentage": ili,
        "total_patients": 0,
        "activity_level": _classify_ili_level(ili),
        "clinical_flags": [],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════
# COMBINED PULSE DASHBOARD
# ══════════════════════════════════════════════════════════════════════

async def get_pulse_dashboard(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON) -> dict:
    """
    Aggregate all environmental intelligence into a single dashboard payload.
    This is the primary endpoint for the frontend Pulse widget.
    """
    # Fetch all feeds concurrently
    weather, aqi, cdc, fda = await asyncio.gather(
        fetch_weather(lat, lon),
        fetch_aqi(lat, lon),
        fetch_cdc_flu_data(),
        fetch_fda_recalls(5),
        return_exceptions=True,
    )

    # Handle any that errored
    if isinstance(weather, Exception):
        weather = {"current": {}, "forecast": [], "clinical_flags": [], "error": str(weather)}
    if isinstance(aqi, Exception):
        aqi = {"aqi": None, "category": "Unavailable", "clinical_flags": [], "error": str(aqi)}
    if isinstance(cdc, Exception):
        cdc = {"activity_level": "unknown", "clinical_flags": [], "error": str(cdc)}
    if isinstance(fda, Exception):
        fda = []

    # Collect all clinical flags
    all_flags = []
    all_flags.extend(weather.get("clinical_flags", []))
    all_flags.extend(aqi.get("clinical_flags", []))
    all_flags.extend(cdc.get("clinical_flags", []) if isinstance(cdc, dict) else [])

    # Sort by severity
    severity_order = {"high": 0, "moderate": 1, "low": 2}
    all_flags.sort(key=lambda f: severity_order.get(f.get("severity", "low"), 2))

    return {
        "weather": weather,
        "air_quality": aqi,
        "flu_surveillance": cdc,
        "drug_recalls": fda,
        "active_alerts": all_flags,
        "alert_count": len(all_flags),
        "last_refresh": datetime.now(timezone.utc).isoformat(),
        "data_direction": "INBOUND_ONLY, zero patient data transmitted",
    }


def get_ingestion_audit_log(limit: int = 50) -> list:
    """Return the audit log of external data pulls, newest last, from the
    audit chain (survives a restart). SYNC: the route is a plain def."""
    from database import db_session

    with db_session() as db:
        rows = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE action_type = ? "
            "ORDER BY id DESC LIMIT ?",
            (EGRESS_AUDIT_ACTION, int(limit)),
        ).fetchall()
    out = []
    for r in reversed(rows):
        try:
            out.append(json.loads(r["metadata_json"] or "{}"))
        except (TypeError, ValueError):
            continue
    return out
