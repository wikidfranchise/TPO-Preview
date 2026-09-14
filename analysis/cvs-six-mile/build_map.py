#!/usr/bin/env python3
"""Build an audited six-mile CVS coverage map from public source data."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from urllib3.util.retry import Retry

import folium
import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from folium.plugins import FastMarkerCluster
from pyproj import Geod
from shapely import contains_xy
from shapely.geometry import Point, Polygon, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
CACHE = ROOT / ".cache"
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)

CVS_DIRECTORY = "https://www.cvs.com/store-locator/cvs-pharmacy-locations"
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
STATE_BOUNDARIES = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_500k.zip"
BLOCK_URL = (
    "https://www2.census.gov/geo/tiger/TIGER2020PL/LAYER/TABBLOCK/2020/"
    "tl_2020_{fips}_tabblock20.zip"
)
CENSUS_API = "https://api.census.gov/data/2020/dec/pl"
RADIUS_M = 6 * 1609.344
GEOD = Geod(ellps="WGS84")
UA = "Mozilla/5.0 (compatible; CVS-Coverage-Research/1.0; public-data-analysis)"

STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}
FIPS_STATE = {v: k for k, v in STATE_FIPS.items()}


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    retry = Retry(
        total=5, backoff_factor=1.2, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    s.mount("https://", requests.adapters.HTTPAdapter(max_retries=retry))
    return s


def get_text(url: str, timeout: int = 60) -> str:
    s = session()
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def valid_latlon(lat: Any, lon: Any) -> bool:
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False
    return 17.0 <= lat <= 72.0 and -180.0 <= lon <= -64.0


def first_value(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def normalized_record(d: dict[str, Any], source_url: str) -> dict[str, Any] | None:
    lat = first_value(d, ("latitude", "lat", "storeLatitude", "store_latitude"))
    lon = first_value(d, ("longitude", "lng", "lon", "storeLongitude", "store_longitude"))
    if not valid_latlon(lat, lon):
        geo = d.get("geo")
        if isinstance(geo, dict):
            lat = first_value(geo, ("latitude", "lat"))
            lon = first_value(geo, ("longitude", "lng", "lon"))
    address = d.get("address") if isinstance(d.get("address"), dict) else d
    street = first_value(address, ("streetAddress", "street", "address1", "street_address"))
    city = first_value(address, ("addressLocality", "city", "locality"))
    state = first_value(address, ("addressRegion", "state", "stateCode", "state_code"))
    postal = first_value(address, ("postalCode", "zip", "zipcode", "postal_code"))
    store_id = first_value(d, ("storeId", "storeNumber", "store_id", "number"))
    if not street or not city or not state:
        return None
    return {
        "store_id": "" if store_id is None else str(store_id),
        "street": str(street).strip(),
        "city": str(city).strip(),
        "state": str(state).strip().upper(),
        "zip": "" if postal is None else str(postal).strip()[:10],
        "latitude": float(lat) if valid_latlon(lat, lon) else None,
        "longitude": float(lon) if valid_latlon(lat, lon) else None,
        "source_url": source_url,
    }


def walk_json(value: Any, source_url: str, found: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        rec = normalized_record(value, source_url)
        if rec:
            found.append(rec)
        for child in value.values():
            walk_json(child, source_url, found)
    elif isinstance(value, list):
        for child in value:
            walk_json(child, source_url, found)


def parse_visible_address(text: str, source_url: str) -> dict[str, Any] | None:
    lines = [re.sub(r"\s+", " ", x).strip(" ,") for x in text.splitlines() if x.strip()]
    if len(lines) < 2:
        joined = re.sub(r"\s+", " ", text).strip()
        match = re.match(r"(.+?)\s+([A-Za-z .'-]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", joined)
        if not match:
            return None
        street, city, state, postal = match.groups()
    else:
        tail = lines[-1]
        match = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", tail)
        if not match:
            return None
        city, state, postal = match.groups()
        street = " ".join(lines[:-1])
    return {
        "store_id": "", "street": street, "city": city, "state": state,
        "zip": postal, "latitude": None, "longitude": None, "source_url": source_url,
    }


def parse_city(url: str) -> list[dict[str, Any]]:
    html = get_text(url)
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict[str, Any]] = []

    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or ("latitude" not in raw.lower() and "storelatitude" not in raw.lower()):
            continue
        try:
            walk_json(json.loads(raw), url, found)
        except (json.JSONDecodeError, TypeError):
            continue

    for p in soup.select("p.store-address"):
        rec = parse_visible_address(p.get_text("\n", strip=True), url)
        if rec:
            found.append(rec)

    deduped: dict[str, dict[str, Any]] = {}
    for rec in found:
        key = "|".join([
            rec["street"].lower(), rec["city"].lower(), rec["state"], rec["zip"][:5]
        ])
        old = deduped.get(key)
        if old is None or (old["latitude"] is None and rec["latitude"] is not None):
            deduped[key] = rec
    return list(deduped.values())


def directory_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.select("div.states li a[href], main a[href*='/store-locator/cvs-pharmacy-locations/']"):
        href = urljoin(base, a.get("href", ""))
        if href.startswith(CVS_DIRECTORY + "/"):
            links.append(href.rstrip("/"))
    return sorted(set(links))


def scrape_cvs() -> pd.DataFrame:
    snapshot = OUT / "cvs-locations.csv"
    override = os.environ.get("CVS_DATA_URL", "").strip()
    if override:
        df = pd.read_csv(override)
        rename = {
            "lat": "latitude", "lng": "longitude", "lon": "longitude",
            "city_name": "city", "state_id": "state", "state_code": "state",
            "postal_code": "zip", "street_address": "street",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        required = {"latitude", "longitude"}
        missing = required - set(df.columns)
        if missing:
            raise RuntimeError("CVS_DATA_URL is missing columns: " + ", ".join(sorted(missing)))
        df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
        df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
        df = df[
            df.apply(lambda r: valid_latlon(r["latitude"], r["longitude"]), axis=1)
        ].copy()
        for column in ("street", "city", "state", "zip", "store_id"):
            if column not in df:
                df[column] = ""
        df["source_url"] = override
        df = df.reset_index(drop=True)
        if len(df) < 8500:
            raise RuntimeError(
                f"Only {len(df):,} valid unique stores were loaded from CVS_DATA_URL."
            )
        df.to_csv(snapshot, index=False)
        return df

    base_html = get_text(CVS_DIRECTORY)
    state_urls = directory_links(base_html, CVS_DIRECTORY)
    if len(state_urls) < 40:
        raise RuntimeError(f"CVS state directory returned only {len(state_urls)} state links.")

    city_urls: set[str] = set()
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(get_text, u): u for u in state_urls}
        for future in as_completed(futures):
            state_url = futures[future]
            html = future.result()
            for u in directory_links(html, state_url):
                if u != state_url:
                    city_urls.add(u)

    if len(city_urls) < 2500:
        raise RuntimeError(f"CVS directory returned only {len(city_urls)} city links.")

    records: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(parse_city, u): u for u in sorted(city_urls)}
        for i, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            try:
                records.extend(future.result())
            except Exception as exc:
                failures.append((url, str(exc)))
            if i % 250 == 0:
                print(f"Parsed {i:,}/{len(city_urls):,} city pages; {len(records):,} stores")

    if failures:
        pd.DataFrame(failures, columns=["url", "error"]).to_csv(OUT / "scrape-failures.csv", index=False)

    df = pd.DataFrame(records)
    if df.empty:
        raise RuntimeError("No CVS stores were extracted.")
    df["key"] = (
        df["street"].str.lower().str.replace(r"\W+", "", regex=True)
        + "|" + df["city"].str.lower() + "|" + df["state"] + "|" + df["zip"].str[:5]
    )
    df = df.sort_values("latitude", na_position="last").drop_duplicates("key", keep="first")
    df = df.drop(columns="key").reset_index(drop=True)

    if len(df) < 8500:
        raise RuntimeError(
            f"Only {len(df):,} unique stores were captured; refusing to call this a national snapshot."
        )
    df.to_csv(snapshot, index=False)
    return df


def census_batch_geocode(df: pd.DataFrame) -> pd.DataFrame:
    missing = df["latitude"].isna() | df["longitude"].isna()
    todo = df[missing].copy()
    if todo.empty:
        return df

    print(f"Geocoding {len(todo):,} addresses with the U.S. Census geocoder")
    results: dict[int, tuple[float, float, str]] = {}
    s = session()
    indices = list(todo.index)

    for start in range(0, len(indices), 5000):
        batch_indices = indices[start:start + 5000]
        payload = io.StringIO()
        writer = csv.writer(payload, lineterminator="\n")
        for idx in batch_indices:
            row = df.loc[idx]
            writer.writerow([idx, row.street, row.city, row.state, str(row.zip)[:5]])

        files = {"addressFile": ("addresses.csv", payload.getvalue(), "text/csv")}
        data = {"benchmark": "Public_AR_Current", "vintage": "Current_Current"}
        response = s.post(CENSUS_GEOCODER, files=files, data=data, timeout=300)
        response.raise_for_status()

        for row in csv.reader(io.StringIO(response.text)):
            if len(row) < 6:
                continue
            idx = int(row[0])
            if row[2].strip().lower() != "match":
                continue
            coords = row[5].split(",")
            if len(coords) != 2:
                continue
            lon, lat = map(float, coords)
            if valid_latlon(lat, lon):
                results[idx] = (lat, lon, row[3])

    for idx, (lat, lon, matched) in results.items():
        df.at[idx, "latitude"] = lat
        df.at[idx, "longitude"] = lon
        df.at[idx, "geocoder_match"] = matched

    unmatched = df[df["latitude"].isna() | df["longitude"].isna()].copy()
    unmatched.to_csv(OUT / "unmatched-locations.csv", index=False)
    match_rate = 1.0 - len(unmatched) / len(df)
    print(f"Coordinate completeness: {match_rate:.3%}")
    if match_rate < 0.99:
        raise RuntimeError(
            f"Coordinate completeness is {match_rate:.2%}; exact-map threshold is 99.00%."
        )
    return df


def geodesic_circle(lon: float, lat: float, radius_m: float, vertices: int = 72) -> Polygon:
    bearings = np.linspace(0.0, 360.0, vertices, endpoint=False)
    lons = np.full(vertices, lon, dtype=float)
    lats = np.full(vertices, lat, dtype=float)
    distances = np.full(vertices, radius_m, dtype=float)
    out_lon, out_lat, _ = GEOD.fwd(lons, lats, bearings, distances)
    coords = list(zip(out_lon.tolist(), out_lat.tolist()))
    coords.append(coords[0])
    return Polygon(coords)


def geodesic_area_sq_m(geom) -> float:
    if geom.is_empty:
        return 0.0
    area, _ = GEOD.geometry_area_perimeter(geom)
    return abs(float(area))


def fetch_boundaries() -> gpd.GeoDataFrame:
    target = CACHE / "states.zip"
    if not target.exists():
        r = session().get(STATE_BOUNDARIES, timeout=300)
        r.raise_for_status()
        target.write_bytes(r.content)
    states = gpd.read_file(target)
    states = states[states["STUSPS"].isin(STATE_FIPS)].to_crs(4326)
    return states[["STATEFP", "STUSPS", "NAME", "geometry"]].copy()


def census_json(params: list[tuple[str, str]], timeout: int = 300) -> list[list[str]]:
    last_error: Exception | None = None
    for attempt in range(8):
        try:
            r = session().get(
                CENSUS_API,
                params=params + [("_", str(time.time_ns()))],
                timeout=timeout,
                headers={"Accept": "application/json", "Cache-Control": "no-cache"},
            )
            r.raise_for_status()
            if not r.content.strip():
                raise RuntimeError(f"Census API returned an empty HTTP {r.status_code} response")
            data = r.json()
            if not isinstance(data, list) or not data:
                raise RuntimeError(f"Unexpected Census API payload: {str(data)[:200]}")
            return data
        except Exception as exc:
            last_error = exc
            if attempt == 7:
                break
            delay = min(2 ** attempt, 20)
            print(f"Census API retry {attempt + 1}/7 in {delay}s: {exc}")
            time.sleep(delay)
    raise RuntimeError(f"Census API failed after 8 attempts: {last_error}")


def county_block_population(state_fips: str, county_fips: str) -> pd.DataFrame:
    params = [
        ("get", "P1_001N"), ("for", "block:*"), ("in", f"state:{state_fips}"),
        ("in", f"county:{county_fips}"), ("in", "tract:*"),
    ]
    try:
        data = census_json(params)
        return pd.DataFrame(data[1:], columns=data[0])
    except Exception:
        tracts = census_json([
            ("get", "NAME"), ("for", "tract:*"), ("in", f"state:{state_fips}"),
            ("in", f"county:{county_fips}"),
        ])
        frames = []
        for tract_row in tracts[1:]:
            tract = tract_row[-1]
            detail = census_json([
                ("get", "P1_001N"), ("for", "block:*"), ("in", f"state:{state_fips}"),
                ("in", f"county:{county_fips}"), ("in", f"tract:{tract}"),
            ])
            frames.append(pd.DataFrame(detail[1:], columns=detail[0]))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_state_population(state_fips: str) -> pd.DataFrame:
    cache_file = CACHE / f"population-{state_fips}.parquet"
    if cache_file.exists():
        return pd.read_parquet(cache_file)

    counties = census_json([
        ("get", "NAME"), ("for", "county:*"), ("in", f"state:{state_fips}")
    ])
    county_ids = [row[-1] for row in counties[1:]]
    frames = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(county_block_population, state_fips, county): county
            for county in county_ids
        }
        for future in as_completed(futures):
            frames.append(future.result())
    pop = pd.concat(frames, ignore_index=True)
    pop["GEOID20"] = pop["state"] + pop["county"] + pop["tract"] + pop["block"]
    pop["population"] = pd.to_numeric(pop["P1_001N"], errors="coerce").fillna(0).astype("int64")
    pop = pop[["GEOID20", "population"]]
    pop.to_parquet(cache_file, index=False)
    return pop


def download_block_zip(state_fips: str) -> Path:
    target = CACHE / f"blocks-{state_fips}.zip"
    if target.exists():
        return target
    url = BLOCK_URL.format(fips=state_fips)
    print(f"Downloading Census blocks for {state_fips}: {url}")
    with session().get(url, stream=True, timeout=900) as r:
        r.raise_for_status()
        with target.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    return target


def population_coverage(coverage, states: gpd.GeoDataFrame) -> pd.DataFrame:
    rows = []
    for _, state_row in states.sort_values("STATEFP").iterrows():
        fips = state_row.STATEFP
        code = state_row.STUSPS
        state_geom = state_row.geometry
        state_coverage = coverage.intersection(state_geom)
        if state_coverage.is_empty:
            rows.append({
                "state": code, "population_total_2020": 0,
                "population_covered_2020": 0, "population_coverage_pct": 0.0,
            })
            continue

        block_zip = download_block_zip(fips)
        blocks = gpd.read_file(
            f"zip://{block_zip}",
            columns=["GEOID20", "INTPTLAT20", "INTPTLON20"],
            engine="pyogrio",
        )
        pop = fetch_state_population(fips)
        blocks = blocks.merge(pop, on="GEOID20", how="left")
        blocks["population"] = blocks["population"].fillna(0).astype("int64")
        x = pd.to_numeric(blocks["INTPTLON20"], errors="coerce").to_numpy()
        y = pd.to_numeric(blocks["INTPTLAT20"], errors="coerce").to_numpy()
        covered = contains_xy(state_coverage, x, y)
        total = int(blocks["population"].sum())
        inside = int(blocks.loc[covered, "population"].sum())
        rows.append({
            "state": code,
            "population_total_2020": total,
            "population_covered_2020": inside,
            "population_coverage_pct": (100.0 * inside / total) if total else 0.0,
        })
        print(f"{code}: {inside:,}/{total:,} people ({100*inside/total:.2f}%)")
        block_zip.unlink(missing_ok=True)
        (CACHE / f"population-{fips}.parquet").unlink(missing_ok=True)

    return pd.DataFrame(rows)


def land_coverage(coverage, states: gpd.GeoDataFrame) -> pd.DataFrame:
    rows = []
    for _, state_row in states.iterrows():
        land = state_row.geometry
        inside = coverage.intersection(land)
        total_sq_m = geodesic_area_sq_m(land)
        covered_sq_m = geodesic_area_sq_m(inside)
        rows.append({
            "state": state_row.STUSPS,
            "land_sq_miles": total_sq_m / 2_589_988.110336,
            "covered_land_sq_miles": covered_sq_m / 2_589_988.110336,
            "land_coverage_pct": 100.0 * covered_sq_m / total_sq_m if total_sq_m else 0.0,
        })
    return pd.DataFrame(rows)


def make_map(stores: pd.DataFrame, coverage, states: gpd.GeoDataFrame, summary: dict[str, Any]) -> None:
    m = folium.Map(
        location=[39.2, -98.5], zoom_start=4, tiles="CartoDB positron",
        control_scale=True, prefer_canvas=True,
    )
    title = f"""
    <div style="position:fixed;top:12px;left:50%;transform:translateX(-50%);
    z-index:9999;background:white;padding:10px 16px;border-radius:8px;
    box-shadow:0 2px 8px #777;font-family:Arial">
    <b>CVS Six-Mile National Coverage</b><br>
    <span style="font-size:12px">{summary['store_count']:,} stores ·
    {summary['population_coverage_pct']:.2f}% of 2020 population ·
    {summary['land_coverage_pct']:.2f}% of U.S. land</span></div>
    """
    m.get_root().html.add_child(folium.Element(title))

    folium.GeoJson(
        states, name="State boundaries",
        style_function=lambda _: {"color": "#777", "weight": 0.6, "fillOpacity": 0},
    ).add_to(m)

    simplified = coverage.simplify(0.002, preserve_topology=True)
    folium.GeoJson(
        mapping(simplified), name="Merged six-mile coverage", show=True,
        style_function=lambda _: {
            "color": "#b00020", "weight": 0.8, "fillColor": "#e31837", "fillOpacity": 0.35
        },
    ).add_to(m)

    circles = folium.FeatureGroup(name="Individual six-mile circles", show=False)
    for row in stores.itertuples():
        folium.Circle(
            location=[row.latitude, row.longitude], radius=RADIUS_M,
            color="#e31837", weight=0.45, fill=True, fill_opacity=0.04,
        ).add_to(circles)
    circles.add_to(m)

    markers = [[float(r.latitude), float(r.longitude)] for r in stores.itertuples()]
    FastMarkerCluster(markers, name="CVS stores", show=True).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(OUT / "cvs-six-mile-map.html")


def main() -> None:
    started = datetime.now(timezone.utc)
    stores = scrape_cvs()
    stores = census_batch_geocode(stores)
    stores = stores.dropna(subset=["latitude", "longitude"]).copy()
    stores["latitude"] = stores["latitude"].astype(float)
    stores["longitude"] = stores["longitude"].astype(float)
    stores.to_csv(OUT / "cvs-locations.csv", index=False)

    print(f"Building {len(stores):,} exact geodesic circles")
    circles = [
        geodesic_circle(row.longitude, row.latitude, RADIUS_M)
        for row in stores.itertuples()
    ]
    coverage = unary_union(circles)
    states = fetch_boundaries()

    land = land_coverage(coverage, states)
    population = population_coverage(coverage, states)
    state_report = land.merge(population, on="state", how="outer")
    state_report = state_report.sort_values("state")
    state_report.to_csv(OUT / "state-coverage.csv", index=False)

    population_total = int(state_report["population_total_2020"].sum())
    population_covered = int(state_report["population_covered_2020"].sum())
    land_total = float(state_report["land_sq_miles"].sum())
    land_covered = float(state_report["covered_land_sq_miles"].sum())

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_snapshot_started_utc": started.isoformat(),
        "store_source": os.environ.get("CVS_DATA_URL", CVS_DIRECTORY),
        "store_snapshot_date": os.environ.get("CVS_DATA_DATE", "live"),
        "store_source_attribution": os.environ.get("CVS_DATA_ATTRIBUTION", "CVS directory"),
        "store_count": int(len(stores)),
        "radius_miles": 6.0,
        "radius_meters": RADIUS_M,
        "distance_method": "WGS84 geodesic circles with 72 vertices per store",
        "overlap_method": "All store circles dissolved; overlap counted once",
        "population_method": (
            "2020 Decennial Census P1 population assigned at each Census block internal point"
        ),
        "population_total_2020": population_total,
        "population_covered_2020": population_covered,
        "population_coverage_pct": 100.0 * population_covered / population_total,
        "land_boundary_source": STATE_BOUNDARIES,
        "land_sq_miles": land_total,
        "covered_land_sq_miles": land_covered,
        "land_coverage_pct": 100.0 * land_covered / land_total,
        "coordinate_completeness_pct": 100.0,
    }
    (OUT / "coverage-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    make_map(stores, coverage, states, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
