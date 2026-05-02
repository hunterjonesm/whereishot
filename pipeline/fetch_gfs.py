"""
Fetch the latest GFS analysis + forecast, sample at city locations, join with
ERA5 climatology, and write data/cities_live.json.

Pipeline steps:
  1. Determine the most recent available GFS cycle (00/06/12/18Z).
  2. Download F000 (analysis) + F003..F024 (forecasts), variable TMP at 2 m AGL,
     using the GRIB filter on NOMADS so we only pull the bytes we need
     (~150KB/file × 9 files = under 2 MB total — well within Actions limits).
  3. Open as xarray, regrid is unnecessary (GFS 0.25° already matches our
     climatology grid).
  4. Sample at city lon/lat using nearest-neighbour.
  5. Compute current_temp_c (F000), daily_max_c, daily_min_c (over 24h window).
  6. Look up day-of-year climatology percentile from precomputed parquet.
  7. Write JSON with metadata.

Usage:
  python pipeline/fetch_gfs.py --cities pipeline/cities.csv \
      --climatology climatology/era5_doy_clim.parquet \
      --output data/cities_live.json

This script is designed to be idempotent and to fail loudly. If the latest
GFS cycle is not yet available, it will fall back to the previous one.
"""
from __future__ import annotations

import argparse
import io
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

# Forecast hours we sample. F000 = analysis, F003..F024 covers the next 24 h
# at 3-hour spacing — sufficient for daily max/min over the upcoming 24h.
FORECAST_HOURS = [0, 3, 6, 9, 12, 15, 18, 21, 24]


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
    """GFS cycles run at 00/06/12/18Z and become available ~3-4 hours later.

    We assume a 4-hour minimum lag for safety, then walk back through cycles
    until we find one that exists on NOMADS.
    """
    now = now_utc or datetime.now(timezone.utc)
    candidate = now - timedelta(hours=4)
    # Round down to nearest 6-hour cycle
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


def fetch_grib(cyc: GFSCycle, fhr: int) -> bytes:
    """Download a single forecast hour as raw GRIB2 bytes."""
    url = NOMADS_FILTER.format(cycle=cyc.cycle, fhr=fhr, date=cyc.date)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    if len(r.content) < 1000:
        raise RuntimeError(f"Suspiciously small response ({len(r.content)}B) for f{fhr:03d}")
    return r.content


def open_grib(buf: bytes) -> xr.DataArray:
    """Open a GRIB2 byte buffer as an xarray DataArray of 2m temperature in °C.

    Uses cfgrib via xarray. Requires `eccodes` system library to be installed.
    GitHub Actions: install via `apt install libeccodes-dev` in the workflow.
    """
    # cfgrib needs a real file path; write to a tempfile.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(buf)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib")
        # GFS 2m temperature variable name is 't2m'
        var = "t2m" if "t2m" in ds.data_vars else list(ds.data_vars)[0]
        da = ds[var] - 273.15  # Kelvin → Celsius
        # GFS longitude is 0..360; convert to -180..180 for sane interpolation
        da = da.assign_coords(longitude=(((da.longitude + 180) % 360) - 180))
        da = da.sortby("longitude")
        return da
    finally:
        Path(path).unlink(missing_ok=True)


def sample_at_cities(da: xr.DataArray, cities: pd.DataFrame) -> np.ndarray:
    """Nearest-neighbour sample of a 2D grid at a list of (lat, lon) points.

    GFS 0.25° is fine enough that nearest-neighbour is ~14 km offset worst case;
    bilinear would be marginally more accurate but adds complexity. Stick with
    nearest for the prototype.
    """
    lats = xr.DataArray(cities["lat"].values, dims="city")
    lons = xr.DataArray(cities["lon"].values, dims="city")
    sampled = da.sel(latitude=lats, longitude=lons, method="nearest")
    return sampled.values


def load_climatology(path: Path) -> pd.DataFrame:
    """Load precomputed ERA5 day-of-year climatology, sampled at city points.

    Expected columns: geonames_id, doy, mean_c, p95_c, p99_c, std_c
    Produced by the Earth Engine notebook in climatology/.
    """
    if not path.exists():
        log.warning(f"Climatology file not found at {path}; percentiles will be null")
        return None
    return pd.read_parquet(path)


def percentile_from_normal(value: float, mean: float, std: float) -> float:
    """Approximate percentile assuming a Gaussian climatology distribution.

    Cheap and good enough for a top-line metric; the precomputed climatology
    can also store empirical percentiles if we want to be more precise later.
    """
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
    ap.add_argument("--cities", type=Path, required=True,
                    help="CSV with columns: geonames_id,name,country,lat,lon,tz")
    ap.add_argument("--climatology", type=Path,
                    default=Path("climatology/era5_doy_clim.parquet"))
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    cities = pd.read_csv(args.cities)
    log.info(f"Loaded {len(cities)} cities")

    cyc = latest_available_cycle()

    # Pull all forecast hours, building a stack of 2D fields
    log.info(f"Fetching GFS {cyc.run_str}, hours {FORECAST_HOURS}")
    fields = {}
    for fhr in FORECAST_HOURS:
        buf = fetch_grib(cyc, fhr)
        fields[fhr] = open_grib(buf)
        log.info(f"  f{fhr:03d}: shape={fields[fhr].shape}, "
                 f"range=[{float(fields[fhr].min()):.1f}, {float(fields[fhr].max()):.1f}]°C")

    # Sample everything at city points
    samples = {fhr: sample_at_cities(fields[fhr], cities) for fhr in FORECAST_HOURS}
    sample_stack = np.stack([samples[fhr] for fhr in FORECAST_HOURS])  # (T, N)

    current = samples[0]
    daily_max = sample_stack.max(axis=0)
    daily_min = sample_stack.min(axis=0)

    clim = load_climatology(args.climatology)
    doy = cyc.datetime_utc.timetuple().tm_yday

    records = build_records(cities, current, daily_max, daily_min, clim, doy)
    out = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "model_run": cyc.run_str,
            "climatology_version": "ERA5 1991-2020 v1" if clim is not None else None,
            "grid_resolution_deg": 0.25,
            "synthetic": False,
            "n_cities": len(records),
            "forecast_hours": FORECAST_HOURS,
        },
        "cities": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2))
    log.info(f"Wrote {len(records)} records to {args.output}")


if __name__ == "__main__":
    sys.exit(main() or 0)
