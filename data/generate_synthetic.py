"""Synthetic generator matching the v2 production schema.

Reads pipeline/cities.csv to stay in sync with the city list, generates
hourly forecast samples and daily-mean-vs-climatology fields, writes to
data/cities_live.json.
"""
import json
import math
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

random.seed(7)

REPO_ROOT = Path(__file__).resolve().parent.parent
CITIES_CSV = REPO_ROOT / "pipeline" / "cities.csv"
OUTPUT_JSON = REPO_ROOT / "data" / "cities_live.json"


def fake_temp(lat, lon, doy, hour_local):
    """Plausible temperature based on lat, season, and local hour-of-day."""
    seasonal = math.cos(2 * math.pi * (doy - 172) / 365.25)
    seasonal_amp = 18 * (abs(lat) / 90) ** 0.7
    base = 30 - 0.6 * abs(lat)
    hemi = 1 if lat >= 0 else -1
    base_temp = base + hemi * seasonal * seasonal_amp
    # Diurnal cycle (peaks at ~14:00 local)
    diurnal = -math.cos(2 * math.pi * (hour_local - 14) / 24)
    diurnal_amp = max(2, 8 - abs(lat) * 0.05)  # smaller in tropics
    base_temp += diurnal * diurnal_amp
    base_temp += random.gauss(0, 2)
    return base_temp


def main():
    cities = pd.read_csv(CITIES_CSV)
    now = datetime.now(timezone.utc).replace(microsecond=0, second=0, minute=0)
    doy = now.timetuple().tm_yday

    # Generate ~21 hourly samples (-2h to +18h around now)
    sample_times = [now + timedelta(hours=h) for h in range(-2, 19)]

    records = []
    for _, row in cities.iterrows():
        try:
            tz = ZoneInfo(row["tz"])
        except Exception:
            continue

        # Local-day window
        local_now = now.astimezone(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = local_midnight.astimezone(timezone.utc)
        day_end_utc = (local_midnight + timedelta(days=1)).astimezone(timezone.utc)
        local_doy = local_midnight.timetuple().tm_yday

        # Generate hourly temps spanning a 36h window so we can extract today
        local_doy_for_temp = local_doy
        wide_times = [day_start_utc + timedelta(hours=h) for h in range(0, 24)]
        wide_temps = []
        for t in wide_times:
            t_local = t.astimezone(tz)
            wide_temps.append(fake_temp(row.lat, row.lon, t_local.timetuple().tm_yday, t_local.hour))

        daily_max = max(wide_temps)
        daily_min = min(wide_temps)
        daily_mean = sum(wide_temps) / len(wide_temps)

        # Hourly emit window (the +/- now slice)
        hourly = []
        for t in sample_times:
            t_local = t.astimezone(tz)
            temp = fake_temp(row.lat, row.lon, t_local.timetuple().tm_yday, t_local.hour)
            hourly.append({
                "utc": t.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "temp_c": round(temp, 1),
            })

        # Synthetic climatology
        seasonal = math.cos(2 * math.pi * (local_doy - 172) / 365.25)
        seasonal_amp = 18 * (abs(row.lat) / 90) ** 0.7
        base = 30 - 0.6 * abs(row.lat)
        hemi = 1 if row.lat >= 0 else -1
        clim_mean = round(base + hemi * seasonal * seasonal_amp, 1)
        clim_std = round(2.5 + 1.5 * (abs(row.lat) / 90), 2)
        clim_p95 = round(clim_mean + 1.645 * clim_std, 1)

        anomaly = round(daily_mean - clim_mean, 1)
        z = anomaly / clim_std
        pct = round(100.0 * 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))), 1)

        records.append({
            "geonames_id": int(row["geonames_id"]),
            "name": row["name"],
            "country": row["country"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "tz": row["tz"],
            "local_doy": local_doy,
            "local_day_start_utc": day_start_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "daily_max_c": round(daily_max, 1),
            "daily_min_c": round(daily_min, 1),
            "daily_mean_c": round(daily_mean, 1),
            "hourly_samples": hourly,
            "climatology_mean_c": clim_mean,
            "climatology_p95_c": clim_p95,
            "climatology_std_c": clim_std,
            "daily_anomaly_c": anomaly,
            "daily_mean_percentile": pct,
        })

    out = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "model_run": None,
            "cycles_used": [],
            "climatology_version": "synthetic-v2",
            "grid_resolution_deg": 0.25,
            "synthetic": True,
            "n_cities": len(records),
            "hourly_window_start_utc": sample_times[0].replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "hourly_window_end_utc": sample_times[-1].replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        },
        "cities": records,
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(records)} cities to {OUTPUT_JSON} ({OUTPUT_JSON.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
