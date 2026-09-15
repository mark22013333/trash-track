from __future__ import annotations

import logging
import math
import os
import ssl
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
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
ROUTE_CACHE_SECONDS = 43_200
TRUCK_CACHE_SECONDS = 120
FAILED_REFRESH_RETRY_SECONDS = 30
GPS_DELAY_SECONDS = 600
HTTP_TIMEOUT = (5, 15)
PAGE_SIZE = 1_000
MAX_PAGES = 100
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

WEEKDAY_SUFFIXES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("trash-track")


class OpenDataError(RuntimeError):
    """官方開放資料暫時無法使用。"""


class CompatibleTLSAdapter(HTTPAdapter):
    """保留憑證驗證，但相容缺少非關鍵 X.509 延伸欄位的政府憑證鏈。"""

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


class OpenDataService:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.mount("https://", CompatibleTLSAdapter())
        self.session.headers.update({"User-Agent": "TrashTrack/1.0 (+local Flask app)"})
        self._route_cache: dict[str, CacheEntry] = {}
        self._truck_cache: CacheEntry | None = None
        self._route_failed_at: dict[str, datetime] = {}
        self._truck_failed_at: datetime | None = None
        self._lock = threading.RLock()
        self.route_api_available: bool | None = None
        self.truck_api_available: bool | None = None
        self.route_last_error: str | None = None
        self.truck_last_error: str | None = None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(TAIPEI_TZ)

    def _fetch_pages(
        self, url: str, *, filter_expression: str | None = None
    ) -> list[dict[str, Any]]:
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
                raise OpenDataError(f"請求 {url} 第 {page} 頁失敗：{exc}") from exc

            if not isinstance(payload, list):
                raise OpenDataError(f"請求 {url} 回傳格式不是 JSON 陣列")

            valid_rows = [row for row in payload if isinstance(row, dict)]
            rows.extend(valid_rows)
            if len(payload) < PAGE_SIZE:
                return rows

        raise OpenDataError(f"請求 {url} 超過安全分頁上限 {MAX_PAGES}")

    def get_district_routes(self, district: str) -> tuple[list[dict[str, Any]], bool]:
        now = self._now()
        with self._lock:
            cached = self._route_cache.get(district)
            if cached and (now - cached.fetched_at).total_seconds() < ROUTE_CACHE_SECONDS:
                return cached.data, False
            failed_at = self._route_failed_at.get(district)
            if (
                cached
                and failed_at
                and (now - failed_at).total_seconds() < FAILED_REFRESH_RETRY_SECONDS
            ):
                return cached.data, True

            try:
                rows = self._fetch_pages(ROUTE_API_URL, filter_expression=f"city eq {district}")
                # 若官方未套用 filter，退回全資料分頁後由 Python 篩選。
                if rows and any(clean_text(row.get("city")) != district for row in rows):
                    logger.warning("路線 API 未正確套用 city filter，改由 Python 端篩選")
                    rows = [
                        row
                        for row in self._fetch_pages(ROUTE_API_URL)
                        if clean_text(row.get("city")) == district
                    ]
                self._route_cache[district] = CacheEntry(rows, now)
                self.route_api_available = True
                self.route_last_error = None
                self._route_failed_at.pop(district, None)
                logger.info("已更新 %s 路線快取，共 %d 筆站點", district, len(rows))
                return rows, False
            except OpenDataError as exc:
                self.route_api_available = False
                self.route_last_error = str(exc)
                self._route_failed_at[district] = now
                logger.warning("路線資料更新失敗：%s", exc)
                if cached:
                    return cached.data, True
                raise

    def get_trucks(self) -> tuple[list[dict[str, Any]], bool]:
        now = self._now()
        with self._lock:
            cached = self._truck_cache
            if cached and (now - cached.fetched_at).total_seconds() < TRUCK_CACHE_SECONDS:
                return cached.data, False
            if (
                cached
                and self._truck_failed_at
                and (now - self._truck_failed_at).total_seconds()
                < FAILED_REFRESH_RETRY_SECONDS
            ):
                return cached.data, True

            try:
                rows = self._fetch_pages(TRUCK_API_URL)
                self._truck_cache = CacheEntry(rows, now)
                self.truck_api_available = True
                self.truck_last_error = None
                self._truck_failed_at = None
                logger.info("已更新垃圾車 GPS 快取，共 %d 台", len(rows))
                return rows, False
            except OpenDataError as exc:
                self.truck_api_available = False
                self.truck_last_error = str(exc)
                self._truck_failed_at = now
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
                "truckCacheTime": iso_time(self._truck_cache.fetched_at)
                if self._truck_cache
                else None,
                "routeApiAvailable": self.route_api_available,
                "truckApiAvailable": self.truck_api_available,
                "routeLastError": self.route_last_error,
                "truckLastError": self.truck_last_error,
                "routeCacheSeconds": ROUTE_CACHE_SECONDS,
                "truckCacheSeconds": TRUCK_CACHE_SECONDS,
            }


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


