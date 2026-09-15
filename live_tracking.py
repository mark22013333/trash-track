"""垃圾車短期軌跡保存與 TDX 交通攝影機介接。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
PROVIDER = "newtaipei"
TDX_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_CCTV_URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/CCTV/City/NewTaipei"
NEW_TAIPEI_TRAFFIC_URL = "https://atis.ntpc.gov.tw/"


def _parse_gps_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=TAIPEI_TZ)
        except ValueError:
            continue
    return None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _safe_url(value: Any) -> str | None:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return text


def _masked_vehicle_label(value: str) -> str:
    compact = "".join(character for character in value if character.isalnum())
    suffix = compact[-4:] if compact else "未識別"
    return f"垃圾車 • {suffix}"


class TrackStore:
    """保存七日內的官方 GPS 快照，不保存完整車牌。"""

    def __init__(self, path: Path | str, *, retention_days: int = 7):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS app_metadata ("
                "meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS vehicle_positions ("
                "provider TEXT NOT NULL, vehicle_id TEXT NOT NULL, vehicle_label TEXT NOT NULL, "
                "line_id TEXT NOT NULL, gps_time TEXT NOT NULL, observed_at TEXT NOT NULL, "
                "latitude REAL NOT NULL, longitude REAL NOT NULL, location TEXT NOT NULL, "
                "PRIMARY KEY(provider, vehicle_id, gps_time));"
                "CREATE INDEX IF NOT EXISTS idx_vehicle_positions_line_time "
                "ON vehicle_positions(provider, line_id, gps_time);"
                "CREATE TABLE IF NOT EXISTS collector_status ("
                "provider TEXT PRIMARY KEY, last_attempt_at TEXT, last_success_at TEXT, "
                "latest_gps_time TEXT, last_error TEXT, row_count INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS collector_leases ("
                "lease_name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL);"
            )
            row = connection.execute(
                "SELECT meta_value FROM app_metadata WHERE meta_key = 'track_hmac_secret'"
            ).fetchone()
            if not row:
                secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
                connection.execute(
                    "INSERT INTO app_metadata(meta_key, meta_value) VALUES('track_hmac_secret', ?)",
                    (secret,),
                )
            connection.commit()

    def _secret(self, connection: sqlite3.Connection) -> bytes:
        value = connection.execute(
            "SELECT meta_value FROM app_metadata WHERE meta_key = 'track_hmac_secret'"
        ).fetchone()[0]
        return base64.urlsafe_b64decode(value.encode("ascii"))

    @staticmethod
    def new_owner() -> str:
        return uuid.uuid4().hex

    def acquire_lease(self, name: str, owner: str, *, seconds: int, now: datetime | None = None) -> bool:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expires = moment + timedelta(seconds=seconds)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM collector_leases WHERE lease_name = ?", (name,)
            ).fetchone()
            available = not row or row["owner"] == owner or datetime.fromisoformat(row["expires_at"]) <= moment
            if available:
                connection.execute(
                    "INSERT INTO collector_leases(lease_name, owner, expires_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(lease_name) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                    (name, owner, expires.isoformat(timespec="seconds")),
                )
            connection.commit()
            return available

    def release_lease(self, name: str, owner: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM collector_leases WHERE lease_name=? AND owner=?", (name, owner)
            )
            connection.commit()

    def record(self, rows: list[dict[str, Any]], *, observed_at: datetime | None = None) -> int:
        observed = (observed_at or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
        accepted: list[tuple[Any, ...]] = []
        latest: datetime | None = None
        with self._lock, closing(self._connect()) as connection:
            secret = self._secret(connection)
            for row in rows:
                car = str(row.get("car") or "").strip()
                line_id = str(row.get("lineid") or "").strip()
                gps_time = _parse_gps_time(row.get("time"))
                latitude, longitude = _float(row.get("latitude")), _float(row.get("longitude"))
                if not car or not line_id or not gps_time or latitude is None or longitude is None:
                    continue
                if not (20 <= latitude <= 27 and 118 <= longitude <= 123):
                    continue
                vehicle_id = hmac.new(secret, f"{PROVIDER}:{car}".encode(), hashlib.sha256).hexdigest()[:24]
                accepted.append((
                    PROVIDER, vehicle_id, _masked_vehicle_label(car), line_id,
                    gps_time.isoformat(timespec="seconds"), observed.isoformat(timespec="seconds"),
                    latitude, longitude, str(row.get("location") or "").strip(),
                ))
                if latest is None or gps_time > latest:
                    latest = gps_time
            insert_cursor = connection.executemany(
                "INSERT OR IGNORE INTO vehicle_positions("
                "provider, vehicle_id, vehicle_label, line_id, gps_time, observed_at, latitude, longitude, location"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                accepted,
            )
            cutoff = observed.astimezone(timezone.utc) - timedelta(days=self.retention_days)
            connection.execute(
                "DELETE FROM vehicle_positions WHERE julianday(gps_time) < julianday(?)",
                (cutoff.isoformat(timespec="seconds"),),
            )
            connection.execute(
                "INSERT INTO collector_status(provider, last_attempt_at, last_success_at, latest_gps_time, last_error, row_count) "
                "VALUES(?, ?, ?, ?, NULL, ?) ON CONFLICT(provider) DO UPDATE SET "
                "last_attempt_at=excluded.last_attempt_at, last_success_at=excluded.last_success_at, "
                "latest_gps_time=COALESCE(excluded.latest_gps_time, collector_status.latest_gps_time), "
                "last_error=NULL, row_count=excluded.row_count",
                (PROVIDER, observed.isoformat(timespec="seconds"), observed.isoformat(timespec="seconds"),
                 latest.isoformat(timespec="seconds") if latest else None, len(rows)),
            )
            connection.commit()
        return max(0, insert_cursor.rowcount)

    def record_failure(self, message: str, *, attempted_at: datetime | None = None) -> None:
        attempted = (attempted_at or datetime.now(TAIPEI_TZ)).isoformat(timespec="seconds")
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO collector_status(provider, last_attempt_at, last_error) VALUES(?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error",
                (PROVIDER, attempted, message[:500]),
            )
            connection.commit()

    def vehicle_id_for(self, car: str) -> str:
        with self._lock, closing(self._connect()) as connection:
            return hmac.new(
                self._secret(connection), f"{PROVIDER}:{car}".encode(), hashlib.sha256
            ).hexdigest()[:24]

    @staticmethod
    def display_name_for(car: str) -> str:
        return _masked_vehicle_label(car)

    def trails(self, line_id: str, selected_date: date, *, limit_per_vehicle: int = 500) -> dict[str, list[dict[str, Any]]]:
        start = datetime.combine(selected_date, datetime.min.time(), TAIPEI_TZ)
        end = start + timedelta(days=1)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT vehicle_id, vehicle_label, gps_time, latitude, longitude, location "
                "FROM vehicle_positions WHERE provider=? AND line_id=? AND gps_time>=? AND gps_time<? "
                "ORDER BY vehicle_id, gps_time",
                (PROVIDER, line_id, start.isoformat(), end.isoformat()),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            points = grouped.setdefault(row["vehicle_id"], [])
            points.append({
                "gpsTime": row["gps_time"], "latitude": row["latitude"], "longitude": row["longitude"],
                "location": row["location"], "vehicleLabel": row["vehicle_label"],
            })
        for vehicle_id, points in list(grouped.items()):
            if len(points) > limit_per_vehicle:
                step = (len(points) - 1) / (limit_per_vehicle - 1)
                grouped[vehicle_id] = [points[round(index * step)] for index in range(limit_per_vehicle)]
        return grouped

    def status(self) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM collector_status WHERE provider=?", (PROVIDER,)).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) FROM vehicle_positions "
                "WHERE provider=? AND julianday(gps_time)>=julianday(?)",
                (PROVIDER, cutoff.isoformat(timespec="seconds")),
            ).fetchone()[0]
        return {
            "collectorLastAttemptAt": row["last_attempt_at"] if row else None,
            "collectorLastSuccessAt": row["last_success_at"] if row else None,
            "collectorLatestGpsTime": row["latest_gps_time"] if row else None,
            "collectorLastError": row["last_error"] if row else None,
            "trackRetentionDays": self.retention_days,
            "trackPositionCount": count,
        }


class CameraService:
    def __init__(
        self, *, client_id: str, client_secret: str, cache_seconds: int = 21_600,
        timeout: tuple[int, int] = (5, 15), session: requests.Session | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self.session = session or requests.Session()
        self._lock = threading.RLock()
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._cameras: list[dict[str, Any]] = []
        self._fetched_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _access_token(self) -> str:
        now = datetime.now(timezone.utc)
        with self._lock:
            if self._token and self._token_expires_at and self._token_expires_at > now + timedelta(seconds=60):
                return self._token
        response = self.session.post(TDX_TOKEN_URL, data={
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret,
        }, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise ValueError("TDX 未回傳 access_token")
        expires = max(120, int(payload.get("expires_in") or 1200))
        with self._lock:
            self._token = token
            self._token_expires_at = now + timedelta(seconds=expires)
        return token

    def _catalog(self) -> tuple[list[dict[str, Any]], bool]:
        now = datetime.now(timezone.utc)
        with self._lock:
            if self._fetched_at and now - self._fetched_at < timedelta(seconds=self.cache_seconds):
                return list(self._cameras), False
        token = self._access_token()
        response = self.session.get(
            TDX_CCTV_URL,
            params={"$top": 2000, "$format": "JSON"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        raw_items = payload if isinstance(payload, list) else payload.get("CCTVs", []) if isinstance(payload, dict) else []
        cameras = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            latitude = _float(item.get("PositionLat"))
            longitude = _float(item.get("PositionLon"))
            camera_id = str(item.get("CCTVID") or "").strip()
            if not camera_id or latitude is None or longitude is None:
                continue
            image_url = _safe_url(item.get("VideoImageURL"))
            stream_url = _safe_url(item.get("VideoStreamURL"))
            cameras.append({
                "id": camera_id, "roadName": str(item.get("RoadName") or "未提供道路名稱").strip(),
                "latitude": latitude, "longitude": longitude, "imageUrl": image_url,
                "streamUrl": stream_url, "sourceUpdatedAt": item.get("SrcUpdateTime") or item.get("UpdateTime"),
            })
        with self._lock:
            self._cameras = cameras
            self._fetched_at = now
        return list(cameras), False

    @staticmethod
    def _media(camera: dict[str, Any]) -> tuple[str, str | None]:
        image_url, stream_url = camera.get("imageUrl"), camera.get("streamUrl")
        if image_url and image_url.startswith("https://"):
            return "image", image_url
        if stream_url and stream_url.startswith("https://"):
            suffix = urlparse(stream_url).path.lower()
            if suffix.endswith((".m3u8", ".mp4")):
                return "video", stream_url
            if suffix.endswith((".jpg", ".jpeg", ".png", ".mjpg", ".mjpeg")):
                return "image", stream_url
        return "link", None

    def nearby(self, latitude: float, longitude: float, *, radius_meters: int, limit: int) -> dict[str, Any]:
        if not self.configured:
            return {"status": "unconfigured", "message": "尚未設定 TDX 交通影像金鑰。", "cameras": []}
        try:
            cameras, stale = self._catalog()
            ranked = []
            for camera in cameras:
                distance = round(_distance(latitude, longitude, camera["latitude"], camera["longitude"]))
                if distance <= radius_meters:
                    media_type, media_url = self._media(camera)
                    official_url = camera.get("streamUrl") or camera.get("imageUrl") or NEW_TAIPEI_TRAFFIC_URL
                    ranked.append((distance, {
                        **camera, "distanceMeters": distance, "mediaType": media_type,
                        "mediaUrl": media_url, "officialUrl": official_url,
                    }))
            ranked.sort(key=lambda item: (item[0], item[1]["id"]))
            return {
                "status": "stale" if stale else "ready", "source": "交通部 TDX 公開交通路況影像",
                "fetchedAt": self._fetched_at.isoformat(timespec="seconds") if self._fetched_at else None,
                "cameras": [item for _, item in ranked[:limit]],
            }
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            with self._lock:
                if self._cameras:
                    cameras = list(self._cameras)
                else:
                    return {"status": "unavailable", "message": "交通影像目前無法取得。", "cameras": []}
            ranked = []
            for camera in cameras:
                distance = round(_distance(latitude, longitude, camera["latitude"], camera["longitude"]))
                if distance <= radius_meters:
                    media_type, media_url = self._media(camera)
                    ranked.append((distance, {
                        **camera, "distanceMeters": distance, "mediaType": media_type, "mediaUrl": media_url,
                        "officialUrl": camera.get("streamUrl") or camera.get("imageUrl") or NEW_TAIPEI_TRAFFIC_URL,
                    }))
            ranked.sort(key=lambda item: item[0])
            return {"status": "stale", "message": "目前顯示最近一次可用的交通影像目錄。", "cameras": [item for _, item in ranked[:limit]]}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tdxConfigured": self.configured,
                "cameraCacheTime": self._fetched_at.isoformat(timespec="seconds") if self._fetched_at else None,
                "cameraCount": len(self._cameras),
            }
