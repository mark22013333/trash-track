import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

import app as trash_track


SAMPLE_ROWS = [
    {
        "city": "新莊區",
        "lineid": "A001",
        "linename": "測試路線",
        "rank": "2",
        "name": "西盛街20號",
        "latitude": "25.001",
        "longitude": "121.001",
        "time": "19:02",
        "garbagetuesday": "Y",
    },
    {
        "city": "新莊區",
        "lineid": "A001",
        "linename": "測試路線",
        "rank": "1",
        "name": "西盛街10號",
        "latitude": "25.000",
        "longitude": "121.000",
        "time": "19:01",
        "garbagetuesday": "Y",
    },
]


class RouteLogicTests(unittest.TestCase):
    def test_find_routes_is_dynamic_and_ranked(self):
        routes, all_by_line = trash_track.find_routes(SAMPLE_ROWS, "西盛街")

        self.assertEqual(1, len(routes))
        self.assertEqual("A001", routes[0]["lineid"])
        self.assertEqual([1, 2], [stop["rank"] for stop in routes[0]["stops"]])
        self.assertEqual(2, len(all_by_line["A001"]))

    def test_missing_street_returns_empty_routes(self):
        routes, _ = trash_track.find_routes(SAMPLE_ROWS, "這條路不存在123")
        self.assertEqual([], routes)

    def test_estimated_next_stop_uses_nearest_rank(self):
        next_stop, nearest_distance = trash_track.estimate_next_stop(
            25.00001, 121.00001, SAMPLE_ROWS
        )
        self.assertEqual(2, next_stop["rank"])
        self.assertLess(nearest_distance, 10)


class CacheFallbackTests(unittest.TestCase):
    def test_stale_route_cache_is_returned_and_retry_is_throttled(self):
        service = trash_track.OpenDataService()
        old_time = datetime.now(trash_track.TAIPEI_TZ) - timedelta(
            seconds=trash_track.ROUTE_CACHE_SECONDS + 1
        )
        service._route_cache["新莊區"] = trash_track.CacheEntry(SAMPLE_ROWS, old_time)
        service._fetch_pages = Mock(side_effect=trash_track.OpenDataError("暫時失敗"))

        rows, stale = service.get_district_routes("新莊區")
        rows_again, stale_again = service.get_district_routes("新莊區")

        self.assertEqual(SAMPLE_ROWS, rows)
        self.assertEqual(SAMPLE_ROWS, rows_again)
        self.assertTrue(stale)
        self.assertTrue(stale_again)
        self.assertEqual(1, service._fetch_pages.call_count)


if __name__ == "__main__":
    unittest.main()
