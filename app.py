from __future__ import annotations

import logging
import math
import os
import ssl
import threading
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template, request
from requests.adapters import HTTPAdapter

from config import get_settings
from location_service import (
    OfficialRoadService,
    SQLiteStore,
    extract_road_lane,
    index_location_options,
    normalize_address,
)
from live_tracking import CameraService, TrackStore
from region_catalog import (
    COUNTY_CODE_BY_NAME,
    district_exists,
    normalize_taiwan_name,
    regions_payload,
)

SETTINGS = get_settings()

OPEN_DATA_BASE_URL = "https://data.ntpc.gov.tw"
ROUTE_DATASET_ID = "edc3ad26-8ae7-4916-a00b-bc6048d19bf8"
TRUCK_DATASET_ID = "28ab4122-60e1-4065-98e5-abccb69aaca6"
ROUTE_API_URL = f"{OPEN_DATA_BASE_URL}/api/datasets/{ROUTE_DATASET_ID}/json"
TRUCK_API_URL = f"{OPEN_DATA_BASE_URL}/api/datasets/{TRUCK_DATASET_ID}/json"
DEFAULT_COUNTY = SETTINGS.default_county
DEFAULT_DISTRICT = SETTINGS.default_district
DEFAULT_STREET = SETTINGS.default_street
LOCAL_PORT = SETTINGS.port
ROUTE_CACHE_SECONDS = SETTINGS.route_cache_seconds
TRUCK_CACHE_SECONDS = SETTINGS.truck_cache_seconds
FAILED_REFRESH_RETRY_SECONDS = SETTINGS.failed_refresh_retry_seconds
GPS_DELAY_SECONDS = SETTINGS.gps_delay_seconds
HTTP_TIMEOUT = (SETTINGS.http_connect_timeout, SETTINGS.http_read_timeout)
PAGE_SIZE = 1_000
MAX_PAGES = 100
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
WEEKDAY_SUFFIXES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
)

