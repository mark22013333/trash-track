from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app as app_module
import collector as collector_module
from app import RouteIndex, build_route_index, predict_next_stop
from live_tracking import CameraService, TrackStore


def gps_row(car: str, line: str, moment: datetime, lat: float = 25.04, lon: float = 121.45):
    return {
        "car": car,
        "lineid": line,
        "time": moment.strftime("%Y/%m/%d %H:%M:%S"),
        "latitude": str(lat),
        "longitude": str(lon),
        "location": "新北市測試路口",
    }


class TrackStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tracking.sqlite3"
        self.store = TrackStore(self.path, retention_days=7)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_snapshot_is_removed_and_vehicle_id_is_stable(self):
        now = datetime.now(app_module.TAIPEI_TZ).replace(microsecond=0)
        row = gps_row("KEJ-3260", "L-1", now)
        self.assertEqual(1, self.store.record([row, row], observed_at=now))
        trails = self.store.trails("L-1", now.date())
        self.assertEqual(1, len(next(iter(trails.values()))))
        first_id = self.store.vehicle_id_for("KEJ-3260")
        self.assertEqual(first_id, TrackStore(self.path).vehicle_id_for("KEJ-3260"))
        self.assertEqual("垃圾車 • 3260", next(iter(trails.values()))[0]["vehicleLabel"])
        with sqlite3.connect(self.path) as connection:
            serialized = " ".join(str(value) for row in connection.execute("SELECT * FROM vehicle_positions") for value in row)
        self.assertNotIn("KEJ-3260", serialized)

    def test_old_positions_are_purged_and_empty_snapshot_is_success(self):
        now = datetime.now(app_module.TAIPEI_TZ).replace(microsecond=0)
        self.store.record([
            gps_row("OLD-0001", "L-1", now - timedelta(days=8)),
            gps_row("NEW-0002", "L-1", now),
        ], observed_at=now)
        self.assertEqual(1, sum(len(points) for points in self.store.trails("L-1", now.date()).values()))
        self.store.record([], observed_at=now + timedelta(minutes=2))
        status = self.store.status()
        self.assertIsNone(status["collectorLastError"])
        self.assertEqual(0, status["collectorLastError"] is not None)

    def test_only_one_collector_holds_a_live_lease(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertTrue(self.store.acquire_lease("gps", "owner-a", seconds=120, now=now))
        self.assertFalse(self.store.acquire_lease("gps", "owner-b", seconds=120, now=now + timedelta(seconds=30)))
        self.assertTrue(self.store.acquire_lease("gps", "owner-b", seconds=120, now=now + timedelta(seconds=121)))
        self.store.release_lease("gps", "owner-b")
        self.assertTrue(self.store.acquire_lease("gps", "owner-a", seconds=120, now=now + timedelta(seconds=122)))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.post_count = 0
        self.get_count = 0

    def post(self, *_args, **_kwargs):
        self.post_count += 1
        return FakeResponse({"access_token": "secret-token", "expires_in": 1200})

    def get(self, *_args, **_kwargs):
        self.get_count += 1
        return FakeResponse([
            {"CCTVID": "far", "RoadName": "遠方路口", "PositionLat": 25.08, "PositionLon": 121.45, "VideoImageURL": "https://example.test/far.jpg"},
            {"CCTVID": "near", "RoadName": "近方路口", "PositionLat": 25.0401, "PositionLon": 121.45, "VideoImageURL": "https://example.test/near.jpg"},
            {"CCTVID": "unsafe", "RoadName": "危險網址", "PositionLat": 25.0402, "PositionLon": 121.45, "VideoImageURL": "javascript:alert(1)"},
        ])


class CameraServiceTests(unittest.TestCase):
    def test_unconfigured_service_degrades_without_error(self):
        payload = CameraService(client_id="", client_secret="").nearby(25, 121, radius_meters=1500, limit=3)
        self.assertEqual("unconfigured", payload["status"])
        self.assertEqual([], payload["cameras"])

    def test_token_and_catalog_are_cached_sorted_and_unsafe_url_is_filtered(self):
        session = FakeSession()
        service = CameraService(client_id="id", client_secret="secret", session=session)
        first = service.nearby(25.04, 121.45, radius_meters=3000, limit=2)
        second = service.nearby(25.04, 121.45, radius_meters=3000, limit=2)
        self.assertEqual(["near", "unsafe"], [item["id"] for item in first["cameras"]])
        unsafe = first["cameras"][1]
        self.assertEqual("link", unsafe["mediaType"])
        self.assertNotIn("javascript:", unsafe["officialUrl"])
        self.assertEqual(first["cameras"], second["cameras"])
        self.assertEqual(1, session.post_count)
        self.assertEqual(1, session.get_count)


class CollectorTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.recorded = None
            self.failure = None

        def acquire_lease(self, *_args, **_kwargs):
            return True

        def record(self, rows):
            self.recorded = rows

        def record_failure(self, message):
            self.failure = message

    def test_empty_non_collection_snapshot_is_success(self):
        store = self.FakeStore()
        data = type("Data", (), {"get_trucks": lambda _self: ([], False)})()
        with patch.object(collector_module, "track_store", store), patch.object(collector_module, "data_service", data):
            self.assertTrue(collector_module.collect_once("owner"))
        self.assertEqual([], store.recorded)
        self.assertIsNone(store.failure)

    def test_malformed_nonempty_snapshot_marks_failure(self):
        store = self.FakeStore()
        data = type("Data", (), {"get_trucks": lambda _self: ([{"unexpected": "shape"}], False)})()
        with patch.object(collector_module, "track_store", store), patch.object(collector_module, "data_service", data):
            self.assertFalse(collector_module.collect_once("owner"))
        self.assertIn("格式異常", store.failure)


class PredictionTests(unittest.TestCase):
    def route_rows(self):
        return [
            {"rank": rank, "name": f"第{rank}站", "latitude": 25 + rank * .001, "longitude": 121.45, "time": f"18:0{rank}"}
            for rank in range(1, 5)
        ]

    def test_crossing_near_earlier_stop_does_not_move_prediction_backward(self):
        now = datetime.now(app_module.TAIPEI_TZ)
        trail = [
            {"latitude": 25.002, "longitude": 121.45},
            {"latitude": 25.001, "longitude": 121.45},
        ]
        prediction = predict_next_stop(25.001, 121.45, self.route_rows(), now, trail)
        self.assertIsNone(prediction["nextStop"])
        self.assertEqual("low", prediction["confidence"])

    def test_stale_gps_does_not_claim_a_next_stop(self):
        old = datetime.now(app_module.TAIPEI_TZ) - timedelta(minutes=11)
        prediction = predict_next_stop(25.001, 121.45, self.route_rows(), old, [])
        self.assertIsNone(prediction["nextStop"])
        self.assertEqual("low", prediction["confidence"])

    def test_fresh_forward_movement_can_return_high_confidence(self):
        now = datetime.now(app_module.TAIPEI_TZ)
        trail = [
            {"latitude": 25.001, "longitude": 121.45},
            {"latitude": 25.002, "longitude": 121.45},
        ]
        prediction = predict_next_stop(25.002, 121.45, self.route_rows(), now, trail)
        self.assertEqual(3, prediction["nextStop"]["rank"])
        self.assertEqual("high", prediction["confidence"])


class LiveApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TrackStore(Path(self.temp.name) / "api.sqlite3")
        self.now = datetime.now(app_module.TAIPEI_TZ).replace(microsecond=0)
        rows = [
            {"lineid": "L-1", "linename": "測試線", "rank": "1", "name": "甲站", "latitude": "25.04", "longitude": "121.45", "time": "18:00"},
            {"lineid": "L-1", "linename": "測試線", "rank": "2", "name": "乙站", "latitude": "25.041", "longitude": "121.45", "time": "18:05"},
        ]
        self.index = build_route_index(rows)

    def tearDown(self):
        self.temp.cleanup()

    def test_live_route_masks_plate_and_returns_trail(self):
        class FakeData:
            def get_route_index(inner_self, _district):
                return self.index, False

            def get_trucks(inner_self):
                return [gps_row("KEJ-3260", "L-1", self.now)], False

        with patch.object(app_module, "data_service", FakeData()), patch.object(app_module, "track_store", self.store):
            response = app_module.app.test_client().get("/api/live-route?district=新莊區&lineid=L-1")
        self.assertEqual(200, response.status_code)
        body = response.get_json()
        serialized = response.get_data(as_text=True)
        self.assertEqual(1, body["activeVehicleCount"])
        self.assertEqual(1, len(body["vehicles"][0]["trail"]))
        self.assertNotIn("KEJ-3260", serialized)
        self.assertIn("3260", body["vehicles"][0]["displayName"])

    def test_live_route_rejects_dates_outside_retention_window(self):
        response = app_module.app.test_client().get("/api/live-route?district=新莊區&lineid=L-1&date=2000-01-01")
        self.assertEqual(400, response.status_code)

    def test_camera_endpoint_enforces_radius_limit(self):
        response = app_module.app.test_client().get("/api/cameras/nearby?latitude=25&longitude=121&radiusMeters=3001")
        self.assertEqual(400, response.status_code)

    def test_status_never_exposes_tdx_secret(self):
        service = CameraService(client_id="public-id", client_secret="super-secret")
        with patch.object(app_module, "camera_service", service), patch.object(app_module, "track_store", self.store):
            response = app_module.app.test_client().get("/api/status")
        serialized = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["tdxConfigured"])
        self.assertNotIn("public-id", serialized)
        self.assertNotIn("super-secret", serialized)


if __name__ == "__main__":
    unittest.main()
