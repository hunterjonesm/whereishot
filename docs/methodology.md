---
layout: default
title: Methodology
---

# Methodology — "Where is it hot"

This page describes how each metric is computed, the assumptions involved, and the limitations users should be aware of.

## Data sources

**Live temperature data.** NOAA Global Forecast System (GFS) at 0.25° resolution. The pipeline pulls the four most recent cycles available on NOMADS — the latest analysis plus the three prior — and assembles a continuous hourly time series of forecast 2-metre air temperature spanning roughly fifty hours of UTC. This is enough to fully cover any city's local calendar day from any moment the pipeline runs, including cities near the dateline.

**Climatology.** ECMWF ERA5 reanalysis daily-mean 2-metre temperature for 1991–2020, sampled at each city's grid cell using Google Earth Engine. The detrended-σ method (described below) is applied to compute the per-day-of-year distribution.

**Cities.** ~310 globally distributed major cities, sourced from GeoNames (CC-BY 4.0). Each city carries a stable GeoNames ID, lat/lon, and IANA timezone identifier.

## How "current" is computed

The pipeline runs every six hours. On each run, it fetches every hourly forecast from the four most recent GFS cycles and emits, for each city, a list of (UTC valid time, temperature) pairs spanning roughly the last two hours through the next eighteen hours.

When you load the page in your browser, the frontend selects, for each city, the hourly sample whose UTC valid time is closest to your wall clock at that moment. This means displayed "current temperature" is always within thirty minutes of true now — even if the pipeline last ran several hours ago. The leaderboard re-runs this selection every five minutes so a tab left open updates automatically.

This design is the reason the pipeline only needs to run four times per day. The "current" view is computed on demand by the browser; the pipeline's job is just to keep the underlying forecast data fresh.

## The four metrics

### Current temperature

The hourly forecast valid closest to your current moment, sampled at the city's grid cell using nearest-neighbor lookup.

### Today vs. historical normal

This is the climatology comparison. It works as follows.

For each city, we take all hourly forecasts whose valid time falls within "today" in that city's local timezone — from local midnight to local midnight. We average those hourly values to obtain today's *daily mean* temperature. We then compare that daily mean to the climatological distribution of daily-mean temperatures for the same calendar day, using a fifteen-day centered window in the 1991–2020 reference period.

The reported number is the percentile rank, computed as Φ((today − μ)/σ) where Φ is the standard normal cumulative distribution function. A value of 95 means today's daily mean is exceeded only 5% of the time at this city for this part of the year, in the climatological record.

The comparison is apples-to-apples: daily-mean today vs. daily-mean climatology. An earlier prototype version compared instantaneous current temperature to daily-mean climatology, which was biased by local time of day; that has been corrected.

### Daily maximum and minimum

The highest (or lowest) hourly forecast value within today, where "today" means local midnight to local midnight in the city's IANA timezone. This matches the WMO convention for daily extremes, and means Tokyo's "today" can span entirely different UTC hours than Geneva's.

## The detrended-σ climatology

A naive approach to climatological standard deviation is to take all daily-mean values from a fifteen-day window across thirty years and compute σ directly. This works in deep summer or deep winter, when temperatures are relatively stable across the window, but it overestimates σ during the equinoxes when the seasonal cycle is rising or falling rapidly within the window itself.

The method we use instead — sometimes called the "anomaly method" — is:

1. For each city, fit a smooth seasonal cycle to the full thirty-year record. We use a Fourier series with four harmonics, which is sufficient to capture the annual and semi-annual structure without overfitting.
2. Subtract the fitted seasonal cycle from each daily observation to obtain anomalies.
3. The climatological σ for any given day-of-year is then the standard deviation of those anomalies within the fifteen-day window.

This separates the seasonal variation (which is captured cleanly by the smooth fit) from interannual variability (which is what σ should actually measure). The resulting σ is much more stable across the calendar year, and the percentile rankings near equinoxes are no longer biased.

## Limitations

Three caveats worth knowing.

GFS forecasts are model output, not observations. They assimilate observations but the value at any specific grid cell reflects the model's view of conditions, not a thermometer reading. For most public-information uses this is appropriate; for record-checking against specific weather stations, station data should be consulted.

The percentile metric assumes a Gaussian distribution of daily-mean anomalies, parameterized by the climatological mean and detrended σ. This is generally a reasonable assumption for daily means (less so for daily maxima or minima), but it understates the true frequency of extreme tails. For tail-sensitive uses, the underlying climatology table also exposes empirical 95th and 99th percentiles for the same window.

The city list is curated, not exhaustive. ~310 cities cover all UN member states and major regional centers, but smaller cities and rural locations are absent. Adding cities is straightforward and documented in the repository.

## Update cadence

The pipeline runs at 05:13, 11:13, 17:13, and 23:13 UTC — five hours after each GFS cycle becomes available, which gives the cycle plenty of time to fully publish on NOMADS. The "Current temperature" displayed in your browser is selected dynamically and is always within thirty minutes of your wall clock; the underlying forecast data never goes more than six hours out of date.

## Reproducibility

The product is built end-to-end from open code and public data. Pipeline source, climatology notebook, and data outputs are all in the public repository. Each published `cities_live.json` carries the GFS cycle identifier, climatology version, and pipeline timestamp in its metadata block.
