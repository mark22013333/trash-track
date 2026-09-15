from __future__ import annotations

import logging
import math
import os
import ssl
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template, request
from requests.adapters import HTTPAdapter

OPEN_DATA_BASE_URL = "https://data.ntpc.gov.tw"
ROUTE_DATASET_ID = "edc3ad26-8ae7-4916-a00b-bc6048d19bf8"
TRUCK_DATASET_ID = "28ab4122-60e1-4065-98e5-abccb69aaca6"
ROUTE_API_URL = f"{OPEN_DATA_BASE_URL}/api/datasets/{ROUTE_DATASET_ID}/json"
TRUCK_API_URL = f"{OPEN_DATA_BASE_URL}/api/datasets/{TRUCK_DATASET_ID}/json"
DEFAULT_DISTRICT = "新莊區"
DEFAULT_STREET = "西盛街"
LOCAL_PORT = int(os.getenv("PORT", "5000"))
ROUTE_CACHE_SECONDS = 43_200
TRUCK_CACHE_SECONDS = 120
FAILED_REFRESH_RETRY_SECONDS = 30
GPS_DELAY_SECONDS = 600
HTTP_TIMEOUT = (5, 15)
PAGE_SIZE = 1_000
MAX_PAGES = 100
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
WEEKDAY_SUFFIXES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
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
    def __init__(self) -> None:
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
                "googleMapsApiKeyConfigured": bool(os.getenv("GOOGLE_MAPS_API_KEY", "").strip()),
                "googleMapsMapIdConfigured": bool(os.getenv("GOOGLE_MAPS_MAP_ID", "").strip()),
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
data_service = OpenDataService()


@app.get("/")
def index() -> str:
    return render_template(
        "index.html", default_district=DEFAULT_DISTRICT, default_street=DEFAULT_STREET,
        google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY", "").strip(),
        google_maps_map_id=os.getenv("GOOGLE_MAPS_MAP_ID", "").strip(),
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


@app.get("/api/status")
def api_status():
    return jsonify(data_service.status())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=LOCAL_PORT, debug=os.getenv("FLASK_DEBUG") == "1")
