"""
Fetch the latest GFS analysis + forecast, sample at city locations, join with
ERA5 climatology, and write data/cities_live.json.

Pipeline steps:
  1. Determine the most recent available GFS cycle (00/06/12/18Z).
  2. Pick a "nowcast" forecast hour whose valid time is closest to clock-now.
     This is what gets reported as `current_temp_c` — it answers
     "what is the model's best estimate for right now," not "what was the
     analysis at the cycle's start." For a 00Z run viewed at 08:30 UTC,
     nowcast_fhr = 9, valid 09:00 UTC.
  3. Build a 24-hour forward window starting at the nowcast hour, sampled
     hourly. From this we derive `daily_max_c` and `daily_min_c` — the
     warmest/coolest moment in the next 24 hours, including now.
  4. Download just the bytes we need from NOAA NOMADS using the GRIB filter
     (~200 KB per file × ~25 files = ~5 MB total).
  5. Open as xarray, sample at city points, join with ERA5 climatology to
     compute anomaly + percentile.
  6. Write JSON with full provenance metadata.

Usage:
  python pipeline/fetch_gfs.py --cities pipeline/cities.csv \
      --climatology climatology/era5_doy_clim.parquet \
      --output data/cities_live.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# How far in the future the nowcast window extends. 24 hours = next-day max/min.
WINDOW_HOURS = 24


@dataclass
class GFSCycle:
    date: str          # YYYYMMDD
    cycle: int         # 0, 6, 12, or 18

    @property
    def run_str(self) -> str:
        return f"GFS {self.date[:4]}-{self.date[4:6]}-{self.date[6:8]} {self.cycle:02d}Z"

    @property
    def datetime_utc(self) -> datetime:
        return datetime.strptime(self.date, "%Y%m%d").replace(
            hour=self.cycle, tzinfo=timezone.utc
        )


def latest_available_cycle(now_utc: datetime | None = None) -> GFSCycle:
    """GFS cycles run at 00/06/12/18Z and become available ~3.5-5 hours later.

    We assume a 4-hour minimum lag for safety, then walk back through cycles
    until we find one that exists on NOMADS.
    """
    now = now_utc or datetime.now(timezone.utc)
    candidate = now - timedelta(hours=4)
    cycle_hour = (candidate.hour // 6) * 6
    candidate = candidate.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)

    for _ in range(8):  # Try last 8 cycles (= 48 hours)
        cyc = GFSCycle(candidate.strftime("%Y%m%d"), candidate.hour)
        if cycle_exists(cyc):
            log.info(f"Using cycle: {cyc.run_str}")
            return cyc
        log.warning(f"Cycle {cyc.run_str} not yet available; trying previous")
        candidate -= timedelta(hours=6)
    raise RuntimeError("No GFS cycle available in last 48 hours — NOAA outage?")


def cycle_exists(cyc: GFSCycle) -> bool:
    """HEAD-check the F000 file for the given cycle."""
    url = NOMADS_FILTER.format(cycle=cyc.cycle, fhr=0, date=cyc.date)
    try:
        r = requests.head(url, timeout=15, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def pick_forecast_hours(cyc: GFSCycle, now_utc: datetime | None = None) -> tuple[int, list[int]]:
    """Decide which forecast hours to fetch.

    Returns (nowcast_fhr, all_fhrs_to_fetch).

    nowcast_fhr is the cycle-relative forecast hour whose valid time is
    nearest to right now — used as `current_temp_c`.

    all_fhrs_to_fetch covers nowcast_fhr through nowcast_fhr+WINDOW_HOURS,
    hourly, capped at 120 (where GFS hourly data ends).
    """
    now = now_utc or datetime.now(timezone.utc)
    hours_since_cycle = (now - cyc.datetime_utc).total_seconds() / 3600
    # Use floor(x + 0.5) for round-half-up; Python's built-in round() does
    # banker's rounding which gives F008 instead of F009 at 8h30m past cycle.
    import math
    nowcast_fhr = max(0, min(120, int(math.floor(hours_since_cycle + 0.5))))
    end_fhr = min(120, nowcast_fhr + WINDOW_HOURS)
    fhrs = list(range(nowcast_fhr, end_fhr + 1))
    return nowcast_fhr, fhrs


def fetch_grib(cyc: GFSCycle, fhr: int) -> bytes:
    """Download a single forecast hour as raw GRIB2 bytes."""
    url = NOMADS_FILTER.format(cycle=cyc.cycle, fhr=fhr, date=cyc.date)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    if len(r.content) < 1000:
        raise RuntimeError(f"Suspiciously small response ({len(r.content)}B) for f{fhr:03d}")
    return r.content


def open_grib(buf: bytes) -> xr.DataArray:
    """Open a GRIB2 byte buffer as an xarray DataArray of 2m temperature in °C."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(buf)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib")
        var = "t2m" if "t2m" in ds.data_vars else list(ds.data_vars)[0]
        da = ds[var] - 273.15
        # GFS longitude is 0..360; convert to -180..180
        da = da.assign_coords(longitude=(((da.longitude + 180) % 360) - 180))
        da = da.sortby("longitude")
        return da
    finally:
        Path(path).unlink(missing_ok=True)


