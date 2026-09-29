"""事件存储与乐观并发控制测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.store import EventStore, VersionConflict

EVENTS = ["A_HAPPENED", "B_HAPPENED"]


class EventStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "log.jsonl"
        self.store = EventStore(self.path, EVENTS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_version_starts_at_one_and_increments(self) -> None:
        e1 = self.store.append("A_HAPPENED", "agg-1", {"x": 1}, 0)
        e2 = self.store.append("A_HAPPENED", "agg-1", {"x": 2}, 1)
        self.assertEqual((e1["version"], e2["version"]), (1, 2))
        self.assertEqual(self.store.version_of("agg-1"), 2)
        self.assertEqual(self.store.version_of("missing"), 0)

    def test_stale_base_version_conflicts(self) -> None:
        self.store.append("A_HAPPENED", "agg-1", {}, 0)
        with self.assertRaises(VersionConflict) as ctx:
            self.store.append("A_HAPPENED", "agg-1", {}, 0)
        self.assertEqual(ctx.exception.current_version, 1)

    def test_conflict_does_not_write_or_advance(self) -> None:
        self.store.append("A_HAPPENED", "agg-1", {}, 0)
        with self.assertRaises(VersionConflict):
            self.store.append("A_HAPPENED", "agg-1", {}, 0)
        self.assertEqual(self.store.version_of("agg-1"), 1)
        self.assertEqual(len(self.store.events()), 1)

    def test_concurrent_appenders_only_one_wins(self) -> None:
        import threading

        self.store.append("A_HAPPENED", "agg-1", {}, 0)
        results: list[object] = []

        def append_at_v1() -> None:
            try:
                self.store.append("B_HAPPENED", "agg-1", {}, 1)
                results.append("ok")
            except VersionConflict:
                results.append("conflict")

        threads = [threading.Thread(target=append_at_v1) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("conflict"), 4)
        self.assertEqual(self.store.version_of("agg-1"), 2)

    def test_independent_aggregates_have_independent_versions(self) -> None:
        self.store.append("A_HAPPENED", "a", {}, 0)
        self.store.append("A_HAPPENED", "b", {}, 0)
        self.store.append("A_HAPPENED", "b", {}, 1)
        self.assertEqual(self.store.version_of("a"), 1)
        self.assertEqual(self.store.version_of("b"), 2)

    def test_persistence_and_replay(self) -> None:
        self.store.append("A_HAPPENED", "a", {"v": 1}, 0)
        self.store.append("B_HAPPENED", "b", {"v": 2}, 0)
        reloaded = EventStore(self.path, EVENTS)
        self.assertEqual(reloaded.version_of("a"), 1)
        self.assertEqual(reloaded.version_of("b"), 1)
        self.assertEqual([e["event_type"] for e in reloaded.events()],
                         ["A_HAPPENED", "B_HAPPENED"])

    def test_as_of_filters_by_occurrence_time(self) -> None:
        self.store.append("A_HAPPENED", "a", {}, 0,
                          occurred_at="2026-01-01T00:00:00+08:00")
        self.store.append("A_HAPPENED", "a", {}, 1,
                          occurred_at="2026-03-01T00:00:00+08:00")
        self.assertEqual(len(self.store.events("2026-02-01T00:00:00+08:00")), 1)
        self.assertEqual(len(self.store.events("2026-01-01T00:00:00+08:00")), 1)

    def test_invalid_event_is_rejected_before_write(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append("UNKNOWN", "a", {}, 0)
        self.assertFalse(self.path.exists())

    def test_log_is_valid_jsonl(self) -> None:
        self.store.append("A_HAPPENED", "a", {"中文": "值"}, 0)
        line = self.path.read_text(encoding="utf-8").strip()
        self.assertEqual(json.loads(line)["payload"], {"中文": "值"})


if __name__ == "__main__":
    unittest.main()
