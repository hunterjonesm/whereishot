"""Generate synthetic 'hottest cities' data matching the production schema.

Produces data/cities_live.json with the same structure the real pipeline will emit,
so the frontend can be developed and tested without the GFS pipeline running.

Schema:
{
  "metadata": {
    "generated_at_utc": ISO8601,
    "model_run": "GFS 2026-05-01 06z",  # null in synthetic
    "climatology_version": "ERA5 1991-2020 v1",
    "grid_resolution_deg": 0.25,
    "synthetic": true
  },
  "cities": [
    {
      "geonames_id": int,
      "name": str,
      "country": str (ISO3),
      "lat": float,
      "lon": float,
      "tz": str,                      # IANA timezone
      "current_temp_c": float,        # GFS analysis F000
      "daily_max_c": float,           # rolling 24h max (forecast+obs)
      "daily_min_c": float,           # rolling 24h min
      "anomaly_c": float,             # current - climatological mean for DOY
      "percentile": float,            # 0-100, current vs DOY climatology
      "climatology_mean_c": float,    # for tooltip context
      "climatology_p95_c": float
    }
  ]
}
"""
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

random.seed(42)

# A curated set of ~120 globally distributed cities. In the production pipeline
# this comes from GeoNames cities15000. Keeping it small here for synthetic dev.
CITIES = [
    # Format: (geonames_id, name, iso3, lat, lon, tz)
    (3936456, "Lima", "PER", -12.046, -77.043, "America/Lima"),
    (3448439, "Sao Paulo", "BRA", -23.548, -46.636, "America/Sao_Paulo"),
    (3871336, "Santiago", "CHL", -33.459, -70.645, "America/Santiago"),
    (3435910, "Buenos Aires", "ARG", -34.611, -58.396, "America/Argentina/Buenos_Aires"),
    (3688689, "Bogota", "COL", 4.711, -74.072, "America/Bogota"),
    (3646738, "Caracas", "VEN", 10.491, -66.880, "America/Caracas"),
    (3530597, "Mexico City", "MEX", 19.428, -99.128, "America/Mexico_City"),
    (5128581, "New York", "USA", 40.713, -74.006, "America/New_York"),
    (4887398, "Chicago", "USA", 41.878, -87.630, "America/Chicago"),
    (5391959, "San Francisco", "USA", 37.775, -122.418, "America/Los_Angeles"),
    (5419384, "Denver", "USA", 39.739, -104.984, "America/Denver"),
    (4684888, "Dallas", "USA", 32.776, -96.797, "America/Chicago"),
    (5308655, "Phoenix", "USA", 33.448, -112.074, "America/Phoenix"),
    (4699066, "Houston", "USA", 29.760, -95.369, "America/Chicago"),
    (5856195, "Honolulu", "USA", 21.307, -157.858, "Pacific/Honolulu"),
    (6167865, "Toronto", "CAN", 43.700, -79.420, "America/Toronto"),
    (6173331, "Vancouver", "CAN", 49.282, -123.120, "America/Vancouver"),
    (2643743, "London", "GBR", 51.508, -0.126, "Europe/London"),
    (2988507, "Paris", "FRA", 48.857, 2.351, "Europe/Paris"),
    (2950158, "Berlin", "DEU", 52.520, 13.405, "Europe/Berlin"),
    (3169070, "Rome", "ITA", 41.892, 12.482, "Europe/Rome"),
    (3117735, "Madrid", "ESP", 40.417, -3.704, "Europe/Madrid"),
    (2673730, "Stockholm", "SWE", 59.329, 18.069, "Europe/Stockholm"),
    (524901, "Moscow", "RUS", 55.756, 37.617, "Europe/Moscow"),
    (745044, "Istanbul", "TUR", 41.013, 28.949, "Europe/Istanbul"),
    (785842, "Athens", "GRC", 37.984, 23.728, "Europe/Athens"),
    (3067696, "Prague", "CZE", 50.088, 14.421, "Europe/Prague"),
    (756135, "Warsaw", "POL", 52.230, 21.012, "Europe/Warsaw"),
    (2759794, "Amsterdam", "NLD", 52.374, 4.889, "Europe/Amsterdam"),
    (2761369, "Vienna", "AUT", 48.208, 16.373, "Europe/Vienna"),
    (2673722, "Helsinki", "FIN", 60.171, 24.941, "Europe/Helsinki"),
    (3413829, "Reykjavik", "ISL", 64.135, -21.895, "Atlantic/Reykjavik"),
    (2643743, "Dublin", "IRL", 53.349, -6.260, "Europe/Dublin"),
    (2618425, "Copenhagen", "DNK", 55.676, 12.568, "Europe/Copenhagen"),
    (3413829, "Lisbon", "PRT", 38.722, -9.139, "Europe/Lisbon"),
    # Africa
    (360630, "Cairo", "EGY", 30.044, 31.236, "Africa/Cairo"),
    (2332459, "Lagos", "NGA", 6.524, 3.379, "Africa/Lagos"),
    (993800, "Johannesburg", "ZAF", -26.205, 28.050, "Africa/Johannesburg"),
    (3369157, "Cape Town", "ZAF", -33.925, 18.424, "Africa/Johannesburg"),
    (184745, "Nairobi", "KEN", -1.292, 36.822, "Africa/Nairobi"),
    (344979, "Addis Ababa", "ETH", 9.025, 38.747, "Africa/Addis_Ababa"),
    (2542997, "Casablanca", "MAR", 33.589, -7.603, "Africa/Casablanca"),
    (2542543, "Marrakech", "MAR", 31.629, -7.981, "Africa/Casablanca"),
    (2464470, "Tunis", "TUN", 36.806, 10.181, "Africa/Tunis"),
    (2464461, "Algiers", "DZA", 36.737, 3.087, "Africa/Algiers"),
    (379252, "Khartoum", "SDN", 15.501, 32.560, "Africa/Khartoum"),
    (2253354, "Dakar", "SEN", 14.694, -17.444, "Africa/Dakar"),
    (2287781, "Abidjan", "CIV", 5.359, -4.008, "Africa/Abidjan"),
    (2306104, "Accra", "GHA", 5.560, -0.205, "Africa/Accra"),
    (149400, "Dar es Salaam", "TZA", -6.792, 39.208, "Africa/Dar_es_Salaam"),
    (160196, "Kampala", "UGA", 0.314, 32.581, "Africa/Kampala"),
    (2314302, "Kinshasa", "COD", -4.325, 15.322, "Africa/Kinshasa"),
    (933773, "Harare", "ZWE", -17.829, 31.054, "Africa/Harare"),
    (1024696, "Maputo", "MOZ", -25.966, 32.568, "Africa/Maputo"),
    (3354077, "Windhoek", "NAM", -22.560, 17.084, "Africa/Windhoek"),
    # Middle East
    (292223, "Dubai", "ARE", 25.205, 55.271, "Asia/Dubai"),
    (108410, "Riyadh", "SAU", 24.713, 46.675, "Asia/Riyadh"),
    (281184, "Jerusalem", "ISR", 31.769, 35.214, "Asia/Jerusalem"),
    (250441, "Amman", "JOR", 31.956, 35.945, "Asia/Amman"),
    (276781, "Beirut", "LBN", 33.889, 35.495, "Asia/Beirut"),
    (98182, "Baghdad", "IRQ", 33.341, 44.401, "Asia/Baghdad"),
    (112931, "Tehran", "IRN", 35.689, 51.389, "Asia/Tehran"),
    (290030, "Doha", "QAT", 25.286, 51.531, "Asia/Qatar"),
    (285787, "Kuwait City", "KWT", 29.378, 47.991, "Asia/Kuwait"),
    # South Asia
    (1275339, "Mumbai", "IND", 19.076, 72.878, "Asia/Kolkata"),
    (1273294, "Delhi", "IND", 28.704, 77.103, "Asia/Kolkata"),
    (1264527, "Chennai", "IND", 13.083, 80.270, "Asia/Kolkata"),
    (1275004, "Kolkata", "IND", 22.572, 88.364, "Asia/Kolkata"),
    (1277333, "Bangalore", "IND", 12.972, 77.594, "Asia/Kolkata"),
    (1176615, "Karachi", "PAK", 24.861, 67.010, "Asia/Karachi"),
    (1185241, "Dhaka", "BGD", 23.811, 90.413, "Asia/Dhaka"),
    (1283240, "Kathmandu", "NPL", 27.717, 85.324, "Asia/Kathmandu"),
    (1248991, "Colombo", "LKA", 6.932, 79.858, "Asia/Colombo"),
    # SE Asia
    (1880252, "Singapore", "SGP", 1.290, 103.852, "Asia/Singapore"),
    (1735161, "Kuala Lumpur", "MYS", 3.139, 101.687, "Asia/Kuala_Lumpur"),
    (1609350, "Bangkok", "THA", 13.756, 100.502, "Asia/Bangkok"),
    (1581130, "Hanoi", "VNM", 21.028, 105.804, "Asia/Ho_Chi_Minh"),
    (1566083, "Ho Chi Minh City", "VNM", 10.823, 106.630, "Asia/Ho_Chi_Minh"),
    (1642911, "Jakarta", "IDN", -6.211, 106.845, "Asia/Jakarta"),
    (1701668, "Manila", "PHL", 14.599, 120.984, "Asia/Manila"),
    (1821306, "Phnom Penh", "KHM", 11.563, 104.916, "Asia/Phnom_Penh"),
    # East Asia
    (1850147, "Tokyo", "JPN", 35.690, 139.692, "Asia/Tokyo"),
    (1853909, "Osaka", "JPN", 34.694, 135.502, "Asia/Tokyo"),
    (1835848, "Seoul", "KOR", 37.566, 126.978, "Asia/Seoul"),
    (1816670, "Beijing", "CHN", 39.905, 116.397, "Asia/Shanghai"),
    (1796236, "Shanghai", "CHN", 31.230, 121.474, "Asia/Shanghai"),
    (1809858, "Guangzhou", "CHN", 23.129, 113.264, "Asia/Shanghai"),
    (1815286, "Chongqing", "CHN", 29.563, 106.551, "Asia/Shanghai"),
    (1819729, "Hong Kong", "HKG", 22.302, 114.177, "Asia/Hong_Kong"),
    (1668341, "Taipei", "TWN", 25.033, 121.565, "Asia/Taipei"),
    (2028462, "Ulaanbaatar", "MNG", 47.886, 106.906, "Asia/Ulaanbaatar"),
    # Oceania
    (2147714, "Sydney", "AUS", -33.868, 151.207, "Australia/Sydney"),
    (2158177, "Melbourne", "AUS", -37.814, 144.963, "Australia/Melbourne"),
    (2174003, "Brisbane", "AUS", -27.470, 153.025, "Australia/Brisbane"),
    (2063523, "Perth", "AUS", -31.953, 115.857, "Australia/Perth"),
    (2078025, "Adelaide", "AUS", -34.929, 138.601, "Australia/Adelaide"),
    (2179537, "Auckland", "NZL", -36.848, 174.763, "Pacific/Auckland"),
    (2179670, "Wellington", "NZL", -41.286, 174.776, "Pacific/Auckland"),
    # Russia / Central Asia
    (2013348, "Vladivostok", "RUS", 43.117, 131.900, "Asia/Vladivostok"),
    (1502026, "Novosibirsk", "RUS", 55.041, 82.934, "Asia/Novosibirsk"),
    (1486209, "Yekaterinburg", "RUS", 56.838, 60.605, "Asia/Yekaterinburg"),
    (1526384, "Almaty", "KAZ", 43.255, 76.913, "Asia/Almaty"),
    (1217752, "Tashkent", "UZB", 41.299, 69.240, "Asia/Tashkent"),
    # Polar
    (3833367, "Ushuaia", "ARG", -54.802, -68.303, "America/Argentina/Ushuaia"),
    (5879400, "Anchorage", "USA", 61.218, -149.900, "America/Anchorage"),
]


