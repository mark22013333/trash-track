"""新北市出勤垃圾車 GPS 常駐蒐集器。"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime

from app import (
    GPS_DELAY_SECONDS, OpenDataError, SETTINGS, TAIPEI_TZ, data_service,
    parse_float, parse_gps_time, track_store,
)


logger = logging.getLogger("trash-track.collector")
stop_event = threading.Event()


def collect_once(owner: str) -> bool:
    interval = SETTINGS.track_collection_seconds
    if not track_store.acquire_lease("newtaipei-gps", owner, seconds=max(interval * 2, 180)):
        logger.info("另一個蒐集器持有租約，本輪略過")
        return False
    try:
        rows, stale = data_service.get_trucks()
        valid_rows = [
            row for row in rows
            if str(row.get("car") or "").strip()
            and str(row.get("lineid") or "").strip()
            and parse_gps_time(row.get("time"))
            and (latitude := parse_float(row.get("latitude"))) is not None
            and (longitude := parse_float(row.get("longitude"))) is not None
            and 20 <= latitude <= 27
            and 118 <= longitude <= 123
        ]
        if rows and not valid_rows:
            raise ValueError("官方 GPS 回傳欄位格式異常")
        track_store.record(rows)
        gps_times = [parsed for row in rows if (parsed := parse_gps_time(row.get("time")))]
        if stale:
            track_store.record_failure("官方 GPS 更新失敗，目前使用快取資料")
        elif gps_times and (datetime.now(TAIPEI_TZ) - max(gps_times)).total_seconds() > GPS_DELAY_SECONDS:
            track_store.record_failure("官方 GPS 已連續超過 10 分鐘沒有新資料")
        else:
            logger.info("GPS 蒐集完成：官方回傳 %d 筆；空陣列代表非清運時段", len(rows))
        return True
    except (OpenDataError, OSError, ValueError) as exc:
        logger.warning("GPS 蒐集失敗：%s", exc)
        track_store.record_failure(str(exc))
        return False


def main() -> None:
    owner = track_store.new_owner()
    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        handled_signals.append(signal.SIGBREAK)
    for signal_name in handled_signals:
        signal.signal(signal_name, lambda *_: stop_event.set())
    logger.info("常駐 GPS 蒐集器已啟動，每 %d 秒執行一次", SETTINGS.track_collection_seconds)
    try:
        while not stop_event.is_set():
            collect_once(owner)
            stop_event.wait(SETTINGS.track_collection_seconds)
    finally:
        track_store.release_lease("newtaipei-gps", owner)
        logger.info("常駐 GPS 蒐集器已停止")


if __name__ == "__main__":
    main()
