import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import app as trash_track
from config import get_settings
from location_service import extract_road_lane, index_location_options


SAMPLE_ROWS = [
    {
        "city": "新莊區", "lineid": "B002", "linename": "夜間線", "rank": "1",
        "name": "西盛街10號", "latitude": "25.0000001", "longitude": "121.0000001",
        "time": "19:01", "garbagetuesday": "Y",
    },
    {
        "city": "新莊區", "lineid": "A001", "linename": "測試路線", "rank": "2",
        "name": "西盛街20號", "latitude": "25.001", "longitude": "121.001",
        "time": "19:20", "garbagetuesday": "Y",
    },
    {
        "city": "新莊區", "lineid": "A001", "linename": "測試路線", "rank": "1",
        "name": "西盛街10號", "latitude": "25.0000002", "longitude": "121.0000002",
        "time": "19:01", "garbagetuesday": "Y",
    },
    {
        "city": "新莊區", "lineid": "A001", "linename": "測試路線", "rank": "3",
        "name": "民安路30號", "latitude": "25.002", "longitude": "121.002",
        "time": "19:35", "garbagetuesday": "Y",
    },
]


class RouteLogicTests(unittest.TestCase):
    def test_full_taiwan_address_extracts_road_and_lane(self):
        self.assertEqual(
            ("西盛街", "33巷2弄"),
            extract_road_lane("新北市新莊區西盛街33巷2弄8號"),
        )

    def test_location_index_lists_only_lanes_seen_in_stops(self):
        options = index_location_options([
            {"name": "西盛街33巷2弄8號"},
            {"name": "西盛街33巷10號"},
            {"name": "民安路20號"},
        ])
        self.assertEqual(["33巷", "33巷2弄"], options["西盛街"])
        self.assertEqual([], options["民安路"])

    def test_route_options_are_deduplicated_and_stably_sorted(self):
        index = trash_track.build_route_index(SAMPLE_ROWS)
        self.assertEqual(["B002", "A001"], [item["lineid"] for item in index.route_summaries])
        self.assertEqual(3, index.route_summaries[1]["stopCount"])
        self.assertEqual("19:01", index.route_summaries[1]["firstTime"])
        self.assertEqual("19:35", index.route_summaries[1]["lastTime"])

    def test_find_routes_keeps_street_compatibility_and_rank_order(self):
        routes, all_by_line = trash_track.find_routes(SAMPLE_ROWS, "西盛街")
        self.assertEqual(2, len(routes))
        self.assertEqual([1, 2], [stop["rank"] for stop in routes[0]["stops"]])
        self.assertEqual(3, len(all_by_line["A001"]))

    def test_exact_line_returns_full_route(self):
        index = trash_track.build_route_index(SAMPLE_ROWS)
        routes = trash_track.select_routes(index, "A001", "西盛街")
        self.assertEqual(["A001"], [route["lineid"] for route in routes])
        self.assertEqual(3, len(routes[0]["stops"]))

    def test_physical_stops_are_deduplicated_by_normalized_location_and_name(self):
        index = trash_track.build_route_index(SAMPLE_ROWS)
        routes = [trash_track.serialize_route(line_id, rows)
                  for line_id, rows in index.routes_by_lineid.items()]
        physical = trash_track.deduplicate_stops(routes)
        self.assertEqual(3, len(physical))
        duplicate = next(stop for stop in physical if stop["name"] == "西盛街10號")
        self.assertEqual(2, len(duplicate["appearances"]))

    def test_estimated_next_stop_uses_nearest_rank(self):
        rows = trash_track.build_route_index(SAMPLE_ROWS).routes_by_lineid["A001"]
        next_stop, nearest_distance = trash_track.estimate_next_stop(25.00001, 121.00001, rows)
        self.assertEqual(2, next_stop["rank"])
        self.assertLess(nearest_distance, 10)


