"""地址正規化、巷弄索引與輕量 SQLite 快取。"""

from __future__ import annotations

import json
import re
import sqlite3
import ssl
import threading
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from region_catalog import COUNTY_CODE_BY_NAME, DISTRICTS, normalize_taiwan_name


ROAD_PATTERN = re.compile(
    r"([\u3400-\u9fffA-Za-z0-9]+?(?:大道|路|街)(?:[一二三四五六七八九十百千0-9]+段)?)"
)
LANE_PATTERN = re.compile(
    r"([一二三四五六七八九十百千0-9－—-]+巷(?:[一二三四五六七八九十百千0-9－—-]+弄)?)"
)


class GovernmentTLSAdapter(HTTPAdapter):
    """保留憑證驗證，相容部分政府站台缺少非關鍵 SKI 延伸欄位的憑證鏈。"""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        context = ssl.create_default_context(cafile=requests.certs.where())
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            context.verify_flags &= ~strict_flag
        pool_kwargs["ssl_context"] = context
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


def normalize_address(value: Any) -> str:
    text = str(value or "").strip().replace("台", "臺")
    text = re.sub(r"\s+", "", text)
    return text.replace("－", "-").replace("—", "-")


def extract_road_lane(value: Any) -> tuple[str | None, str | None]:
    text = normalize_address(value)
    for county, code in COUNTY_CODE_BY_NAME.items():
        if county in text:
            text = text.split(county, 1)[1]
            for district in DISTRICTS[code]:
                if district in text:
                    text = text.split(district, 1)[1]
                    break
            break
    roads = ROAD_PATTERN.findall(text)
    lane = LANE_PATTERN.search(text)
    return (roads[-1] if roads else None, lane.group(1) if lane else None)


def index_location_options(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    roads: dict[str, set[str]] = {}
    for row in rows:
        road, lane = extract_road_lane(row.get("name"))
        if not road:
            continue
        roads.setdefault(road, set())
        if lane:
            roads[road].add(lane)
    return {road: sorted(lanes, key=lambda item: (len(item), item)) for road, lanes in sorted(roads.items())}


class SQLiteStore:
    """只保存官方公開資料快取；使用者地址永不寫入。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_entries ("
                "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )
            connection.commit()

    def get(self, key: str, max_age_seconds: int | None = None) -> tuple[Any, datetime] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload, fetched_at FROM cache_entries WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        fetched_at = datetime.fromisoformat(row[1])
        if max_age_seconds is not None and datetime.now(timezone.utc) - fetched_at > timedelta(seconds=max_age_seconds):
            return None
        try:
            return json.loads(row[0]), fetched_at
        except json.JSONDecodeError:
            return None

    def set(self, key: str, payload: Any) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO cache_entries(cache_key, payload, fetched_at) VALUES(?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
                (key, serialized, fetched_at),
            )
            connection.commit()


class OfficialRoadService:
    NLSC_BASE_URL = "https://api.nlsc.gov.tw"

    def __init__(self, store: SQLiteStore, *, cache_seconds: int, timeout: tuple[int, int]):
        self.store = store
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.mount("https://", GovernmentTLSAdapter())
        self.session.headers.update({"User-Agent": "TrashTrack/3.0 (public civic utility)"})

    def _xml(self, path: str) -> ET.Element:
        response = self.session.get(f"{self.NLSC_BASE_URL}{path}", timeout=self.timeout)
        response.raise_for_status()
        return ET.fromstring(response.content)

    def roads(self, county: str, district: str) -> tuple[list[str], bool]:
        county = normalize_taiwan_name(county)
        district = normalize_taiwan_name(district)
        cache_key = f"roads:{county}:{district}"
        cached = self.store.get(cache_key, self.cache_seconds)
        if cached:
            return list(cached[0]), False

        county_code = COUNTY_CODE_BY_NAME[county]
        town_root = self._xml(f"/other/ListTown1/{quote(county_code)}")
        town_code = next(
            (item.findtext("towncode01") for item in town_root.findall(".//townItem")
             if normalize_taiwan_name(item.findtext("townname") or "") == district),
            None,
        )
        if not town_code:
            raise ValueError("找不到對應的官方行政區代碼")
        road_root = self._xml(f"/idc/ListRoadM/{quote(county_code)}/{quote(town_code)}")
        values = {
            normalize_address(node.text)
            for tag in ("roadname", "roadName", "name")
            for node in road_root.findall(f".//{tag}")
            if node.text
            and normalize_address(node.text)
            and "巷" not in normalize_address(node.text)
            and "弄" not in normalize_address(node.text)
        }
        roads = sorted(values, key=lambda item: (len(item), item))
        self.store.set(cache_key, roads)
        return roads, False
