# Where is it hot — global heat extremes prototype

A live global view of temperature extremes, refreshed every three hours, built on free public climate data. Reference deliverable for **TOR §1.3.1** ("what is hot now") of the ClimaHealth/GHHIN website redesign.

This repository implements the **cache-and-serve** architecture mandated by §1.3.3: scheduled jobs pull and process upstream data, write static outputs into the repo, and the frontend consumes those static files only — the browser never touches an upstream API.

---

## What you're looking at

A single page (`frontend/index.html`) showing a global map. The user picks one of four definitions of "hot" from the sidebar:

1. **Current temperature** — latest GFS analysis, 2 m air temp
2. **Anomaly vs. climatology** — percentile rank of current temp against ERA5 1991–2020 normals (15-day centered DOY window)
3. **Daily maximum** — highest observed-or-forecast temp in next 24 h
4. **Daily minimum** — lowest observed-or-forecast temp in next 24 h

A day/night terminator is overlaid in real time so the global view makes intuitive sense. A leaderboard of top 25 cities accompanies the map. Click any city for a popup with all four metrics plus climatology context.

---

## Architecture

```
                   one-time / occasional                   live (every 3 h)
                   ─────────────────────                   ───────────────────
                                                           NOAA NOMADS
                       Earth Engine                              │ GFS 0.25°
                            │                                    │ GRIB2
                            ▼                                    ▼
                    ERA5 1991–2020             ┌───── pipeline/fetch_gfs.py ──────┐
                    daily-mean temp            │                                  │
                            │                  │  • pull cycle, 9 forecast hours  │
                       sample at city          │  • sample at city points         │
                       points × 30 yrs         │  • join with climatology         │
                            │                  │  • compute 4 metrics             │
                            ▼                  │                                  │
                  climatology/era5_doy_clim    └─────────────┬────────────────────┘
                  .parquet  (committed once)                 │
                            │                                ▼
                            └────────────────────►   data/cities_live.json
                                                              │
                                                     committed by GitHub Action
                                                              │
                                                              ▼
                                                   frontend/index.html  (GitHub Pages)
                                                              │
                                                              ▼
                                                          end users
```

All three blocks run on free-tier infrastructure. No API keys, no secrets, no server.

---

## Repository layout

```
.
├── frontend/
│   └── index.html            ← single-file MapLibre app (deploy to GitHub Pages)
├── pipeline/
│   ├── fetch_gfs.py          ← live ingest, runs in GitHub Actions every 3 h
│   ├── cities.csv            ← curated city list, joined to GeoNames IDs
│   └── requirements.txt
├── climatology/
│   └── build_climatology.ipynb  ← one-time precompute, runs in Google Colab
├── data/
│   ├── cities_live.json      ← committed by the Action; the frontend reads this
│   └── generate_synthetic.py ← develop the frontend without running the pipeline
├── .github/workflows/
│   └── refresh.yml           ← cron schedule + commit step
└── docs/
    └── methodology.md        ← user-facing methods note (see TOR §1.5.5)
```

---

## Quick start (local dev)

```bash
# 1. Generate a synthetic data file matching the production schema
python data/generate_synthetic.py

# 2. Serve the frontend locally (any static server works)
cd frontend && python -m http.server 8000
# open http://localhost:8000

# 3. Switch metrics from the sidebar; click cities to inspect
```

Frontend changes don't require touching the pipeline — the synthetic data has the same schema as the live data.

---

## Setting up the live pipeline (one-time)

### A. Build the climatology (run once)

1. Open `climatology/build_climatology.ipynb` in Google Colab.
2. Sign up for Earth Engine (free for nonprofit/research use): https://earthengine.google.com
3. Replace the placeholder project ID in cell 1.
4. Run cells 1–4. This kicks off 30 export tasks (one per year, 1991–2020). Each finishes in a few minutes; in parallel, expect under an hour total. Files land in your Drive at `heatmap_climatology/era5_samples_<year>.csv`.
5. Run cells 5–8 to assemble the per-year CSVs, compute the 15-day-window DOY climatology, sanity-check, and download `era5_doy_clim.parquet`.
6. Commit it: `mv ~/Downloads/era5_doy_clim.parquet climatology/ && git add climatology/era5_doy_clim.parquet && git commit -m "Add ERA5 1991-2020 climatology"`

The parquet is small (~5 MB). It belongs in the repo.

**When to re-run:** the WMO updates the standard climate normal every 10 years; the next update will land in 2031 (1991-2020 → 2001-2030). Until then, the climatology never changes.

### B. Enable the GitHub Action

Once `climatology/era5_doy_clim.parquet` is in the repo:

1. Push to GitHub (your free Pro student plan is more than sufficient — see "Resource budget" below).
2. The workflow at `.github/workflows/refresh.yml` will start running on its 3-hour cron immediately, **and** is exposed as a manual trigger button under Actions → Refresh heat map data → Run workflow.
3. First run typically takes ~3-4 minutes. Watch the Actions tab; if it fails, the failed `cities_live.json` is uploaded as an artifact for debugging.

