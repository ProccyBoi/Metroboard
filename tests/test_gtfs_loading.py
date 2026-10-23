import csv
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import app as metro


def _dict_reader_ids(path):
    """The previous loaders' field access, retained as a parity oracle."""
    with open(path, encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            yield (row.get("trip_id") or "").strip(), (row.get("stop_id") or "").strip()


class StopTimeProjectionTests(unittest.TestCase):
    def test_csv_edge_cases_match_dict_reader(self):
        tables = {
            "empty": "",
            "header_only": "trip_id,stop_id\n",
            "reordered_bom": "\ufeffstop_id,extra,trip_id\n 10 ,unused, t1 \n",
            "short_and_extra": "trip_id,extra,stop_id\nt1\nt2,x\nt3,x,20,ignored\n",
            "duplicates": "trip_id,stop_id,trip_id,stop_id\nold,10,new,20\nold,10,new\nold,10\n",
            "missing_trip": "stop_id,extra\n10,t1,extra\n",
            "missing_stop": "trip_id,extra\nt1,10,extra\n",
            "empty_header": "\nt1,10\n\nt2,20\n",
            "blank_rows": "trip_id,stop_id\n\n\nt1,10\n,\n\nt2,20\n",
            "quoted_fields": 'trip_id,stop_id,extra\n" t,1 "," 10 ","quoted\nline"\n" t\n2 ","20",x\n',
            "header_whitespace": " trip_id,stop_id \nt1,10\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stop_times.txt"
            for name, table in tables.items():
                with self.subTest(name=name):
                    path.write_text(table, encoding="utf-8")
                    self.assertEqual(
                        list(metro._iter_stop_time_ids(path)),
                        list(_dict_reader_ids(path)),
                    )


class StaticGtfsLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {}
        files = {
            "agency": "agency_id,agency_name\na,Rail\nb,Bus\n",
            "routes": (
                "route_id,route_short_name,route_long_name,route_type,agency_id\n"
                "r1,T1,Rail route,2,a\nr2,M1,Metro route,1,a\nr3,B1,Bus route,3,b\n"
                "r4,,No short name,2,a\nr5,T9,Invalid type,invalid,a\n"
            ),
            "trips": "trip_id,route_id\nt1,r1\nt2,r2\nt3,r3\nt4,r4\nt5,r5\n",
            "stops": (
                "stop_id,stop_name,location_type,parent_station\n"
                "100,Alpha Station,1,\n101,Alpha platform,0,100\n"
                "200,Beta Station,1,\n201,Beta platform,0,200\n"
                "300,Gamma Station,1,\n301,Gamma platform,0,300\n"
                "depot,Depot Station,1,\n"
            ),
        }
        for name, text in files.items():
            path = self.root / (name + ".txt")
            path.write_text(text, encoding="utf-8")
            self.paths[name] = str(path)
        self.paths["stop_times"] = str(self.root / "stop_times.txt")
        for attribute, value in (("MB_VERBOSE", False), ("ROUTE_SHORT_OVERRIDES_PATH", ""), ("ROUTE_SHORT_OVERRIDES_JSON", "")):
            patcher = mock.patch.object(metro, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(metro.os.environ, {"MB_PERMISSIVE_PAIRS": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _load(self, extra=None):
        options = {"route_short_allow": set(), **(extra or {})}
        parent_options = {"agencies_allowed": set(), **options}
        pair_options = {"agencies_allowed": set(), **options}
        stop_paths = {key: path for key, path in self.paths.items() if key != "agency"}
        stop_options = {key: value for key, value in options.items() if key != "agencies_allowed"}
        return (
            metro.require_allowed_parents(**self.paths, **parent_options),
            metro.require_allowed_pairs(**self.paths, **pair_options),
            metro.load_stop_route_pairs(**stop_paths, **stop_options),
        )

    def test_loaders_preserve_filters_and_csv_variants(self):
        rows = (
            ("t1", "101"), ("t2", "201"), ("t3", "301"),
            ("t4", "300"), ("t5", "101"), ("missing", "101"),
            ("t1", "missing"), ("t1", "depot"), ("t1", "101"),
        )
        variants = {
            "standard": "trip_id,stop_id\n" + "".join(f" {t} , {s} \n" for t, s in rows),
            "reordered": "\ufeffstop_id,ignored,trip_id\n" + "".join(f"{s},x,{t}\n" for t, s in rows),
            "duplicates": "trip_id,stop_id,trip_id,stop_id\n" + "".join(f"wrong,999,{t},{s}\n" for t, s in rows),
        }
        for variant, text in variants.items():
            # Include blank, short and overlong records, all accepted by DictReader.
            text += "\n\nshort\n,,\nunknown,missing,extra,extra,extra\n"
            Path(self.paths["stop_times"]).write_text(text, encoding="utf-8")
            for options in ({}, {"route_short_allow": {"M1"}}, {"types": ()}, {"agencies_allowed": {"Bus"}}):
                with self.subTest(variant=variant, options=options):
                    with mock.patch.object(metro, "_iter_stop_time_ids", _dict_reader_ids):
                        expected = self._load(options)
                    self.assertEqual(self._load(options), expected)
            with self.subTest(variant=variant, expected_mappings=True):
                parents, pairs, stops = self._load()
                self.assertEqual(parents, {"100", "200", "300"})
                self.assertEqual(pairs, {("100", "T1"), ("200", "M1")})
                self.assertEqual(stops, {"101": {"T1"}, "201": {"M1"}, "depot": {"T1"}})

    def test_empty_and_missing_columns_preserve_fallbacks(self):
        for text in ("", "trip_id,stop_id\n", "ignored\nvalue\n", "trip_id\nt1\n"):
            Path(self.paths["stop_times"]).write_text(text, encoding="utf-8")
            with self.subTest(table=text):
                with mock.patch.object(metro, "_iter_stop_time_ids", _dict_reader_ids):
                    expected = self._load()
                self.assertEqual(self._load(), expected)
                self.assertEqual(expected[0], {"100", "200", "300"})
                self.assertEqual(expected[2], {})


if __name__ == "__main__":
    unittest.main()
