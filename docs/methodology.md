# Methodology — "Where is it hot"

This page describes how each of the four heat metrics on the map is computed, and what assumptions we are making.

## Data sources

**Live temperature data** — National Oceanic and Atmospheric Administration (NOAA) Global Forecast System, 0.25° global grid. We pull the latest available cycle (00, 06, 12, or 18 UTC) from NOAA NOMADS, including the analysis (F000) and forecast hours F003 through F024 in three-hour steps.

**Climatology** — European Centre for Medium-Range Weather Forecasts (ECMWF) ERA5 reanalysis, daily-mean 2-metre temperature, 1991–2020. Computed in Google Earth Engine using a 15-day centered window per day of year, sampled at each city's location.

**Cities** — GeoNames cities15000 (CC-BY 4.0). The list is curated to ~100 globally distributed major cities for the prototype.

## The four metrics

### Current temperature
The 2-metre air temperature from the most recent GFS analysis, sampled at each city's grid cell using nearest-neighbor lookup. This is the model's best estimate of present conditions, available within roughly four hours of the cycle time.

### Anomaly vs. climatology (percentile)
For each city, we compare the current temperature against the day-of-year climatology distribution and report a percentile. The percentile is computed as Φ((x − μ)/σ), where μ and σ are the climatological mean and standard deviation for the surrounding 15-day window in 1991–2020, and Φ is the standard normal cumulative distribution. A reading at the 95th percentile means the current temperature is exceeded only 5% of the time at this location during this part of the year, in the climatological record.

### Daily maximum / minimum
The maximum (or minimum) of the analysis-plus-forecast temperature stack from F000 through F024, sampled in three-hour steps. This represents the warmest (or coolest) point in the next 24 hours, including the present moment.

## Limitations and caveats

Two practical and one philosophical caveat to keep in mind:

GFS analysis is a model estimate, not a station observation. It assimilates observations but the value at any specific point reflects the model's view, not a thermometer reading. For most public-information uses this is appropriate; for record-checking, station data should be consulted.

Climatology is computed from ERA5 daily-mean temperature, while the live signal is a point-in-time reading from GFS. These are not identical quantities, but they share the same day-of-year structure, so the percentile rank remains a meaningful indicator of how unusual current conditions are. We are not claiming an absolute calibration between the two products.

The Gaussian percentile fit is convenient and stable but understates the true frequency of extreme values in the tails. For tail-sensitive uses, the underlying climatology parquet also exposes empirical 95th and 99th percentiles for the same window.

## Update cadence

The live data refreshes every three hours, around 4, 7, 10, 13, 16, 19, 22, and 1 UTC, scheduled to run roughly an hour after each new GFS cycle becomes available. The "last updated" timestamp on the map header reflects the most recent successful pipeline run.

## Reproducibility

This product is built end-to-end from open code and public data. The pipeline source, climatology notebook, and data outputs are all in the public repository. Each published `cities_live.json` carries the GFS cycle identifier and climatology version in its metadata block, providing the version pinning required by the Terms of Reference.
