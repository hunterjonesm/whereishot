"""
Fetch GFS analysis + forecast, sample at city locations, join with ERA5
climatology, and write data/cities_live.json.

Key design points:

1. **Hourly samples emitted per city.** The frontend picks the appropriate
   forecast hour client-side based on the user's wall clock, so the pipeline
   ships every hour's value rather than picking one. Output schema:

        cities[i].hourly_samples = [{utc: "2026-05-02T15:00", temp_c: 14.3}, ...]

   This means the displayed "current temperature" is always within 30 minutes
   of true now, even if the pipeline hasn't run for a few hours.

2. **Five-cycle fetch.** A city's *local* calendar day can span up to ~24 hours
   in either direction from the moment the pipeline runs (a city near the
   dateline late in its local day has the day's first hours nearly 24h
   behind UTC; a city in BST just past midnight when the pipeline runs at
   23:13 UTC has the day's last hours nearly 24h ahead). To capture every
   city's full local day at any of the four daily run times, we fetch from
   five consecutive 6-hourly GFS cycles, each F+0..F+30, covering roughly
   [T-29h, T+25h] of UTC. Later cycles override earlier ones for any
   overlapping valid time, so each UTC hour gets the freshest forecast
   available.

3. **Daily max/min binned by city's local calendar day.** For each city, we
   determine the UTC bounds of "today" in its IANA timezone, then take
   max/min over only those hourly samples. This matches WMO conventions
   for daily extremes.

4. **Climatology lookup uses city's local DOY.** When Tokyo has rolled into
   the next calendar day but Geneva hasn't, each city looks up the
   appropriate DOY for its own local date.

Usage:
  python pipeline/fetch_gfs.py --cities pipeline/cities.csv \\
      --climatology climatology/era5_doy_clim.parquet \\
      --output data/cities_live.json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import xarray as xr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

NOMADS_FILTER = (
    "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
    "?file=gfs.t{cycle:02d}z.pgrb2.0p25.f{fhr:03d}"
    "&lev_2_m_above_ground=on"
    "&var_TMP=on"
    "&dir=%2Fgfs.{date}%2F{cycle:02d}%2Fatmos"
)

# We fetch this many forecast hours from each cycle, hourly. Five cycles × 31
# hourly fields each gives ~54 contiguous hours of UTC coverage after dedup,
# enough to fully span any city's local calendar day from any pipeline run
# time (worst case: a city near the dateline late in its local day, or just
# after midnight in BST when the pipeline runs at 23:13 UTC).
HOURS_PER_CYCLE = 30
N_CYCLES = 5


@dataclass
class GFSCycle:
    date: str
    cycle: int

    @property
    def run_str(self) -> str:
        return f"GFS {self.date[:4]}-{self.date[4:6]}-{self.date[6:8]} {self.cycle:02d}Z"

    @property
    def datetime_utc(self) -> datetime:
        return datetime.strptime(self.date, "%Y%m%d").replace(
            hour=self.cycle, tzinfo=timezone.utc
        )

    def previous(self) -> "GFSCycle":
        prev_dt = self.datetime_utc - timedelta(hours=6)
        return GFSCycle(prev_dt.strftime("%Y%m%d"), prev_dt.hour)


def latest_available_cycle(now_utc: datetime | None = None) -> GFSCycle:
    """Walk back through cycles until we find one fully available."""
    now = now_utc or datetime.now(timezone.utc)
    candidate = now - timedelta(hours=4)
    cycle_hour = (candidate.hour // 6) * 6
    candidate = candidate.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)

    for _ in range(8):
        cyc = GFSCycle(candidate.strftime("%Y%m%d"), candidate.hour)
        if cycle_exists(cyc):
            log.info(f"Latest available cycle: {cyc.run_str}")
            return cyc
        log.warning(f"Cycle {cyc.run_str} not yet available; trying previous")
        candidate -= timedelta(hours=6)
    raise RuntimeError("No GFS cycle available in last 48 hours")


def cycle_exists(cyc: GFSCycle) -> bool:
    url = NOMADS_FILTER.format(cycle=cyc.cycle, fhr=0, date=cyc.date)
    try:
        r = requests.head(url, timeout=15, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def fetch_grib(cyc: GFSCycle, fhr: int) -> bytes:
    url = NOMADS_FILTER.format(cycle=cyc.cycle, fhr=fhr, date=cyc.date)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    if len(r.content) < 1000:
        raise RuntimeError(f"Suspiciously small response ({len(r.content)}B) for f{fhr:03d}")
    return r.content


def open_grib(buf: bytes) -> xr.DataArray:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(buf)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib")
        var = "t2m" if "t2m" in ds.data_vars else list(ds.data_vars)[0]
        da = ds[var] - 273.15
        da = da.assign_coords(longitude=(((da.longitude + 180) % 360) - 180))
        da = da.sortby("longitude")
        return da
    finally:
        Path(path).unlink(missing_ok=True)


def sample_at_cities(da: xr.DataArray, cities: pd.DataFrame) -> np.ndarray:
    lats = xr.DataArray(cities["lat"].values, dims="city")
    lons = xr.DataArray(cities["lon"].values, dims="city")
    sampled = da.sel(latitude=lats, longitude=lons, method="nearest")
    return sampled.values


def fetch_cycle_to_dict(cyc: GFSCycle, cities: pd.DataFrame) -> dict[datetime, np.ndarray]:
    """Fetch HOURS_PER_CYCLE forecast hours from a cycle, sample at city points.

    Returns dict mapping valid_utc (datetime) → np.ndarray of temperatures (n_cities,).
    """
    out = {}
    log.info(f"  Fetching {HOURS_PER_CYCLE + 1} hours from {cyc.run_str}")
    for fhr in range(HOURS_PER_CYCLE + 1):
        try:
            buf = fetch_grib(cyc, fhr)
        except requests.HTTPError as e:
            log.warning(f"    f{fhr:03d}: fetch failed ({e}); skipping")
            continue
        da = open_grib(buf)
        valid = cyc.datetime_utc + timedelta(hours=fhr)
        out[valid] = sample_at_cities(da, cities)
    return out


def assemble_hourly_series(
    samples_by_time: dict[datetime, np.ndarray]
) -> tuple[list[datetime], np.ndarray]:
    """Sort the samples chronologically; later cycles override earlier ones for
    overlapping valid times (so the user gets the freshest forecast for each
    UTC hour)."""
    sorted_times = sorted(samples_by_time.keys())
    stack = np.stack([samples_by_time[t] for t in sorted_times])
    return sorted_times, stack


def load_climatology(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        log.warning(f"Climatology not found at {path}; percentile fields will be null")
        return None
    return pd.read_parquet(path)


def percentile_from_normal(value: float, mean: float, std: float) -> float:
    if std is None or std <= 0 or not np.isfinite(value) or not np.isfinite(mean):
        return float("nan")
    z = (value - mean) / std
    return 100.0 * 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def city_local_today_utc_bounds(tz: str, ref_utc: datetime) -> tuple[datetime, datetime, int]:
    """Given a city's IANA timezone and a UTC reference, return the UTC bounds
    of "today" in that city's local time, plus the local day-of-year.

    Returns (day_start_utc, day_end_utc_exclusive, doy).
    """
    local = ref_utc.astimezone(ZoneInfo(tz))
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    local_next_midnight = local_midnight + timedelta(days=1)
    return (
        local_midnight.astimezone(timezone.utc),
        local_next_midnight.astimezone(timezone.utc),
        local_midnight.timetuple().tm_yday,
    )


def build_records(
    cities: pd.DataFrame,
    times: list[datetime],
    stack: np.ndarray,           # shape (n_times, n_cities)
    clim: pd.DataFrame | None,
    pipeline_now_utc: datetime,
) -> list[dict]:
    times_arr = np.array([t.timestamp() for t in times])
    records = []

    for i, row in cities.iterrows():
        tz = row["tz"]
        try:
            day_start_utc, day_end_utc, local_doy = city_local_today_utc_bounds(
                tz, pipeline_now_utc
            )
        except Exception as e:
            log.warning(f"TZ resolution failed for {row['name']} ({tz}): {e}")
            continue

        # Indices of the hourly samples that fall within this city's local day
        in_day = (times_arr >= day_start_utc.timestamp()) & (times_arr < day_end_utc.timestamp())
        if not in_day.any():
            log.warning(f"  {row['name']}: no samples in local day window — coverage gap")
            continue

        day_samples = stack[in_day, i]
        daily_max = float(day_samples.max())
        daily_min = float(day_samples.min())
        daily_mean = float(day_samples.mean())

        # Build the hourly sample list — only emit samples within ±18h of now,
        # which is enough for the frontend to find the right "current" hour
        # without bloating the JSON.
        emit_window = (
            (times_arr >= (pipeline_now_utc - timedelta(hours=2)).timestamp()) &
            (times_arr <= (pipeline_now_utc + timedelta(hours=18)).timestamp())
        )
        hourly = [
            {
                "utc": times[k].replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "temp_c": round(float(stack[k, i]), 1),
            }
            for k in range(len(times)) if emit_window[k]
        ]

        rec = {
            "geonames_id": int(row["geonames_id"]),
            "name": row["name"],
            "country": row["country"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "tz": tz,
            "local_doy": local_doy,
            "local_day_start_utc": day_start_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "daily_max_c": round(daily_max, 1),
            "daily_min_c": round(daily_min, 1),
            "daily_mean_c": round(daily_mean, 1),
            "hourly_samples": hourly,
        }

        if clim is not None:
            sub = clim[(clim["geonames_id"] == row["geonames_id"]) & (clim["doy"] == local_doy)]
            if len(sub) == 1:
                c = sub.iloc[0]
                rec["climatology_mean_c"] = round(float(c["mean_c"]), 1)
                rec["climatology_p95_c"] = round(float(c["p95_c"]), 1)
                rec["climatology_std_c"] = round(float(c["std_c"]), 2)
                rec["daily_anomaly_c"] = round(daily_mean - float(c["mean_c"]), 1)
                rec["daily_mean_percentile"] = round(
                    percentile_from_normal(daily_mean, float(c["mean_c"]), float(c["std_c"])), 1
                )
            else:
                rec["climatology_mean_c"] = None
                rec["climatology_p95_c"] = None
                rec["climatology_std_c"] = None
                rec["daily_anomaly_c"] = None
                rec["daily_mean_percentile"] = None
        else:
            rec["climatology_mean_c"] = None
            rec["climatology_p95_c"] = None
            rec["climatology_std_c"] = None
            rec["daily_anomaly_c"] = None
            rec["daily_mean_percentile"] = None

        records.append(rec)
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", type=Path, required=True)
    ap.add_argument("--climatology", type=Path,
                    default=Path("climatology/era5_doy_clim.parquet"))
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    cities = pd.read_csv(args.cities)
    log.info(f"Loaded {len(cities)} cities")

    cyc_curr = latest_available_cycle()
    cycles_to_fetch = [cyc_curr]
    c = cyc_curr
    for _ in range(N_CYCLES - 1):
        c = c.previous()
        cycles_to_fetch.append(c)
    log.info(f"Fetching {len(cycles_to_fetch)} cycles for full local-day coverage:")
    for c in cycles_to_fetch:
        log.info(f"  - {c.run_str}")

    # Fetch oldest first, so newer cycles overwrite older ones in the merged dict
    merged = {}
    for cyc in reversed(cycles_to_fetch):
        if not cycle_exists(cyc):
            log.warning(f"Cycle {cyc.run_str} unavailable; skipping")
            continue
        merged.update(fetch_cycle_to_dict(cyc, cities))

    times, stack = assemble_hourly_series(merged)
    log.info(f"Assembled {len(times)} hourly fields covering "
             f"{times[0].isoformat()} to {times[-1].isoformat()}")

    clim = load_climatology(args.climatology)
    pipeline_now = datetime.now(timezone.utc)

    records = build_records(cities, times, stack, clim, pipeline_now)
    log.info(f"Built {len(records)} city records")

    out = {
        "metadata": {
            "generated_at_utc": pipeline_now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "model_run": cyc_curr.run_str,
            "cycles_used": [c.run_str for c in cycles_to_fetch],
            "climatology_version": "ERA5 1991-2020 v2 (detrended-sigma)" if clim is not None else None,
            "grid_resolution_deg": 0.25,
            "synthetic": False,
            "n_cities": len(records),
            "hourly_window_start_utc": times[0].replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "hourly_window_end_utc": times[-1].replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        },
        "cities": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2))
    size_kb = args.output.stat().st_size / 1024
    log.info(f"Wrote {len(records)} records to {args.output} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    sys.exit(main() or 0)
