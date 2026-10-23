import re
import unittest
from pathlib import Path
from unittest import mock

import app as metro


ROOT = Path(__file__).resolve().parents[1]

# Physical M1 LEDs, in order from index 0 to index 30. Keep the existing board
# wiring stable when updating station data or the firmware.
M1_STATIONS = (
    "Tallawong", "Rouse Hill", "Kellyville", "Bella Vista", "Norwest",
    "Hills Showground", "Castle Hill", "Cherrybrook", "Epping",
    "Macquarie University", "Macquarie Park", "North Ryde", "Chatswood",
    "Crows Nest", "Victoria Cross", "Barangaroo", "Martin Place", "Gadigal",
    "Central", "Waterloo", "Sydenham", "Marrickville", "Dulwich Hill",
    "Hurlstone Park", "Canterbury", "Campsie", "Belmore", "Lakemba",
    "Wiley Park", "Punchbowl", "Bankstown",
)
EXTENSION_STATIONS = M1_STATIONS[21:]
DISTANCE_RADII = {"solid_radius_m": 200, "near_radius_m": 800, "far_radius_m": 1500}


class BankstownReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the committed mappings and whitelist without loading the
        # large stop_times file or requesting any live data.
        with mock.patch.object(metro, "MB_VERBOSE", False):
            cls.route_pairs = metro.load_station_route_pairs(
                str(ROOT / "station_line_pairs.csv")
            )
            cls.child_to_parent = metro.build_child_to_parent(str(ROOT / "stops.txt"))
            cls.sid_whitelist, cls.name_whitelist = metro.load_whitelist(
                str(ROOT / "stations_whitelist.txt")
            )
            index = metro.load_station_index(
                str(ROOT / "stops.txt"),
                sid_whitelist=cls.sid_whitelist,
                name_whitelist=cls.name_whitelist,
                allowed_parents=set(cls.route_pairs),
                allowed_pairs={
                    (parent, route)
                    for parent, routes in cls.route_pairs.items()
                    for route in routes
                },
            )
        # Retain other lines at shared stations to catch cross-line lighting.
        cls.station_index = {
            key: station for key, station in index.items()
            if station["base_name"] in M1_STATIONS
        }
        cls.sketch = (ROOT / "metroboard_public.ino").read_text(encoding="utf-8")

    def setUp(self):
        patches = mock.patch.multiple(
            metro,
            STATION_INDEX=self.station_index,
            CHILD_TO_PARENT=self.child_to_parent,
            STATION_ROUTE_PAIRS=self.route_pairs,
            STATION_NAME_TO_PARENT={
                station["base_name"]: station["stop_id"]
                for station in self.station_index.values()
            },
            ROUTE_ID_TO_SHORT={},
            TRIP_ID_TO_SHORT={},
            STOP_ROUTE_PAIRS={},
            ROUTE_SHORT_ALLOW=set(),
            MB_STRICT_ROUTE_MATCH=True,
            MB_FORCE_DISTANCE=False,
            MB_VERBOSE=False,
        )
        patches.start()
        self.addCleanup(patches.stop)

    def _feed(self, name, route="M1", *, position=False, latitude_offset=0.0):
        station = self.station_index[name + "M"]
        feed = metro.rt.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        vehicle = feed.entity.add(id="readiness-vehicle").vehicle
        vehicle.trip.route_id = route
        if position:
            # Opening-day platforms may have IDs absent from the bundled GTFS.
            vehicle.stop_id = "unmapped-opening-day-platform"
            vehicle.position.latitude = station["lat"] + latitude_offset
            vehicle.position.longitude = station["lon"]
            vehicle.current_status = metro.rt.VehiclePosition.IN_TRANSIT_TO
        else:
            vehicle.stop_id = station["stop_id"]
            vehicle.current_status = metro.rt.VehiclePosition.STOPPED_AT
        return feed

    def _active_states(self, feed):
        with mock.patch.object(metro, "fetch_vp_cached", return_value=(feed, 0.0)):
            states = metro._calculate_states_for_subset(
                self.station_index, DISTANCE_RADII
            )
        self.assertEqual(set(states), set(self.station_index))
        return {key: state for key, state in states.items() if state}

    def test_m1_firmware_preserves_all_31_physical_led_indices(self):
        sequence = re.search(r"\bM1_SEQ\[\]\s*=\s*\{([^}]+)\}", self.sketch)
        self.assertIsNotNone(sequence)
        names = re.findall(r'"([^"\n]+)"', sequence.group(1))
        self.assertEqual(tuple(names), M1_STATIONS)

        line_enum = re.search(r"enum\s+Line[^\{]*\{([^}]+)\}", self.sketch)
        self.assertIsNotNone(line_enum)
        lines = re.findall(r"\bL_[A-Z0-9]+\b", line_enum.group(1))
        lengths = re.search(r"\bSTRIP_LEN\[LINE_COUNT\]\s*=\s*\{([^}]+)\}", self.sketch)
        reverse = re.search(r"\bREVERSE\[LINE_COUNT\]\s*=\s*\{([^}]+)\}", self.sketch)
        self.assertIsNotNone(lengths)
        self.assertIsNotNone(reverse)
        strip_lengths = [int(value.strip()) for value in lengths.group(1).split(",")]
        strip_reverse = [int(value.strip()) for value in reverse.group(1).split(",")]
        self.assertEqual(len(strip_lengths), len(lines))
        self.assertEqual(len(strip_reverse), len(lines))
        self.assertEqual(strip_lengths[lines.index("L_M1")], 31)
        self.assertEqual(strip_reverse[lines.index("L_M1")], 0)

    def test_all_m1_leds_have_real_whitelisted_station_coordinates(self):
        expected_keys = {name + "M" for name in M1_STATIONS}
        actual_keys = {
            key for key, station in self.station_index.items()
            if station["route_short"] == "M1"
        }
        self.assertEqual(actual_keys, expected_keys)
        for name in M1_STATIONS:
            with self.subTest(station=name):
                station = self.station_index[name + "M"]
                self.assertEqual(station["base_name"], name)
                self.assertIn("M1", self.route_pairs[station["stop_id"]])
                self.assertTrue(
                    station["stop_id"] in self.sid_whitelist
                    or name.lower() in self.name_whitelist
                )
                self.assertGreater(station["lat"], -34)
                self.assertLess(station["lat"], -33)
                self.assertGreater(station["lon"], 150)
                self.assertLess(station["lon"], 152)

    def test_each_extension_stop_lights_only_its_metro_led(self):
        for name in ("Sydenham", *EXTENSION_STATIONS):
            with self.subTest(station=name):
                self.assertEqual(self._active_states(self._feed(name)), {name + "M": 3})

    def test_unmapped_platforms_use_position_on_the_correct_metro_line(self):
        for route in ("M1", "M1_new", "SM_new", "SMNW_new", "METRO_new"):
            for name in EXTENSION_STATIONS:
                with self.subTest(route=route, station=name):
                    feed = self._feed(name, route, position=True)
                    self.assertEqual(self._active_states(feed), {name + "M": 3})

    def test_approaching_extension_trains_keep_distance_brightness_states(self):
        for name in EXTENSION_STATIONS:
            with self.subTest(station=name):
                # About 334 metres north: outside the solid radius, inside the
                # near radius, and still nearest to this station on M1.
                feed = self._feed(name, position=True, latitude_offset=0.003)
                self.assertEqual(self._active_states(feed), {name + "M": 2})

    def test_bankstown_t6_vehicle_does_not_light_the_metro_led(self):
        feed = self._feed("Bankstown", "T6_service")
        self.assertEqual(self._active_states(feed), {"Bankstown6": 3})

    def test_hills_showground_vehicle_reaches_its_existing_led(self):
        self.assertEqual(
            self._active_states(self._feed("Hills Showground")),
            {"Hills ShowgroundM": 3},
        )

    def test_no_vehicles_leave_future_extension_leds_off(self):
        feed = metro.rt.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        self.assertEqual(self._active_states(feed), {})


if __name__ == "__main__":
    unittest.main()