def sample_at_cities(da: xr.DataArray, cities: pd.DataFrame) -> np.ndarray:
    """Nearest-neighbour sample of a 2D grid at a list of (lat, lon) points."""
    lats = xr.DataArray(cities["lat"].values, dims="city")
    lons = xr.DataArray(cities["lon"].values, dims="city")
    sampled = da.sel(latitude=lats, longitude=lons, method="nearest")
    return sampled.values


def load_climatology(path: Path) -> pd.DataFrame | None:
    """Load precomputed ERA5 day-of-year climatology, sampled at city points."""
    if not path.exists():
        log.warning(f"Climatology file not found at {path}; percentiles will be null")
        return None
    return pd.read_parquet(path)


def percentile_from_normal(value: float, mean: float, std: float) -> float:
    """Approximate percentile assuming a Gaussian climatology distribution."""
    if std is None or std <= 0:
        return float("nan")
    from math import erf, sqrt
    z = (value - mean) / std
    return 100.0 * 0.5 * (1.0 + erf(z / sqrt(2.0)))


def build_records(
    cities: pd.DataFrame,
    current: np.ndarray,
    daily_max: np.ndarray,
    daily_min: np.ndarray,
    clim: pd.DataFrame | None,
    doy: int,
) -> list[dict]:
    records = []
    if clim is not None:
        clim_today = clim[clim["doy"] == doy].set_index("geonames_id")
    else:
        clim_today = None
    for i, row in cities.iterrows():
        rec = {
            "geonames_id": int(row["geonames_id"]),
            "name": row["name"],
            "country": row["country"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "tz": row["tz"],
            "current_temp_c": round(float(current[i]), 1),
            "daily_max_c": round(float(daily_max[i]), 1),
            "daily_min_c": round(float(daily_min[i]), 1),
        }
        if clim_today is not None and row["geonames_id"] in clim_today.index:
            c = clim_today.loc[row["geonames_id"]]
            rec["climatology_mean_c"] = round(float(c["mean_c"]), 1)
            rec["climatology_p95_c"] = round(float(c["p95_c"]), 1)
            rec["anomaly_c"] = round(rec["current_temp_c"] - rec["climatology_mean_c"], 1)
            rec["percentile"] = round(percentile_from_normal(
                rec["current_temp_c"], c["mean_c"], c["std_c"]
            ), 1)
        else:
            rec["climatology_mean_c"] = None
            rec["climatology_p95_c"] = None
            rec["anomaly_c"] = None
            rec["percentile"] = None
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

    cyc = latest_available_cycle()
    nowcast_fhr, fhrs_to_fetch = pick_forecast_hours(cyc)
    nowcast_valid = cyc.datetime_utc + timedelta(hours=nowcast_fhr)
    log.info(f"Nowcast: F{nowcast_fhr:03d} valid {nowcast_valid.isoformat()}")
    log.info(f"Window: F{fhrs_to_fetch[0]:03d}..F{fhrs_to_fetch[-1]:03d} "
             f"({len(fhrs_to_fetch)} hours, hourly)")

    log.info(f"Fetching {len(fhrs_to_fetch)} GRIB files from NOMADS...")
    samples_by_fhr = {}
    for fhr in fhrs_to_fetch:
        buf = fetch_grib(cyc, fhr)
        da = open_grib(buf)
        samples_by_fhr[fhr] = sample_at_cities(da, cities)
        log.info(f"  F{fhr:03d}: range "
                 f"[{float(da.min()):.1f}, {float(da.max()):.1f}]°C")

    # Stack: shape (n_hours, n_cities)
    sample_stack = np.stack([samples_by_fhr[fhr] for fhr in fhrs_to_fetch])

    current = samples_by_fhr[nowcast_fhr]
    daily_max = sample_stack.max(axis=0)
    daily_min = sample_stack.min(axis=0)

    clim = load_climatology(args.climatology)
    doy = nowcast_valid.timetuple().tm_yday

    records = build_records(cities, current, daily_max, daily_min, clim, doy)
    out = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "model_run": cyc.run_str,
            "nowcast_valid_utc": nowcast_valid.replace(microsecond=0).isoformat(),
            "nowcast_forecast_hour": nowcast_fhr,
            "window_hours": WINDOW_HOURS,
            "climatology_version": "ERA5 1991-2020 v1" if clim is not None else None,
            "grid_resolution_deg": 0.25,
            "synthetic": False,
            "n_cities": len(records),
        },
        "cities": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2))
    log.info(f"Wrote {len(records)} records to {args.output}")


if __name__ == "__main__":
    sys.exit(main() or 0)