### C. Publish the frontend

Settings → Pages → Source: `main` branch → root.

The frontend is configured to fetch `../data/cities_live.json` relative to its own URL, so a GitHub Pages deploy from repo root just works. Custom domain (e.g., `whereishot.climahealth.org`) is configurable later in repo Settings → Pages.

---

## Resource budget

| Resource | Usage | Free-tier limit | Headroom |
|----------|-------|-----------------|----------|
| GitHub Actions minutes | 8 runs/day × ~3 min = 720 min/month | 3,000 min/month (Pro) | 4× |
| GitHub Pages bandwidth | Negligible (~1 MB/page load) | 100 GB/month | Effectively unlimited |
| Repo storage | ~10 MB total | 1 GB recommended | Unconstrained |
| NOAA NOMADS bandwidth | ~2 MB/run × 8/day = 16 MB/day | None published | Comfortable |
| Earth Engine compute | One-time, ~30 min total | Free for nonprofit | Trivial |
| Google Drive (export) | ~1 GB peak then cleanup | 15 GB free | Trivial |

The architecture is deliberately quiet on quotas. If a future use case needs more frequent runs or more cities, the migration path (per **TOR §1.3.4**) is to move only the pipeline step to Cloudflare Workers or a small VM; the data contract (`cities_live.json`) doesn't change.

---

## Data sources & licensing (TOR §1.6.1 audit)

| Source | License | Attribution required | Redistribution |
|--------|---------|----------------------|----------------|
| **NOAA GFS** (live obs+fcst) | US Government work — public domain | None required, courteous to credit NOAA | Permitted |
| **ECMWF ERA5** (climatology) | Copernicus license | Yes: *"Generated using Copernicus Climate Change Service information [year]"* | Permitted with attribution |
| **GeoNames** cities | CC-BY 4.0 | Yes | Permitted with attribution |
| **Natural Earth** boundaries | Public domain | None required, courteous to credit | Permitted |

Attribution strings are surfaced in the frontend colophon and in the `metadata` block of `cities_live.json`. **For production launch, country boundaries must be swapped from Natural Earth to a WHO-endorsed source per TOR §1.6.3** — leaving Natural Earth in for the prototype is fine, but flagging it as a known follow-up for the design-system delivery.

---

## Methodology decisions worth knowing

- **Data paradigm: gridded, not station.** GFS analysis is used as the "current obs" because using the same model for analysis and forecast keeps the four metrics in a single internally-consistent paradigm. Station-level extremes can be a future supplement.
- **Climatology window: 15-day centered, day-of-year, 1991–2020.** Weekly is too noisy at point-sampled grid cells (each day-of-year × city has only ~30 samples in raw form); 15 days × 30 years gives ~450 samples per DOY, comfortable for stable percentiles. 1991–2020 is the WMO standard normal as of writing.
- **Percentile model: Gaussian fit.** Computed as Φ((x − μ)/σ) using the precomputed mean and standard deviation. Cheap, smooth, and adequate for the top-line metric. If a future analysis needs heavy-tail accuracy, the climatology parquet can be extended with empirical p95/p99 (already computed) or full quantile splines.
- **Forecast horizon: 24 h, 3-hourly samples.** Daily max/min is taken over F000–F024 in 3-hour steps. Wider windows would conflate "daily" with "next several days."
- **Nearest-neighbour grid sampling.** GFS 0.25° is fine enough that bilinear gives marginal gains relative to the model's own native uncertainty. KISS.

A user-facing version of this section lives in `docs/methodology.md` and is linked from the frontend.

---

## Cloning this for the next visualization (TOR §1.3.4)

This repo is structured as a **template**: the in-house technical team should be able to fork it, swap the data ingest, and produce a second visualization without agency support. The data contract is:

- `data/<feature>_live.json` is the only thing the frontend reads
- The pipeline writes that file
- The Action commits it
- The frontend fetches it

Anything that fits this contract slots in. Recommended next: a heat-burden product computed from ERA5 daily + a population raster — the climatology pipeline is already 80% of what's needed.

---

## Acceptance criteria status (TOR §1.8)

- [x] In-house technical team can fork & adapt — template structure documented above
- [x] Cache-and-serve architecture — implemented, no browser-to-API calls
- [ ] 14 consecutive days of unattended refreshes — verifiable only after deployment; the Action's structure is correct, monitor the Actions tab
- [ ] Mobile responsive at 360 px — sidebar collapses to top panel below 900 px viewport; spot-check on real devices before sign-off
- [ ] WCAG 2.1 AA — color ramps include luminance variation (not color-only), keyboard nav present via `<button>` elements; full audit needed before launch (TOR §1.5.4)
- [ ] Lighthouse ≥85 mobile — measure after first deploy; the page is intentionally minimal and should pass

---

## License

Code: MIT. Data outputs in `data/` carry their upstream licenses (Copernicus + GeoNames + Natural Earth attribution as listed above).