def fake_temp_for_lat_lon(lat, lon, doy):
    """Plausible-ish temperature based on latitude and day of year.

    This is purely for synthetic data; real values come from GFS.
    """
    # Seasonal cycle: DOY 172 = ~21 June (NH summer)
    seasonal = math.cos(2 * math.pi * (doy - 172) / 365.25)
    # Stronger in higher latitudes
    seasonal_amplitude = 18 * (abs(lat) / 90) ** 0.7
    # Latitudinal mean
    base = 30 - 0.6 * abs(lat)
    # Hemisphere flip
    hemisphere_sign = 1 if lat >= 0 else -1
    temp = base + hemisphere_sign * seasonal * seasonal_amplitude
    # Continental vs maritime — very rough proxy
    continentality = 1 + 0.1 * math.sin(math.radians(lon * 2))
    temp *= continentality
    # Random weather noise
    temp += random.gauss(0, 4)
    return round(temp, 1)


def build_record(city, doy):
    gid, name, iso3, lat, lon, tz = city
    current = fake_temp_for_lat_lon(lat, lon, doy)
    daily_max = current + abs(random.gauss(3, 1.5))
    daily_min = current - abs(random.gauss(5, 2))
    # Climatological mean for this DOY at this lat (zero noise)
    clim_mean = fake_temp_for_lat_lon(lat, lon, doy) - random.gauss(0, 0.2)
    # Recompute deterministic clim
    seasonal = math.cos(2 * math.pi * (doy - 172) / 365.25)
    seasonal_amplitude = 18 * (abs(lat) / 90) ** 0.7
    base = 30 - 0.6 * abs(lat)
    hemisphere_sign = 1 if lat >= 0 else -1
    clim_mean = round(base + hemisphere_sign * seasonal * seasonal_amplitude, 1)
    clim_p95 = round(clim_mean + 4.5, 1)
    anomaly = round(current - clim_mean, 1)
    # Cheap percentile estimate from anomaly assuming sigma~3
    z = anomaly / 3.0
    # Approx normal CDF
    pct = 0.5 * (1 + math.erf(z / math.sqrt(2))) * 100
    return {
        "geonames_id": gid,
        "name": name,
        "country": iso3,
        "lat": lat,
        "lon": lon,
        "tz": tz,
        "current_temp_c": current,
        "daily_max_c": round(daily_max, 1),
        "daily_min_c": round(daily_min, 1),
        "anomaly_c": anomaly,
        "percentile": round(pct, 1),
        "climatology_mean_c": clim_mean,
        "climatology_p95_c": clim_p95,
    }


def main():
    now = datetime.now(timezone.utc)
    doy = int(now.strftime("%j"))
    cities = [build_record(c, doy) for c in CITIES]
    out = {
        "metadata": {
            "generated_at_utc": now.replace(microsecond=0).isoformat(),
            "model_run": None,
            "climatology_version": "synthetic-v0",
            "grid_resolution_deg": 0.25,
            "synthetic": True,
            "n_cities": len(cities),
        },
        "cities": cities,
    }
    out_path = Path(__file__).parent / "cities_live.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(cities)} cities to {out_path}")


if __name__ == "__main__":
    main()