logging.basicConfig(
    level=SETTINGS.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("trash-track")


class OpenDataError(RuntimeError):
    """官方開放資料暫時無法使用。"""


class CompatibleTLSAdapter(HTTPAdapter):
    """保留憑證驗證，但相容政府憑證鏈缺少的非關鍵延伸欄位。"""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        context = ssl.create_default_context(cafile=requests.certs.where())
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            context.verify_flags &= ~strict_flag
        pool_kwargs["ssl_context"] = context
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


@dataclass
class CacheEntry:
    data: list[dict[str, Any]]
    fetched_at: datetime


@dataclass
class RouteIndex:
    rows: list[dict[str, Any]]
    routes_by_lineid: dict[str, tuple[dict[str, Any], ...]]
    route_summaries: tuple[dict[str, Any], ...]
    street_lineids: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass
class RouteCacheEntry:
    index: RouteIndex
    fetched_at: datetime


def clean_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parse_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def parse_rank(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 999_999


def normalized_time(value: Any) -> str:
    text = clean_text(value)
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        return ""


def parse_gps_time(value: Any) -> datetime | None:
    text = clean_text(value)
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=TAIPEI_TZ)
        except ValueError:
            continue
    return None


def iso_time(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def is_service_day(value: Any) -> bool:
    return clean_text(value).lower() in {"y", "yes", "1", "true", "是", "有"}


def service_flags(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, bool]:
    # 每次回應依台灣當日計算，避免 12 小時路線快取跨日後沿用昨天結果。
    suffix = WEEKDAY_SUFFIXES[datetime.now(TAIPEI_TZ).weekday()]
    return {
        "garbage": any(is_service_day(row.get(f"garbage{suffix}")) for row in rows),
        "recycling": any(is_service_day(row.get(f"recycling{suffix}")) for row in rows),
        "foodScraps": any(is_service_day(row.get(f"foodscraps{suffix}")) for row in rows),
    }


def serialize_stop(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": parse_rank(row.get("rank")),
        "name": clean_text(row.get("name")),
        "village": clean_text(row.get("village")),
        "latitude": parse_float(row.get("latitude")),
        "longitude": parse_float(row.get("longitude")),
        "scheduledTime": clean_text(row.get("time")),
        "memo": clean_text(row.get("memo")),
    }


def build_route_index(rows: list[dict[str, Any]]) -> RouteIndex:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        line_id = clean_text(row.get("lineid"))
        if line_id:
            grouped[line_id].append(row)
    routes_by_lineid: dict[str, tuple[dict[str, Any], ...]] = {}
    summaries: list[dict[str, Any]] = []
    for line_id, route_rows in grouped.items():
        ordered = tuple(sorted(route_rows, key=lambda row: parse_rank(row.get("rank"))))
        routes_by_lineid[line_id] = ordered
        times = sorted(time for row in ordered if (time := normalized_time(row.get("time"))))
        summaries.append({
            "lineid": line_id,
            "linename": clean_text(ordered[0].get("linename")) if ordered else "",
            "stopCount": len(ordered),
            "firstTime": times[0] if times else None,
            "lastTime": times[-1] if times else None,
        })
    summaries.sort(key=lambda item: (item["linename"].casefold(), item["lineid"].casefold()))
    return RouteIndex(rows, routes_by_lineid, tuple(summaries))


def serialize_route(
    line_id: str, rows: tuple[dict[str, Any], ...], *, street: str | None = None
) -> dict[str, Any]:
    visible_rows = (
        [row for row in rows if street in clean_text(row.get("name"))]
        if street else list(rows)
    )
    return {
        "routeKey": f"newtaipei:{line_id}",
        "lineid": line_id,
        "linename": clean_text(rows[0].get("linename")) if rows else "",
        "today": service_flags(rows),
        "stops": [serialize_stop(row) for row in visible_rows],
        "totalRouteStops": len(rows),
    }


def deduplicate_stops(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    physical: dict[str, dict[str, Any]] = {}
    for route in routes:
        for stop in route["stops"]:
            latitude, longitude = stop.get("latitude"), stop.get("longitude")
            if latitude is None or longitude is None:
                continue
            normalized_name = " ".join(clean_text(stop.get("name")).split()).casefold()
            key = f"{latitude:.6f}|{longitude:.6f}|{normalized_name}"
            item = physical.setdefault(key, {
                "name": stop["name"], "village": stop["village"],
                "latitude": latitude, "longitude": longitude, "appearances": [],
            })
            appearance = {
                "lineid": route["lineid"], "linename": route["linename"],
                "rank": stop["rank"], "scheduledTime": stop["scheduledTime"],
            }
            if appearance not in item["appearances"]:
                item["appearances"].append(appearance)
    for item in physical.values():
        item["appearances"].sort(key=lambda value: (value["lineid"], value["rank"]))
    return list(physical.values())


class OpenDataService:
    def __init__(self, store: SQLiteStore | None = None) -> None:
        self.store = store
        self.session = requests.Session()
        self.session.mount("https://", CompatibleTLSAdapter())
        self.session.headers.update({"User-Agent": "TrashTrack/2.0 (+local Flask app)"})
        self._route_cache: dict[str, RouteCacheEntry | CacheEntry] = {}
        self._truck_cache: CacheEntry | None = None
        self._route_failed_at: dict[str, datetime] = {}
        self._truck_failed_at: datetime | None = None
        self._lock = threading.RLock()
        self._route_refresh_locks: dict[str, threading.Lock] = {}
        self._truck_refresh_lock = threading.Lock()
        self.route_api_available: bool | None = None
        self.truck_api_available: bool | None = None
        self.route_last_error: str | None = None
        self.truck_last_error: str | None = None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(TAIPEI_TZ)

    def _fetch_pages(self, url: str, *, filter_expression: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            params: dict[str, Any] = {"page": page, "size": PAGE_SIZE}
            if filter_expression:
                params["$filter"] = filter_expression
            try:
                response = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise OpenDataError(f"請求官方資料第 {page} 頁失敗：{exc}") from exc
            if not isinstance(payload, list):
                raise OpenDataError("官方資料回傳格式不是 JSON 陣列")
            rows.extend(row for row in payload if isinstance(row, dict))
            if len(payload) < PAGE_SIZE:
                return rows
        raise OpenDataError(f"官方資料超過安全分頁上限 {MAX_PAGES}")

    @staticmethod
    def _entry_index(entry: RouteCacheEntry | CacheEntry) -> RouteIndex:
        return entry.index if isinstance(entry, RouteCacheEntry) else build_route_index(entry.data)

    def get_route_index(self, district: str) -> tuple[RouteIndex, bool]:
        now = self._now()
        with self._lock:
            cached = self._route_cache.get(district)
            if not cached and self.store:
                persisted = self.store.get(f"routes:newtaipei:{district}", SETTINGS.stale_cache_seconds)
                if persisted:
                    rows, fetched_at = persisted
                    cached = RouteCacheEntry(build_route_index(rows), fetched_at.astimezone(TAIPEI_TZ))
                    self._route_cache[district] = cached
            if cached and (now - cached.fetched_at).total_seconds() < ROUTE_CACHE_SECONDS:
                return self._entry_index(cached), False
            failed_at = self._route_failed_at.get(district)
            if cached and failed_at and (now - failed_at).total_seconds() < FAILED_REFRESH_RETRY_SECONDS:
                return self._entry_index(cached), True
            refresh_lock = self._route_refresh_locks.setdefault(district, threading.Lock())

        # 網路與索引建立不持有全域鎖；同一行政區仍只會有一個更新作業。
        with refresh_lock:
            now = self._now()
            with self._lock:
                cached = self._route_cache.get(district)
                if cached and (now - cached.fetched_at).total_seconds() < ROUTE_CACHE_SECONDS:
                    return self._entry_index(cached), False
                failed_at = self._route_failed_at.get(district)
                if cached and failed_at and (now - failed_at).total_seconds() < FAILED_REFRESH_RETRY_SECONDS:
                    return self._entry_index(cached), True
            try:
                rows = self._fetch_pages(ROUTE_API_URL, filter_expression=f"city eq {district}")
                if rows and any(clean_text(row.get("city")) != district for row in rows):
                    logger.warning("路線 API 未正確套用 city filter，改由 Python 端篩選")
                    rows = [row for row in self._fetch_pages(ROUTE_API_URL)
                            if clean_text(row.get("city")) == district]
                index = build_route_index(rows)
                with self._lock:
                    self._route_cache[district] = RouteCacheEntry(index, now)
                    self.route_api_available = True
                    self.route_last_error = None
                    self._route_failed_at.pop(district, None)
                if self.store:
                    self.store.set(f"routes:newtaipei:{district}", rows)
                logger.info("已更新 %s 路線快取，共 %d 筆站點", district, len(rows))
                return index, False
            except OpenDataError as exc:
                with self._lock:
                    self.route_api_available = False
                    self.route_last_error = str(exc)
                    self._route_failed_at[district] = now
                    cached = self._route_cache.get(district)
                logger.warning("路線資料更新失敗：%s", exc)
                if cached:
                    return self._entry_index(cached), True
                raise

    def get_district_routes(self, district: str) -> tuple[list[dict[str, Any]], bool]:
        index, stale = self.get_route_index(district)
        return index.rows, stale

    def lineids_for_street(self, index: RouteIndex, street: str) -> tuple[str, ...]:
        with self._lock:
            cached = index.street_lineids.get(street)
        if cached is not None:
            return cached
        matches = tuple(line_id for line_id, rows in index.routes_by_lineid.items()
                        if any(street in clean_text(row.get("name")) for row in rows))
        with self._lock:
            return index.street_lineids.setdefault(street, matches)

    def get_trucks(self) -> tuple[list[dict[str, Any]], bool]:
        now = self._now()
        with self._lock:
            cached = self._truck_cache
            if not cached and self.store:
                persisted = self.store.get("trucks:newtaipei", SETTINGS.stale_cache_seconds)
                if persisted:
                    rows, fetched_at = persisted
                    cached = CacheEntry(rows, fetched_at.astimezone(TAIPEI_TZ))
                    self._truck_cache = cached
            if cached and (now - cached.fetched_at).total_seconds() < TRUCK_CACHE_SECONDS:
                return cached.data, False
            if cached and self._truck_failed_at and (now - self._truck_failed_at).total_seconds() < FAILED_REFRESH_RETRY_SECONDS:
                return cached.data, True
        with self._truck_refresh_lock:
            now = self._now()
            with self._lock:
                cached = self._truck_cache
                if cached and (now - cached.fetched_at).total_seconds() < TRUCK_CACHE_SECONDS:
                    return cached.data, False
                if cached and self._truck_failed_at and (now - self._truck_failed_at).total_seconds() < FAILED_REFRESH_RETRY_SECONDS:
                    return cached.data, True
            try:
                rows = self._fetch_pages(TRUCK_API_URL)
                with self._lock:
                    self._truck_cache = CacheEntry(rows, now)
                    self.truck_api_available = True
                    self.truck_last_error = None
                    self._truck_failed_at = None
                if self.store:
                    self.store.set("trucks:newtaipei", rows)
                logger.info("已更新垃圾車 GPS 快取，共 %d 台", len(rows))
                return rows, False
            except OpenDataError as exc:
                with self._lock:
                    self.truck_api_available = False
                    self.truck_last_error = str(exc)
                    self._truck_failed_at = now
                    cached = self._truck_cache
                logger.warning("GPS 資料更新失敗：%s", exc)
                if cached:
                    return cached.data, True
                raise

    def status(self) -> dict[str, Any]:
        settings = get_settings()
        with self._lock:
            route_times = [entry.fetched_at for entry in self._route_cache.values()]
            return {
                "status": "ok",
                "routeCacheTime": iso_time(max(route_times)) if route_times else None,
                "truckCacheTime": iso_time(self._truck_cache.fetched_at) if self._truck_cache else None,
                "routeApiAvailable": self.route_api_available,
                "truckApiAvailable": self.truck_api_available,
                "routeLastError": self.route_last_error,
                "truckLastError": self.truck_last_error,
                "routeCacheSeconds": ROUTE_CACHE_SECONDS,
                "truckCacheSeconds": TRUCK_CACHE_SECONDS,
                "googleMapsApiKeyConfigured": bool(settings.google_maps_api_key),
                "googleMapsMapIdConfigured": bool(settings.google_maps_map_id),
                "googlePlacesEnabled": settings.google_places_enabled,
                "googlePlacesAvailable": settings.places_available,
            }


def find_routes(district_rows: list[dict[str, Any]], street: str) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """保留舊有純函式介面，供既有整合程式與測試使用。"""
    index = build_route_index(district_rows)
    line_ids = [line_id for line_id, rows in index.routes_by_lineid.items()
                if any(street in clean_text(row.get("name")) for row in rows)]
    routes = [serialize_route(line_id, index.routes_by_lineid[line_id], street=street)
              for line_id in sorted(line_ids)]
    return routes, {key: list(value) for key, value in index.routes_by_lineid.items()}


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def estimate_next_stop(latitude: float, longitude: float, route_rows) -> tuple[dict[str, Any] | None, float | None]:
    candidates = []
    for row in route_rows:
        stop_lat, stop_lon = parse_float(row.get("latitude")), parse_float(row.get("longitude"))
        if stop_lat is None or stop_lon is None:
            continue
        candidates.append((haversine_meters(latitude, longitude, stop_lat, stop_lon), parse_rank(row.get("rank")), row))
    if not candidates:
        return None, None
    nearest_distance, nearest_rank, _ = min(candidates, key=lambda item: item[0])
    later = [item for item in candidates if item[1] > nearest_rank]
    if not later:
        return None, nearest_distance
    return serialize_stop(min(later, key=lambda item: item[1])[2]), nearest_distance


def predict_next_stop(
    latitude: float,
    longitude: float,
    route_rows,
    gps_time: datetime | None,
    trail: list[dict[str, Any]],
) -> dict[str, Any]:
    """保守推估下一站；資料不足時寧可不猜，也不製造精準 ETA 的錯覺。"""
    candidates = []
    for row in route_rows:
        stop_lat, stop_lon = parse_float(row.get("latitude")), parse_float(row.get("longitude"))
        if stop_lat is None or stop_lon is None:
            continue
        candidates.append((haversine_meters(latitude, longitude, stop_lat, stop_lon), parse_rank(row.get("rank")), row))
    if not candidates:
        return {"nextStop": None, "confidence": "low", "reason": "路線沒有可用座標", "nearestStopDistanceMeters": None}

    nearest_distance, nearest_rank, _ = min(candidates, key=lambda item: item[0])
    age = (datetime.now(TAIPEI_TZ) - gps_time).total_seconds() if gps_time else None
    if age is None or age > GPS_DELAY_SECONDS:
        return {"nextStop": None, "confidence": "low", "reason": "GPS 已超過 10 分鐘，暫時無法可靠推估", "nearestStopDistanceMeters": round(nearest_distance)}
    if nearest_distance > 1_500:
        return {"nextStop": None, "confidence": "low", "reason": "車輛距表定站點線過遠，暫時無法可靠推估", "nearestStopDistanceMeters": round(nearest_distance)}

    direction_known = False
    inferred_rank = nearest_rank
    if len(trail) >= 2:
        previous = trail[-2]
        movement = haversine_meters(
            previous["latitude"], previous["longitude"], latitude, longitude
        )
        previous_candidates = [
            (haversine_meters(previous["latitude"], previous["longitude"], parse_float(row.get("latitude")), parse_float(row.get("longitude"))), parse_rank(row.get("rank")))
            for row in route_rows
            if parse_float(row.get("latitude")) is not None and parse_float(row.get("longitude")) is not None
        ]
        if previous_candidates and movement >= 10:
            previous_rank = min(previous_candidates, key=lambda item: item[0])[1]
            direction_known = nearest_rank >= previous_rank
            inferred_rank = max(nearest_rank, previous_rank)

    if not direction_known:
        return {
            "nextStop": None,
            "confidence": "low",
            "reason": "移動方向資料不足，暫時無法可靠推估",
            "nearestStopDistanceMeters": round(nearest_distance),
        }

    later = [item for item in candidates if item[1] > inferred_rank]
    if not later:
        return {"nextStop": None, "confidence": "low", "reason": "車輛已接近表定路線末端", "nearestStopDistanceMeters": round(nearest_distance)}
    next_stop = serialize_stop(min(later, key=lambda item: item[1])[2])
    if nearest_distance <= 250 and age <= 180 and direction_known:
        confidence = "high"
    elif nearest_distance <= 1_000 and age <= GPS_DELAY_SECONDS:
        confidence = "medium"
    else:
        confidence = "low"
    reason = "依最近站序、移動方向與 GPS 新鮮度推估"
    return {"nextStop": next_stop, "confidence": confidence, "reason": reason, "nearestStopDistanceMeters": round(nearest_distance)}


def validated_query() -> tuple[str, str | None, str | None]:
    district = request.args.get("district", DEFAULT_DISTRICT).strip()
    line_id = request.args.get("lineid", "").strip() or None
    street = request.args.get("street", "").strip() or None
    if not line_id and not street:
        street = DEFAULT_STREET
    if not district:
        raise ValueError("行政區不可空白")
    if len(district) > 20 or (street and len(street) > 50) or (line_id and len(line_id) > 100):
        raise ValueError("查詢條件過長")
    return district, line_id, street


def select_routes(index: RouteIndex, line_id: str | None, street: str | None) -> list[dict[str, Any]]:
    if line_id:
        rows = index.routes_by_lineid.get(line_id)
        return [serialize_route(line_id, rows)] if rows else []
    assert street is not None
    return [serialize_route(value, index.routes_by_lineid[value], street=street)
            for value in data_service.lineids_for_street(index, street)]


def serialize_trucks(active_rows: list[dict[str, Any]], index: RouteIndex, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    route_names = {route["lineid"]: route["linename"] for route in routes}
    now = datetime.now(TAIPEI_TZ)
    trucks = []
    for row in active_rows:
        line_id = clean_text(row.get("lineid"))
        if line_id not in route_names:
            continue
        latitude, longitude = parse_float(row.get("latitude")), parse_float(row.get("longitude"))
        if latitude is None or longitude is None:
            continue
        gps_time = parse_gps_time(row.get("time"))
        next_stop, nearest_distance = estimate_next_stop(latitude, longitude, index.routes_by_lineid.get(line_id, ()))
        age_seconds = (now - gps_time).total_seconds() if gps_time else None
        trucks.append({
            "lineid": line_id, "linename": route_names[line_id], "car": clean_text(row.get("car")),
            "gpsTime": iso_time(gps_time), "location": clean_text(row.get("location")),
            "latitude": latitude, "longitude": longitude, "estimatedNextStop": next_stop,
            "nearestStopDistanceMeters": round(nearest_distance) if nearest_distance is not None else None,
            "gpsDelayed": age_seconds is None or age_seconds > GPS_DELAY_SECONDS,
        })
    return trucks


app = Flask(__name__)
location_store = SQLiteStore(SETTINGS.database_path)
data_service = OpenDataService(location_store)
track_store = TrackStore(SETTINGS.database_path, retention_days=SETTINGS.track_retention_days)
camera_service = CameraService(
    client_id=SETTINGS.tdx_client_id,
    client_secret=SETTINGS.tdx_client_secret,
    cache_seconds=SETTINGS.camera_cache_seconds,
    timeout=HTTP_TIMEOUT,
)
road_service = OfficialRoadService(
    location_store,
    cache_seconds=SETTINGS.road_cache_seconds,
    timeout=HTTP_TIMEOUT,
)


@app.get("/")
def index() -> str:
    settings = get_settings()
    return render_template(
        "index.html",
        default_county=settings.default_county,
        default_district=settings.default_district,
        default_street=settings.default_street,
        google_maps_api_key=settings.google_maps_api_key,
        google_maps_map_id=settings.google_maps_map_id,
        google_places_enabled=settings.google_places_enabled,
        google_maps_language=settings.google_maps_language,
        google_maps_region=settings.google_maps_region,
        default_radius=settings.default_search_radius_meters,
        max_radius=settings.max_search_radius_meters,
        result_limit=settings.nearby_result_limit,
        live_poll_seconds=settings.live_poll_seconds,
        track_retention_days=settings.track_retention_days,
        camera_radius_meters=settings.camera_radius_meters,
    )


@app.get("/api/route-options")
def api_route_options():
    try:
        district = request.args.get("district", DEFAULT_DISTRICT).strip()
        if not district or len(district) > 20:
            raise ValueError("行政區不可空白或超過 20 字")
        index, stale = data_service.get_route_index(district)
        routes = list(index.route_summaries)
        return jsonify({"district": district, "routeCount": len(routes), "routes": routes, "stale": stale})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得垃圾車路線選項，請稍後再試。"}), 503


def route_payload(include_trucks: bool = False) -> dict[str, Any]:
    district, line_id, street = validated_query()
    index, route_stale = data_service.get_route_index(district)
    routes = select_routes(index, line_id, street)
    payload = {
        "district": district, "lineid": line_id, "street": None if line_id else street,
        "selectedRoute": routes[0] if line_id and routes else None,
        "routeCount": len(routes), "routes": routes,
        "physicalStops": deduplicate_stops(routes),
        "updatedAt": iso_time(datetime.now(TAIPEI_TZ)), "stale": route_stale,
    }
    if include_trucks:
        active_rows, truck_stale = data_service.get_trucks()
        trucks = serialize_trucks(active_rows, index, routes)
        payload.update({"truckCount": len(trucks), "trucks": trucks, "stale": route_stale or truck_stale})
    return payload


@app.get("/api/routes")
def api_routes():
    try:
        return jsonify(route_payload())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得垃圾車路線資料，請稍後再試。"}), 503


@app.get("/api/trucks")
def api_trucks():
    try:
        payload = route_payload(include_trucks=True)
        return jsonify({key: value for key, value in payload.items()
                        if key not in {"routes", "physicalStops", "selectedRoute"}})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得最新垃圾車資料，請稍後再試。"}), 503


@app.get("/api/dashboard")
def api_dashboard():
    try:
        return jsonify(route_payload(include_trucks=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得清運儀表板資料，請稍後再試。"}), 503


WEEKDAY_LABELS = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")
MOENV_ROUTE_URL = "https://hwms.moenv.gov.tw/dispPageBox/route/routeCP.aspx?ddsPageID=ROUTE"


def row_service_days(row: dict[str, Any]) -> list[int]:
    return [
        index for index, suffix in enumerate(WEEKDAY_SUFFIXES)
        if is_service_day(row.get(f"garbage{suffix}"))
    ]


def next_service(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    possibilities = []
    for day_offset in range(8):
        date = now.date() + timedelta(days=day_offset)
        weekday = date.weekday()
        for row in rows:
            if weekday not in row_service_days(row):
                continue
            time_text = normalized_time(row.get("time"))
            if not time_text:
                continue
            hour, minute = (int(part) for part in time_text.split(":"))
            scheduled = datetime.combine(date, datetime.min.time()).replace(
                tzinfo=TAIPEI_TZ, hour=hour, minute=minute
            )
            if scheduled >= now:
                possibilities.append((scheduled, row))
        if possibilities:
            scheduled, row = min(possibilities, key=lambda item: item[0])
            return {
                "date": scheduled.date().isoformat(),
                "weekday": WEEKDAY_LABELS[scheduled.weekday()],
                "scheduledTime": scheduled.strftime("%H:%M"),
                "stopName": clean_text(row.get("name")),
            }
    return None


def nearby_payload(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    county = normalize_taiwan_name(clean_text(payload.get("county")))
    district = normalize_taiwan_name(clean_text(payload.get("district")))
    road = normalize_address(payload.get("road"))
    lane = normalize_address(payload.get("lane")) or None
    if county not in COUNTY_CODE_BY_NAME:
        raise ValueError("請選擇有效的縣市")
    if not district_exists(county, district):
        raise ValueError("行政區與縣市不相符")
    if not road or len(road) > 80 or (lane and len(lane) > 40):
        raise ValueError("請輸入有效的道路名稱")

    base = {
        "query": {"county": county, "district": district, "road": road, "lane": lane},
        "capabilities": {
            "schedule": county == "新北市",
            "stopCoordinates": county == "新北市",
            "liveGps": county == "新北市",
        },
        "officialUrl": MOENV_ROUTE_URL,
        "updatedAt": iso_time(datetime.now(TAIPEI_TZ)),
    }
    if county != "新北市":
        return {
            **base,
            "answer": {
                "status": "sourceUnavailable",
                "title": "這個地區請使用官方全國查詢",
                "detail": "行政區與道路已定位，但本站尚未取得可穩定介接的官方清運時刻；這不代表今天沒有垃圾車。",
                "next": None,
            },
            "matchQuality": "unavailable",
            "stops": [],
            "stale": False,
        }

    index, stale = data_service.get_route_index(district)
    latitude = parse_float(payload.get("latitude"))
    longitude = parse_float(payload.get("longitude"))
    requested_limit = payload.get("limit", settings.nearby_result_limit)
    try:
        limit = max(1, min(int(requested_limit), 20))
    except (TypeError, ValueError):
        limit = settings.nearby_result_limit

    matches: list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any]]] = []
    now = datetime.now(TAIPEI_TZ)
    for line_id, rows in index.routes_by_lineid.items():
        linename = clean_text(rows[0].get("linename")) if rows else ""
        for row in rows:
            name = normalize_address(row.get("name"))
            parsed_road, parsed_lane = extract_road_lane(name)
            if road != parsed_road and road not in name:
                continue
            exact_lane = bool(lane and parsed_lane == lane)
            row_lat, row_lon = parse_float(row.get("latitude")), parse_float(row.get("longitude"))
            distance = None
            if latitude is not None and longitude is not None and row_lat is not None and row_lon is not None:
                distance = round(haversine_meters(latitude, longitude, row_lat, row_lon))
                if distance > settings.max_search_radius_meters:
                    continue
            time_text = normalized_time(row.get("time"))
            future_today = False
            if time_text and now.weekday() in row_service_days(row):
                hour, minute = (int(part) for part in time_text.split(":"))
                future_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0) >= now
            item = {
                "routeKey": f"newtaipei:{line_id}",
                "provider": "新北市政府資料開放平臺",
                "lineid": line_id,
                "linename": linename,
                "name": clean_text(row.get("name")),
                "scheduledTime": time_text or None,
                "serviceDays": [WEEKDAY_LABELS[day] for day in row_service_days(row)],
                "today": now.weekday() in row_service_days(row),
                "timePassed": bool(time_text and now.weekday() in row_service_days(row) and not future_today),
                "distanceMeters": distance,
                "latitude": row_lat,
                "longitude": row_lon,
                "matchQuality": "exactLane" if exact_lane else "sameRoad",
            }
            sort_key = (
                0 if exact_lane else 1,
                0 if future_today else 1,
                distance if distance is not None else 99_999_999,
                time_text or "99:99",
                parse_rank(row.get("rank")),
            )
            matches.append((sort_key, row, item))

    matches.sort(key=lambda item: item[0])
    all_matched_rows = [row for _, row, _ in matches]
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    matched_rows: list[dict[str, Any]] = []
    for _, row, item in matches:
        key = (item["routeKey"], item["name"], item["scheduledTime"] or "")
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)
        matched_rows.append(row)
        if len(deduplicated) >= limit:
            break

    if not deduplicated:
        answer = {
            "status": "noMatch",
            "title": "這個地址附近尚未找到清運站",
            "detail": "這不代表今天沒有垃圾車；請改選整條路、嘗試相鄰道路，或使用官方全國查詢。",
            "next": None,
        }
        match_quality = "none"
    else:
        today_rows = [row for row in matched_rows if now.weekday() in row_service_days(row)]
        upcoming = next_service(all_matched_rows, now)
        if today_rows:
            future_today = [item for item in deduplicated if item["today"] and not item["timePassed"]]
            title = "今天有垃圾車" if future_today else "今天有清運，表定時間可能已過"
            detail = "優先列出同巷弄與今天尚未到達的停靠點。" if future_today else "請確認即時車輛資訊，或查看下一個清運日。"
            status = "today"
        else:
            title = "今天沒有表定清運"
            detail = "已為你找到下一個有清運的時間。" if upcoming else "官方資料沒有提供下一班完整時間。"
            status = "notToday"
        answer = {"status": status, "title": title, "detail": detail, "next": upcoming}
        match_quality = "exactLane" if any(item["matchQuality"] == "exactLane" for item in deduplicated) else "sameRoad"

    return {
        **base,
        "answer": answer,
        "matchQuality": match_quality,
        "stops": deduplicated,
        "stale": stale,
    }


@app.get("/api/regions")
def api_regions():
    counties = regions_payload()
    return jsonify({
        "status": "ready",
        "source": "內政部國土測繪中心行政區清單",
        "snapshotDate": "2026-09-15",
        "countyCount": len(counties),
        "districtCount": sum(len(county["districts"]) for county in counties),
        "counties": counties,
    })


@app.get("/api/location-options")
def api_location_options():
    county = normalize_taiwan_name(request.args.get("county", DEFAULT_COUNTY))
    district = normalize_taiwan_name(request.args.get("district", DEFAULT_DISTRICT))
    road = normalize_address(request.args.get("road", ""))
    try:
        if county not in COUNTY_CODE_BY_NAME or not district_exists(county, district):
            raise ValueError("行政區與縣市不相符")
        if county == "新北市":
            index, stale = data_service.get_route_index(district)
            locations = index_location_options(index.rows)
            options = locations.get(road, []) if road else list(locations)
            return jsonify({
                "status": "stale" if stale else "ready",
                "level": "lane" if road else "road",
                "county": county,
                "district": district,
                "road": road or None,
                "options": options,
                "laneSupported": True,
                "source": "新北市政府清運停靠點",
            })
        if road:
            return jsonify({
                "status": "unavailable",
                "level": "lane",
                "county": county,
                "district": district,
                "road": road,
                "options": [],
                "laneSupported": False,
                "message": "此地區尚無可介接的官方清運巷弄資料，可輸入巷弄或改用官方查詢。",
            })
        roads, stale = road_service.roads(county, district)
        return jsonify({
            "status": "stale" if stale else "ready",
            "level": "road",
            "county": county,
            "district": district,
            "options": roads,
            "laneSupported": False,
            "source": "內政部國土測繪中心主要道路清單",
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (requests.RequestException, ET.ParseError) as exc:
        logger.warning("官方道路資料取得失敗：%s", exc)
        return jsonify({"error": "目前無法取得官方道路清單，請直接輸入道路名稱。"}), 503
    except OpenDataError:
        return jsonify({"error": "目前無法取得清運地址選項，請稍後再試。"}), 503


@app.post("/api/nearby")
def api_nearby():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "請使用 JSON 傳送地址查詢條件"}), 400
    try:
        return jsonify(nearby_payload(payload))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得官方清運資料，請稍後再試。"}), 503


def selected_track_date(value: str | None) -> date:
    today = datetime.now(TAIPEI_TZ).date()
    try:
        selected = date.fromisoformat(value) if value else today
    except ValueError as exc:
        raise ValueError("date 必須使用 YYYY-MM-DD 格式") from exc
    earliest = today - timedelta(days=SETTINGS.track_retention_days - 1)
    if selected < earliest or selected > today:
        raise ValueError(f"date 只接受 {earliest.isoformat()} 至 {today.isoformat()}")
    return selected


@app.get("/api/live-route")
def api_live_route():
    try:
        district = request.args.get("district", DEFAULT_DISTRICT).strip()
        line_id = request.args.get("lineid", "").strip()
        if not district or len(district) > 20 or not line_id or len(line_id) > 100:
            raise ValueError("請提供有效的行政區與路線編號")
        selected_date = selected_track_date(request.args.get("date"))
        index, route_stale = data_service.get_route_index(district)
        route_rows = index.routes_by_lineid.get(line_id)
        if not route_rows:
            return jsonify({"error": "找不到指定的清運路線"}), 404

        gps_stale = False
        gps_error = None
        active_rows: list[dict[str, Any]] = []
        try:
            active_rows, gps_stale = data_service.get_trucks()
            track_store.record(active_rows)
        except OpenDataError:
            gps_stale = True
            gps_error = "最新 GPS 暫時無法取得，保留最後有效軌跡。"

        trails = track_store.trails(line_id, selected_date, limit_per_vehicle=500)
        current_by_vehicle: dict[str, dict[str, Any]] = {}
        current_labels: dict[str, str] = {}
        now = datetime.now(TAIPEI_TZ)
        linename = clean_text(route_rows[0].get("linename")) if route_rows else ""
        for row in active_rows:
            if clean_text(row.get("lineid")) != line_id:
                continue
            latitude, longitude = parse_float(row.get("latitude")), parse_float(row.get("longitude"))
            car = clean_text(row.get("car"))
            gps_time = parse_gps_time(row.get("time"))
            if latitude is None or longitude is None or not car:
                continue
            vehicle_id = track_store.vehicle_id_for(car)
            current_labels[vehicle_id] = track_store.display_name_for(car)
            age_seconds = max(0, round((now - gps_time).total_seconds())) if gps_time else None
            current_by_vehicle[vehicle_id] = {
                "gpsTime": iso_time(gps_time),
                "latitude": latitude,
                "longitude": longitude,
                "location": clean_text(row.get("location")),
                "ageSeconds": age_seconds,
                "gpsDelayed": age_seconds is None or age_seconds > GPS_DELAY_SECONDS,
            }

        vehicles = []
        for vehicle_id in sorted(set(trails) | set(current_by_vehicle)):
            trail = trails.get(vehicle_id, [])
            current = current_by_vehicle.get(vehicle_id)
            label = trail[0]["vehicleLabel"] if trail else current_labels.get(vehicle_id, "垃圾車 • 已匿名")
            prediction = (
                predict_next_stop(
                    current["latitude"], current["longitude"], route_rows,
                    datetime.fromisoformat(current["gpsTime"]) if current.get("gpsTime") else None,
                    trail,
                )
                if current else
                {"nextStop": None, "confidence": "low", "reason": "這台車目前未出勤", "nearestStopDistanceMeters": None}
            )
            vehicles.append({
                "vehicleId": vehicle_id,
                "displayName": label,
                "lineid": line_id,
                "linename": linename,
                "current": current,
                "trail": trail,
                "prediction": prediction,
            })

        today = datetime.now(TAIPEI_TZ).date()
        return jsonify({
            "status": "ready" if not (route_stale or gps_stale) else "stale",
            "message": gps_error,
            "district": district,
            "date": selected_date.isoformat(),
            "availableDates": [
                (today - timedelta(days=offset)).isoformat()
                for offset in range(SETTINGS.track_retention_days)
            ],
            "route": serialize_route(line_id, route_rows),
            "vehicles": vehicles,
            "activeVehicleCount": len(current_by_vehicle),
            "updatedAt": iso_time(now),
            "sourceUpdateSeconds": SETTINGS.track_collection_seconds,
            "disclaimer": "表定線僅依官方站序連接，不代表實際行駛道路；距離均為直線距離。",
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得指定路線，請稍後再試。"}), 503


@app.get("/api/cameras/nearby")
def api_cameras_nearby():
    try:
        latitude = parse_float(request.args.get("latitude"))
        longitude = parse_float(request.args.get("longitude"))
        if latitude is None or longitude is None or not (20 <= latitude <= 27 and 118 <= longitude <= 123):
            raise ValueError("請提供有效的臺灣經緯度")
        try:
            radius = int(request.args.get("radiusMeters", SETTINGS.camera_radius_meters))
            limit = int(request.args.get("limit", 3))
        except ValueError as exc:
            raise ValueError("radiusMeters 與 limit 必須是整數") from exc
        if not 100 <= radius <= 3_000:
            raise ValueError("搜尋半徑必須介於 100 至 3000 公尺")
        if not 1 <= limit <= 5:
            raise ValueError("攝影機數量必須介於 1 至 5")
        return jsonify(camera_service.nearby(latitude, longitude, radius_meters=radius, limit=limit))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/status")
def api_status():
    return jsonify({**data_service.status(), **track_store.status(), **camera_service.status()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=LOCAL_PORT, debug=os.getenv("FLASK_DEBUG") == "1")