def service_flags(rows: list[dict[str, Any]]) -> dict[str, bool]:
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


def find_routes(
    district_rows: list[dict[str, Any]], street: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    all_by_line: dict[str, list[dict[str, Any]]] = defaultdict(list)
    matching_line_ids: set[str] = set()

    for row in district_rows:
        line_id = clean_text(row.get("lineid"))
        if not line_id:
            continue
        all_by_line[line_id].append(row)
        if street in clean_text(row.get("name")):
            matching_line_ids.add(line_id)

    routes: list[dict[str, Any]] = []
    for line_id in sorted(matching_line_ids):
        all_rows = sorted(all_by_line[line_id], key=lambda row: parse_rank(row.get("rank")))
        matching_rows = [row for row in all_rows if street in clean_text(row.get("name"))]
        routes.append(
            {
                "lineid": line_id,
                "linename": clean_text(all_rows[0].get("linename")) if all_rows else "",
                "today": service_flags(matching_rows or all_rows),
                "stops": [serialize_stop(row) for row in matching_rows],
                "totalRouteStops": len(all_rows),
            }
        )

    return routes, all_by_line


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def estimate_next_stop(
    latitude: float, longitude: float, route_rows: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, float | None]:
    candidates = []
    for row in route_rows:
        stop_lat = parse_float(row.get("latitude"))
        stop_lon = parse_float(row.get("longitude"))
        if stop_lat is None or stop_lon is None:
            continue
        distance = haversine_meters(latitude, longitude, stop_lat, stop_lon)
        candidates.append((distance, parse_rank(row.get("rank")), row))

    if not candidates:
        return None, None

    nearest_distance, nearest_rank, _ = min(candidates, key=lambda item: item[0])
    later = [item for item in candidates if item[1] > nearest_rank]
    if not later:
        return None, nearest_distance

    _, _, next_row = min(later, key=lambda item: item[1])
    return serialize_stop(next_row), nearest_distance


def validated_search() -> tuple[str, str]:
    district = request.args.get("district", DEFAULT_DISTRICT).strip()
    street = request.args.get("street", DEFAULT_STREET).strip()
    if not district or not street:
        raise ValueError("行政區與道路名稱不可空白")
    if len(district) > 20 or len(street) > 50:
        raise ValueError("行政區或道路名稱過長")
    return district, street


app = Flask(__name__)
data_service = OpenDataService()


@app.get("/")
def index() -> str:
    return render_template(
        "index.html", default_district=DEFAULT_DISTRICT, default_street=DEFAULT_STREET
    )


@app.get("/api/routes")
def api_routes():
    try:
        district, street = validated_search()
        district_rows, stale = data_service.get_district_routes(district)
        routes, _ = find_routes(district_rows, street)
        return jsonify(
            {
                "district": district,
                "street": street,
                "routeCount": len(routes),
                "routes": routes,
                "updatedAt": iso_time(datetime.now(TAIPEI_TZ)),
                "stale": stale,
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得垃圾車路線資料，請稍後再試。"}), 503


@app.get("/api/trucks")
def api_trucks():
    try:
        district, street = validated_search()
        district_rows, route_stale = data_service.get_district_routes(district)
        routes, all_by_line = find_routes(district_rows, street)
        route_names = {route["lineid"]: route["linename"] for route in routes}
        active_rows, truck_stale = data_service.get_trucks()
        now = datetime.now(TAIPEI_TZ)
        trucks = []

        for row in active_rows:
            line_id = clean_text(row.get("lineid"))
            if line_id not in route_names:
                continue
            latitude = parse_float(row.get("latitude"))
            longitude = parse_float(row.get("longitude"))
            if latitude is None or longitude is None:
                continue
            gps_time = parse_gps_time(row.get("time"))
            next_stop, nearest_distance = estimate_next_stop(
                latitude, longitude, all_by_line.get(line_id, [])
            )
            age_seconds = (now - gps_time).total_seconds() if gps_time else None
            trucks.append(
                {
                    "lineid": line_id,
                    "linename": route_names[line_id],
                    "car": clean_text(row.get("car")),
                    "gpsTime": iso_time(gps_time),
                    "location": clean_text(row.get("location")),
                    "latitude": latitude,
                    "longitude": longitude,
                    "estimatedNextStop": next_stop,
                    "nearestStopDistanceMeters": round(nearest_distance)
                    if nearest_distance is not None
                    else None,
                    "gpsDelayed": age_seconds is None or age_seconds > GPS_DELAY_SECONDS,
                }
            )

        return jsonify(
            {
                "district": district,
                "street": street,
                "routeCount": len(routes),
                "updatedAt": iso_time(now),
                "truckCount": len(trucks),
                "trucks": trucks,
                "stale": route_stale or truck_stale,
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OpenDataError:
        return jsonify({"error": "目前無法取得最新垃圾車資料，請稍後再試。"}), 503


@app.get("/api/status")
def api_status():
    return jsonify(data_service.status())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