class CacheFallbackTests(unittest.TestCase):
    def test_stale_route_cache_is_returned_and_retry_is_throttled(self):
        service = trash_track.OpenDataService()
        old_time = datetime.now(trash_track.TAIPEI_TZ) - timedelta(
            seconds=trash_track.ROUTE_CACHE_SECONDS + 1
        )
        service._route_cache["新莊區"] = trash_track.RouteCacheEntry(
            trash_track.build_route_index(SAMPLE_ROWS), old_time
        )
        service._fetch_pages = Mock(side_effect=trash_track.OpenDataError("暫時失敗"))
        rows, stale = service.get_district_routes("新莊區")
        rows_again, stale_again = service.get_district_routes("新莊區")
        self.assertEqual(SAMPLE_ROWS, rows)
        self.assertEqual(SAMPLE_ROWS, rows_again)
        self.assertTrue(stale)
        self.assertTrue(stale_again)
        self.assertEqual(1, service._fetch_pages.call_count)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = trash_track.app.test_client()
        self.index = trash_track.build_route_index(SAMPLE_ROWS)
        self.route_patch = patch.object(
            trash_track.data_service, "get_route_index", return_value=(self.index, False)
        )
        self.truck_patch = patch.object(
            trash_track.data_service, "get_trucks", return_value=([], False)
        )
        self.route_mock = self.route_patch.start()
        self.truck_mock = self.truck_patch.start()

    def tearDown(self):
        self.route_patch.stop()
        self.truck_patch.stop()

    def test_route_options_endpoint(self):
        response = self.client.get("/api/route-options?district=新莊區")
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json["routeCount"])
        self.assertEqual(["B002", "A001"], [item["lineid"] for item in response.json["routes"]])

    def test_exact_line_endpoint_returns_only_full_selected_route(self):
        with patch.object(trash_track.data_service, "lineids_for_street") as street_search:
            response = self.client.get("/api/routes?district=新莊區&lineid=A001")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json["routeCount"])
        self.assertEqual(3, len(response.json["routes"][0]["stops"]))
        street_search.assert_not_called()

    def test_unknown_line_returns_empty_200_not_500(self):
        response = self.client.get("/api/routes?district=新莊區&lineid=UNKNOWN")
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json["routes"])

    def test_street_endpoint_remains_compatible(self):
        response = self.client.get("/api/routes?district=新莊區&street=西盛街")
        self.assertEqual(200, response.status_code)
        self.assertEqual("西盛街", response.json["street"])
        self.assertEqual(2, response.json["routeCount"])
        self.assertNotIn("民安路30號", [stop["name"] for route in response.json["routes"] for stop in route["stops"]])

    def test_dashboard_gets_route_index_only_once(self):
        response = self.client.get("/api/dashboard?district=新莊區&lineid=A001")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, self.route_mock.call_count)
        self.assertEqual(1, self.truck_mock.call_count)

    def test_home_is_200_without_google_maps_key(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'"mapsApiKeyConfigured": false', response.data)
        self.assertIn(b'data-map-type="satellite"', response.data)
        self.assertIn(b'id="street-view-button"', response.data)

    def test_status_does_not_leak_google_maps_key(self):
        secret = "not-for-output-super-secret"
        with patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": secret, "GOOGLE_MAPS_MAP_ID": "map-id"}):
            response = self.client.get("/api/status")
        self.assertEqual(200, response.status_code)
        self.assertNotIn(secret.encode(), response.data)
        self.assertTrue(response.json["googleMapsApiKeyConfigured"])

    def test_regions_lists_all_counties_and_districts(self):
        response = self.client.get("/api/regions")
        self.assertEqual(200, response.status_code)
        self.assertEqual(22, response.json["countyCount"])
        self.assertEqual(368, response.json["districtCount"])
        new_taipei = next(item for item in response.json["counties"] if item["name"] == "新北市")
        self.assertTrue(new_taipei["capabilities"]["liveGps"])

    def test_location_options_lists_roads_from_cleanup_stops(self):
        response = self.client.get("/api/location-options?county=新北市&district=新莊區")
        self.assertEqual(200, response.status_code)
        self.assertEqual(["民安路", "西盛街"], response.json["options"])

    def test_nearby_uses_post_and_returns_global_route_key(self):
        response = self.client.post("/api/nearby", json={
            "county": "新北市", "district": "新莊區", "road": "西盛街", "limit": 2,
        })
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(response.json["stops"]))
        self.assertTrue(response.json["stops"][0]["routeKey"].startswith("newtaipei:"))
        self.assertEqual("sameRoad", response.json["matchQuality"])

    def test_unknown_lane_falls_back_to_same_road_instead_of_false_no_result(self):
        response = self.client.post("/api/nearby", json={
            "county": "新北市", "district": "新莊區", "road": "西盛街", "lane": "999巷",
        })
        self.assertEqual(200, response.status_code)
        self.assertEqual("sameRoad", response.json["matchQuality"])
        self.assertGreater(len(response.json["stops"]), 0)

    def test_unsupported_county_never_claims_there_is_no_truck(self):
        response = self.client.post("/api/nearby", json={
            "county": "臺北市", "district": "信義區", "road": "市府路",
        })
        self.assertEqual(200, response.status_code)
        self.assertEqual("sourceUnavailable", response.json["answer"]["status"])
        self.assertFalse(response.json["capabilities"]["schedule"])

    def test_nearby_rejects_ambiguous_or_mismatched_district(self):
        response = self.client.post("/api/nearby", json={
            "county": "新北市", "district": "信義區", "road": "市府路",
        })
        self.assertEqual(400, response.status_code)


class ConfigurationTests(unittest.TestCase):
    def test_environment_overrides_defaults_and_invalid_radius_falls_back(self):
        with patch.dict(os.environ, {
            "GOOGLE_MAPS_API_KEY": "browser-key",
            "GOOGLE_MAPS_MAP_ID": "map-id",
            "GOOGLE_PLACES_ENABLED": "false",
            "DEFAULT_SEARCH_RADIUS_METERS": "not-a-number",
        }, clear=True):
            settings = get_settings()
        self.assertEqual("browser-key", settings.google_maps_api_key)
        self.assertEqual(600, settings.default_search_radius_meters)
        self.assertFalse(settings.places_available)


if __name__ == "__main__":
    unittest.main()
