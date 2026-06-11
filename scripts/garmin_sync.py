#!/usr/bin/env python3
"""Fetch today's Garmin health stats and upsert them into Supabase.

Required Supabase columns (run once in the SQL editor):
    ALTER TABLE garmin_daily
      ADD COLUMN IF NOT EXISTS deep_sleep_seconds  INTEGER,
      ADD COLUMN IF NOT EXISTS light_sleep_seconds INTEGER,
      ADD COLUMN IF NOT EXISTS rem_sleep_seconds   INTEGER,
      ADD COLUMN IF NOT EXISTS awake_seconds       INTEGER,
      ADD COLUMN IF NOT EXISTS sleep_stress        INTEGER,
      ADD COLUMN IF NOT EXISTS spo2_avg            NUMERIC(4,1),
      ADD COLUMN IF NOT EXISTS breathing_rate      NUMERIC(4,1);
"""

import os
import sys
import logging
from datetime import date, datetime, timezone

from garminconnect import Garmin, GarminConnectAuthenticationError
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def _get_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        log.error("Missing required environment variable: %s", key)
        sys.exit(1)
    return value


def _safe_int(value) -> "int | None":
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_float(value, ndigits: int = 1) -> "float | None":
    try:
        return round(float(value), ndigits) if value is not None else None
    except (TypeError, ValueError):
        return None


def fetch_garmin(email: str, password: str, today: str) -> dict:
    log.info("Logging into Garmin Connect as %s", email)
    client = Garmin(email, password)
    client.login()

    log.info("Fetching daily stats for %s", today)
    stats = client.get_stats(today)

    # Body Battery: peak charged value for the day (used as recovery score)
    body_battery = _safe_int(
        stats.get("bodyBatteryChargedValue")
        or stats.get("bodyBatteryMostRecentValue")
    )

    # Average Stress: 0–100 scale (used as strain proxy)
    avg_stress = _safe_int(stats.get("averageStressLevel"))

    # Resting Heart Rate
    resting_hr = _safe_int(stats.get("restingHeartRate"))

    log.info("Fetching sleep data for %s", today)
    sleep_score:         "int | None"   = None
    sleep_seconds:       "int | None"   = None
    deep_sleep_seconds:  "int | None"   = None
    light_sleep_seconds: "int | None"   = None
    rem_sleep_seconds:   "int | None"   = None
    awake_seconds:       "int | None"   = None
    sleep_stress:        "int | None"   = None
    spo2_avg:            "float | None" = None
    breathing_rate:      "float | None" = None

    try:
        sleep_data = client.get_sleep_data(today)
        dto = (sleep_data or {}).get("dailySleepDTO") or {}

        # Duration & stages
        sleep_seconds       = _safe_int(dto.get("sleepTimeSeconds"))
        deep_sleep_seconds  = _safe_int(dto.get("deepSleepSeconds"))
        light_sleep_seconds = _safe_int(dto.get("lightSleepSeconds"))
        rem_sleep_seconds   = _safe_int(dto.get("remSleepSeconds"))
        awake_seconds       = _safe_int(dto.get("awakeSleepSeconds"))

        # Sleep stress (Garmin 0–100, lower = calmer)
        sleep_stress = _safe_int(dto.get("avgSleepStress"))

        # Blood oxygen
        spo2_avg = _safe_float(dto.get("averageSpO2Value"))

        # Breathing rate (breaths per minute)
        breathing_rate = _safe_float(dto.get("averageRespirationValue"))

        # Sleep score — path differs across Garmin firmware versions
        scores = dto.get("sleepScores") or {}
        if isinstance(scores, dict):
            overall = scores.get("overall")
            if isinstance(overall, dict):
                sleep_score = _safe_int(overall.get("value"))
            else:
                sleep_score = _safe_int(overall)
        if sleep_score is None:
            sleep_score = _safe_int(dto.get("sleepScore"))

    except Exception as exc:
        log.warning("Could not parse sleep data: %s", exc)

    return {
        "body_battery":        body_battery,
        "avg_stress":          avg_stress,
        "resting_hr":          resting_hr,
        "sleep_score":         sleep_score,
        "sleep_seconds":       sleep_seconds,
        "deep_sleep_seconds":  deep_sleep_seconds,
        "light_sleep_seconds": light_sleep_seconds,
        "rem_sleep_seconds":   rem_sleep_seconds,
        "awake_seconds":       awake_seconds,
        "sleep_stress":        sleep_stress,
        "spo2_avg":            spo2_avg,
        "breathing_rate":      breathing_rate,
    }


def main() -> None:
    garmin_email    = _get_env("GARMIN_EMAIL")
    garmin_password = _get_env("GARMIN_PASSWORD")
    supabase_url    = _get_env("SUPABASE_URL")
    supabase_key    = _get_env("SUPABASE_KEY")

    today = date.today().isoformat()

    try:
        garmin_row = fetch_garmin(garmin_email, garmin_password, today)
    except GarminConnectAuthenticationError as exc:
        log.error("Garmin authentication failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Garmin fetch failed: %s", exc)
        sys.exit(1)

    log.info(
        "body_battery=%s  avg_stress=%s  resting_hr=%s  "
        "sleep_score=%s  sleep_seconds=%s  "
        "deep=%s  light=%s  rem=%s  awake=%s  "
        "sleep_stress=%s  spo2=%s  breathing=%s",
        garmin_row["body_battery"],
        garmin_row["avg_stress"],
        garmin_row["resting_hr"],
        garmin_row["sleep_score"],
        garmin_row["sleep_seconds"],
        garmin_row["deep_sleep_seconds"],
        garmin_row["light_sleep_seconds"],
        garmin_row["rem_sleep_seconds"],
        garmin_row["awake_seconds"],
        garmin_row["sleep_stress"],
        garmin_row["spo2_avg"],
        garmin_row["breathing_rate"],
    )

    # Filter out None values so existing columns that aren't present yet are skipped
    row = {
        "date": today,
        **{k: v for k, v in garmin_row.items() if v is not None},
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }

    log.info("Upserting into Supabase table 'garmin_daily'...")
    sb = create_client(supabase_url, supabase_key)
    result = sb.table("garmin_daily").upsert(row, on_conflict="date").execute()
    log.info("Done — row: %s", result.data)


if __name__ == "__main__":
    main()
