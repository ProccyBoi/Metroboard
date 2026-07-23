import threading
import unittest
from unittest import mock

import requests

import app as metro


def _feed_bytes(*entity_ids: str) -> bytes:
    feed = metro.rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for entity_id in entity_ids:
        feed.entity.add(id=entity_id)
    return feed.SerializeToString()


def _entity_ids(feed: metro.rt.FeedMessage) -> list[str]:
    return [entity.id for entity in feed.entity]


class _Clock:
    def __init__(self) -> None:
        self.monotonic = 100.0
        self.wall = 1_000.0

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += seconds


class _Response:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        error = requests.HTTPError(f"HTTP {self.status_code}")
        error.response = self
        raise error


class _ScriptedSession:
    def __init__(self, responses: dict[str, list[_Response | Exception]]) -> None:
        self.responses = {url: list(items) for url, items in responses.items()}
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.calls.append(url)
        try:
            item = self.responses[url].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"unexpected request for {url}") from exc
        if isinstance(item, Exception):
            raise item
        return item


class FeedCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.saved = {
            "API_TOKEN": metro.API_TOKEN,
            "URLS_VP": metro.URLS_VP,
            "FEED_CACHE_SEC": metro.FEED_CACHE_SEC,
            "FEED_FAILURE_BACKOFF_MAX_SEC": metro.FEED_FAILURE_BACKOFF_MAX_SEC,
            "FEED_STALE_MAX_SEC": metro.FEED_STALE_MAX_SEC,
            "MB_VERBOSE": metro.MB_VERBOSE,
        }
        metro.API_TOKEN = "test-token"
        metro.URLS_VP = ["rail", "metro"]
        metro.FEED_CACHE_SEC = 5
        metro.FEED_FAILURE_BACKOFF_MAX_SEC = 20
        metro.FEED_STALE_MAX_SEC = 90
        metro.MB_VERBOSE = False
        self.clock_patches = [
            mock.patch.object(
                metro, "_feed_now_monotonic", side_effect=lambda: self.clock.monotonic
            ),
            mock.patch.object(
                metro, "_feed_now_wall", side_effect=lambda: self.clock.wall
            ),
            mock.patch.object(metro, "_feed_refresh_jitter_seconds", return_value=0.0),
        ]
        for patcher in self.clock_patches:
            patcher.start()
        self._reset_cache()

    def tearDown(self) -> None:
        self._reset_cache()
        for patcher in reversed(self.clock_patches):
            patcher.stop()
        for name, value in self.saved.items():
            setattr(metro, name, value)

    @staticmethod
    def _reset_cache() -> None:
        with metro._FEED_CONDITION:
            metro._FEED_CACHE = None
            metro._FEED_TS = 0.0
            metro._FEED_LAST_GOOD_BY_URL.clear()
            metro._FEED_LAST_GOOD_TS_BY_URL.clear()
            metro._FEED_NEXT_DUE_BY_URL.clear()
            metro._FEED_FAILURES_BY_URL.clear()
            metro._FEED_LAST_ATTEMPT_TS = 0.0
            metro._FEED_CONSECUTIVE_TOTAL_FAILURES = 0
            metro._FEED_REFRESHING = False
            metro._FEED_CONDITION.notify_all()

    def _use_session(self, session):
        return mock.patch.object(metro, "_session", return_value=session)

    def test_cache_interval_has_a_hard_five_second_floor(self) -> None:
        self.assertEqual(metro._effective_feed_cache_seconds("3"), 5)
        self.assertEqual(metro._effective_feed_cache_seconds("5"), 5)
        self.assertEqual(metro._effective_feed_cache_seconds("9"), 9)
        self.assertEqual(metro._effective_feed_stale_max_seconds("3", 5), 5)
        self.assertEqual(metro._effective_feed_stale_max_seconds("90", 5), 90)

    def test_successful_feed_is_cached_for_five_seconds(self) -> None:
        metro.URLS_VP = ["rail"]
        session = _ScriptedSession({
            "rail": [
                _Response(200, _feed_bytes("rail-old")),
                _Response(200, _feed_bytes("rail-new")),
            ],
        })

        with self._use_session(session):
            first, first_age = metro.fetch_vp_cached()
            self.clock.advance(4.999)
            cached, cached_age = metro.fetch_vp_cached()
            self.clock.advance(0.001)
            refreshed, refreshed_age = metro.fetch_vp_cached()

        self.assertEqual(_entity_ids(first), ["rail-old"])
        self.assertIs(cached, first)
        self.assertAlmostEqual(first_age, 0.0)
        self.assertAlmostEqual(cached_age, 4.999)
        self.assertEqual(_entity_ids(refreshed), ["rail-new"])
        self.assertAlmostEqual(refreshed_age, 0.0)
        self.assertEqual(session.calls, ["rail", "rail"])

    def test_partial_failure_keeps_that_feeds_last_good_snapshot(self) -> None:
        session = _ScriptedSession({
            "rail": [
                _Response(200, _feed_bytes("rail-old")),
                _Response(200, _feed_bytes("rail-new")),
                _Response(200, _feed_bytes("rail-newer")),
            ],
            "metro": [
                _Response(200, _feed_bytes("metro-old")),
                _Response(503),
                _Response(200, _feed_bytes("metro-new")),
            ],
        })

        with self._use_session(session):
            initial, _ = metro.fetch_vp_cached()
            self.clock.advance(5)
            partial, _ = metro.fetch_vp_cached()
            failures_after_partial = metro._FEED_FAILURES_BY_URL["metro"]
            self.clock.advance(5)
            recovered, _ = metro.fetch_vp_cached()

        self.assertEqual(_entity_ids(initial), ["rail-old", "metro-old"])
        self.assertEqual(_entity_ids(partial), ["rail-new", "metro-old"])
        self.assertEqual(_entity_ids(recovered), ["rail-newer", "metro-new"])
        self.assertEqual(session.calls.count("rail"), 3)
        self.assertEqual(session.calls.count("metro"), 3)
        self.assertEqual(failures_after_partial, 1)
        self.assertEqual(metro._FEED_FAILURES_BY_URL["metro"], 0)

    def test_all_feed_failures_keep_last_good_and_back_off(self) -> None:
        session = _ScriptedSession({
            "rail": [
                _Response(200, _feed_bytes("rail-good")),
                _Response(429),
                _Response(503),
            ],
            "metro": [
                _Response(200, _feed_bytes("metro-good")),
                _Response(429),
                _Response(503),
            ],
        })

        with self._use_session(session):
            good, _ = metro.fetch_vp_cached()
            self.clock.advance(5)
            after_first_failure, _ = metro.fetch_vp_cached()
            first_diagnostics = metro._feed_cache_diagnostics()
            self.clock.advance(4.999)
            during_backoff, _ = metro.fetch_vp_cached()
            self.clock.advance(0.001)
            after_second_failure, _ = metro.fetch_vp_cached()
            second_diagnostics = metro._feed_cache_diagnostics()
            self.clock.advance(9.999)
            still_backing_off, _ = metro.fetch_vp_cached()

        expected = ["rail-good", "metro-good"]
        self.assertEqual(_entity_ids(good), expected)
        self.assertEqual(_entity_ids(after_first_failure), expected)
        self.assertEqual(_entity_ids(during_backoff), expected)
        self.assertEqual(_entity_ids(after_second_failure), expected)
        self.assertEqual(_entity_ids(still_backing_off), expected)
        self.assertEqual(session.calls, ["rail", "metro", "rail", "metro", "rail", "metro"])
        self.assertEqual(first_diagnostics["feed_retry_in_seconds"], 5)
        self.assertEqual(first_diagnostics["feed_consecutive_total_failures"], 1)
        self.assertEqual(second_diagnostics["feed_retry_in_seconds"], 10)
        self.assertEqual(second_diagnostics["feed_consecutive_total_failures"], 2)

    def test_cold_start_failure_is_cached_then_recovers_at_normal_speed(self) -> None:
        metro.URLS_VP = ["rail"]
        session = _ScriptedSession({
            "rail": [
                _Response(429),
                _Response(200, _feed_bytes("recovered")),
                _Response(200, _feed_bytes("healthy-refresh")),
            ],
        })

        with self._use_session(session):
            failed, failed_age = metro.fetch_vp_cached()
            health_after_failure = metro.healthz()
            self.clock.advance(4.999)
            cached_failure, _ = metro.fetch_vp_cached()
            self.clock.advance(0.001)
            recovered, recovered_age = metro.fetch_vp_cached()
            health_after_recovery = metro.healthz()
            self.clock.advance(5)
            healthy_refresh, _ = metro.fetch_vp_cached()

        self.assertEqual(_entity_ids(failed), [])
        self.assertIs(cached_failure, failed)
        self.assertEqual(failed_age, 0.0)
        self.assertIsNone(health_after_failure["trains_last_fetch_s"])
        self.assertEqual(health_after_failure["feed_cache_seconds"], 5)
        self.assertEqual(health_after_failure["feed_consecutive_total_failures"], 1)
        self.assertEqual(_entity_ids(recovered), ["recovered"])
        self.assertEqual(recovered_age, 0.0)
        self.assertEqual(health_after_recovery["feed_consecutive_total_failures"], 0)
        self.assertEqual(_entity_ids(healthy_refresh), ["healthy-refresh"])
        self.assertEqual(session.calls, ["rail", "rail", "rail"])

    def test_expired_last_good_is_excluded_without_resetting_backoff(self) -> None:
        metro.URLS_VP = ["rail"]
        session = _ScriptedSession({
            "rail": [_Response(200, _feed_bytes("old-position"))],
        })

        with self._use_session(session):
            fresh, _ = metro.fetch_vp_cached()
            with metro._FEED_CONDITION:
                metro._FEED_FAILURES_BY_URL["rail"] = 4
                metro._FEED_NEXT_DUE_BY_URL["rail"] = self.clock.monotonic + 200
                retry_at = metro._FEED_NEXT_DUE_BY_URL["rail"]
            self.clock.advance(90)
            boundary, boundary_age = metro.fetch_vp_cached()
            self.clock.advance(0.001)
            expired, expired_age = metro.fetch_vp_cached()
            health = metro.healthz()

        self.assertEqual(_entity_ids(fresh), ["old-position"])
        self.assertEqual(_entity_ids(boundary), ["old-position"])
        self.assertEqual(boundary_age, 90)
        self.assertEqual(_entity_ids(expired), [])
        self.assertEqual(expired_age, 0)
        self.assertIsNone(health["trains_last_fetch_s"])
        self.assertEqual(health["feed_stale_max_seconds"], 90)
        self.assertEqual(metro._FEED_FAILURES_BY_URL["rail"], 4)
        self.assertEqual(metro._FEED_NEXT_DUE_BY_URL["rail"], retry_at)
        self.assertEqual(session.calls, ["rail"])

    def test_concurrent_stale_callers_share_one_refresh(self) -> None:
        metro.URLS_VP = ["rail"]
        initial_session = _ScriptedSession({
            "rail": [_Response(200, _feed_bytes("stale"))],
        })
        with self._use_session(initial_session):
            stale, _ = metro.fetch_vp_cached()
        self.clock.advance(5)

        entered = threading.Event()
        release = threading.Event()

        class _BlockingSession:
            calls = 0

            def get(self, _url: str, **_kwargs) -> _Response:
                self.calls += 1
                entered.set()
                if not release.wait(timeout=2):
                    raise AssertionError("test did not release blocked refresh")
                return _Response(200, _feed_bytes("fresh"))

        blocking_session = _BlockingSession()
        results: dict[str, metro.rt.FeedMessage] = {}

        def fetch(name: str) -> None:
            results[name] = metro.fetch_vp_cached()[0]

        with self._use_session(blocking_session):
            refresher = threading.Thread(target=fetch, args=("refresher",))
            follower = threading.Thread(target=fetch, args=("follower",))
            refresher.start()
            self.assertTrue(entered.wait(timeout=1))
            follower.start()
            follower.join(timeout=1)
            try:
                self.assertFalse(follower.is_alive())
                self.assertEqual(_entity_ids(results["follower"]), ["stale"])
            finally:
                release.set()
                refresher.join(timeout=2)
                follower.join(timeout=2)

        self.assertFalse(refresher.is_alive())
        self.assertEqual(_entity_ids(stale), ["stale"])
        self.assertEqual(_entity_ids(results["refresher"]), ["fresh"])
        self.assertEqual(blocking_session.calls, 1)

    def test_http_session_does_not_multiply_failed_requests(self) -> None:
        session = metro._session()
        retries = session.get_adapter("https://").max_retries
        self.assertEqual(retries.total, 0)
        self.assertEqual(retries.status, 0)

    def test_refresh_uses_bounded_jitter_before_upstream_calls(self) -> None:
        metro.URLS_VP = ["rail"]
        session = _ScriptedSession({
            "rail": [_Response(200, _feed_bytes("rail"))],
        })

        with (
            self._use_session(session),
            mock.patch.object(
                metro, "_feed_refresh_jitter_seconds", return_value=0.125
            ),
            mock.patch.object(metro.time, "sleep") as sleep,
        ):
            metro.fetch_vp_cached()

        sleep.assert_called_once_with(0.125)
        self.assertEqual(session.calls, ["rail"])


if __name__ == "__main__":
    unittest.main()
