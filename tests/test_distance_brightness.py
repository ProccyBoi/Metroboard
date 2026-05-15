import unittest

import app as metro


class DistanceBrightnessStateTests(unittest.TestCase):
    def setUp(self):
        self._old_station_index = metro.STATION_INDEX
        self._old_child_to_parent = metro.CHILD_TO_PARENT
        self._old_station_name_to_parent = metro.STATION_NAME_TO_PARENT
        self._old_fetch_vp_cached = metro.fetch_vp_cached
        self._old_collect_trains = metro.collect_trains
        self._old_strict_route_match = metro.MB_STRICT_ROUTE_MATCH
        self._old_force_distance = metro.MB_FORCE_DISTANCE
        self._old_verbose = metro.MB_VERBOSE

        metro.MB_VERBOSE = False
        metro.MB_STRICT_ROUTE_MATCH = True
        metro.MB_FORCE_DISTANCE = False
        metro.STATION_NAME_TO_PARENT = {}
        metro.CHILD_TO_PARENT = {}
        metro.STATION_INDEX = {
            "Alpha1": {
                "name": "Alpha",
                "base_name": "Alpha",
                "stop_id": "alpha_parent",
                "lat": 0.0,
                "lon": 0.0,
                "route_short": "T1",
            },
            "Beta1": {
                "name": "Beta",
                "base_name": "Beta",
                "stop_id": "beta_parent",
                "lat": 0.0,
                "lon": 0.01,
                "route_short": "T1",
            },
            "Gamma1": {
                "name": "Gamma",
                "base_name": "Gamma",
                "stop_id": "gamma_parent",
                "lat": 0.0,
                "lon": 0.02,
                "route_short": "T1",
            },
        }
        self.trains = []
        metro.fetch_vp_cached = lambda: (object(), 0.0)
        metro.collect_trains = lambda _feed, _routes: (self.trains, {}, {})

    def tearDown(self):
        metro.STATION_INDEX = self._old_station_index
        metro.CHILD_TO_PARENT = self._old_child_to_parent
        metro.STATION_NAME_TO_PARENT = self._old_station_name_to_parent
        metro.fetch_vp_cached = self._old_fetch_vp_cached
        metro.collect_trains = self._old_collect_trains
        metro.MB_STRICT_ROUTE_MATCH = self._old_strict_route_match
        metro.MB_FORCE_DISTANCE = self._old_force_distance
        metro.MB_VERBOSE = self._old_verbose

    def test_train_targets_one_station_even_when_subset_omits_it(self):
        self.trains = [
            (0.0, 0.004, "T1", None, metro.rt.VehiclePosition.IN_TRANSIT_TO),
        ]

        full = metro._calculate_states_for_subset(metro.STATION_INDEX)
        beta_only = metro._calculate_states_for_subset({"Beta1": metro.STATION_INDEX["Beta1"]})

        self.assertEqual(full["Alpha1"], 2)
        self.assertEqual(full["Beta1"], 0)
        self.assertEqual(beta_only["Beta1"], 0)

    def test_reported_stopped_station_is_full_brightness_state(self):
        metro.CHILD_TO_PARENT = {"child_beta": "beta_parent"}
        self.trains = [
            (None, None, "T1", "child_beta", metro.rt.VehiclePosition.STOPPED_AT),
        ]

        states = metro._calculate_states_for_subset(metro.STATION_INDEX)

        self.assertEqual(states["Beta1"], 3)
        self.assertEqual(states["Alpha1"], 0)
        self.assertEqual(states["Gamma1"], 0)

    def test_distance_close_to_station_is_full_brightness_state(self):
        self.trains = [
            (0.0, 0.0105, "T1", None, metro.rt.VehiclePosition.IN_TRANSIT_TO),
        ]

        states = metro._calculate_states_for_subset(metro.STATION_INDEX)

        self.assertEqual(states["Beta1"], 3)
        self.assertEqual(states["Alpha1"], 0)
        self.assertEqual(states["Gamma1"], 0)


if __name__ == "__main__":
    unittest.main()
