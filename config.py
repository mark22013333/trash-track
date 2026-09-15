"""TrashTrack 集中式設定。

本機開發可使用 .env；正式環境變數永遠優先，且祕密不會由此模組寫入檔案。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    if value < minimum or (maximum is not None and value > maximum):
        return default
    return value


@dataclass(frozen=True)
class Settings:
    google_maps_api_key: str
    google_maps_map_id: str
    google_places_enabled: bool
    google_maps_language: str
    google_maps_region: str
    default_county: str
    default_district: str
    default_street: str
    default_search_radius_meters: int
    max_search_radius_meters: int
    nearby_result_limit: int
    route_cache_seconds: int
    truck_cache_seconds: int
    region_cache_seconds: int
    road_cache_seconds: int
    stale_cache_seconds: int
    failed_refresh_retry_seconds: int
    gps_delay_seconds: int
    track_retention_days: int
    track_collection_seconds: int
    live_poll_seconds: int
    tdx_client_id: str
    tdx_client_secret: str
    camera_cache_seconds: int
    camera_radius_meters: int
    http_connect_timeout: int
    http_read_timeout: int
    port: int
    log_level: str
    database_path: Path

    @property
    def maps_configured(self) -> bool:
        return bool(self.google_maps_api_key and self.google_maps_map_id)

    @property
    def places_available(self) -> bool:
        return self.maps_configured and self.google_places_enabled

    @classmethod
    def from_env(cls) -> "Settings":
        default_radius = _int_env("DEFAULT_SEARCH_RADIUS_METERS", 600, minimum=100, maximum=10_000)
        max_radius = _int_env("MAX_SEARCH_RADIUS_METERS", 1_500, minimum=default_radius, maximum=20_000)
        database_path = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "trashtrack.sqlite3")))
        if not database_path.is_absolute():
            database_path = BASE_DIR / database_path
        return cls(
            google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY", "").strip(),
            google_maps_map_id=os.getenv("GOOGLE_MAPS_MAP_ID", "").strip(),
            google_places_enabled=_bool_env("GOOGLE_PLACES_ENABLED", True),
            google_maps_language=os.getenv("GOOGLE_MAPS_LANGUAGE", "zh-TW").strip() or "zh-TW",
            google_maps_region=os.getenv("GOOGLE_MAPS_REGION", "TW").strip() or "TW",
            default_county=os.getenv("DEFAULT_COUNTY", "新北市").strip() or "新北市",
            default_district=os.getenv("DEFAULT_DISTRICT", "新莊區").strip() or "新莊區",
            default_street=os.getenv("DEFAULT_STREET", "西盛街").strip() or "西盛街",
            default_search_radius_meters=default_radius,
            max_search_radius_meters=max_radius,
            nearby_result_limit=_int_env("NEARBY_RESULT_LIMIT", 5, minimum=1, maximum=20),
            route_cache_seconds=_int_env("ROUTE_CACHE_SECONDS", 43_200),
            truck_cache_seconds=_int_env("TRUCK_CACHE_SECONDS", 120),
            region_cache_seconds=_int_env("REGION_CACHE_SECONDS", 604_800),
            road_cache_seconds=_int_env("ROAD_CACHE_SECONDS", 86_400),
            stale_cache_seconds=_int_env("STALE_CACHE_SECONDS", 604_800),
            failed_refresh_retry_seconds=_int_env("FAILED_REFRESH_RETRY_SECONDS", 30),
            gps_delay_seconds=_int_env("GPS_DELAY_SECONDS", 600),
            track_retention_days=_int_env("TRACK_RETENTION_DAYS", 7, minimum=1, maximum=30),
            track_collection_seconds=_int_env("TRACK_COLLECTION_SECONDS", 120, minimum=30, maximum=3_600),
            live_poll_seconds=_int_env("LIVE_POLL_SECONDS", 30, minimum=15, maximum=300),
            tdx_client_id=os.getenv("TDX_CLIENT_ID", "").strip(),
            tdx_client_secret=os.getenv("TDX_CLIENT_SECRET", "").strip(),
            camera_cache_seconds=_int_env("CCTV_CACHE_SECONDS", 21_600, minimum=300, maximum=86_400),
            camera_radius_meters=_int_env("CCTV_SEARCH_RADIUS_METERS", 1_500, minimum=100, maximum=3_000),
            http_connect_timeout=_int_env("HTTP_CONNECT_TIMEOUT", 5, maximum=60),
            http_read_timeout=_int_env("HTTP_READ_TIMEOUT", 15, maximum=120),
            port=_int_env("PORT", 5000, minimum=1, maximum=65_535),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip() or "INFO",
            database_path=database_path,
        )


def get_settings() -> Settings:
    """每次呼叫重新讀取環境變數，方便測試與部署平台動態覆寫。"""
    return Settings.from_env()
