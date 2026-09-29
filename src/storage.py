"""JSONL 追加式事件存储。

业务事实一旦接收就不原地改写：所有状态变更都是追加事件。
通过文件锁 + 按聚合版本的乐观并发控制检测并发更新冲突。
"""
from __future__ import annotations

import fcntl
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .errors import Conflict, DuplicateEvent


class EventStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # ---- 读取 ----------------------------------------------------------

    def load(self) -> list[dict]:
        """按追加顺序返回全部事件（含全局序号 seq）。"""
        if not self.path.exists():
            return []
        events: list[dict] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    events.append(record)
        return events

    def load_for(self, aggregate_id: str) -> list[dict]:
        return [e for e in self.load() if e["aggregate_id"] == aggregate_id]

    # ---- 写入 ----------------------------------------------------------

    def append(
        self,
        event: dict,
        *,
        expected_version: int | None = None,
    ) -> dict:
        """追加单个事件。

        expected_version 为该聚合当前的最新版本号（新聚合传 0 或 None）。
        """
        return self.append_batch([event], {event["aggregate_id"]: expected_version or 0})[0]

    def append_batch(
        self,
        events: list[dict],
        expected_versions: dict[str, int],
    ) -> list[dict]:
        """原子追加一批事件（可跨聚合），全部成功或全部不写入。

        expected_versions: 每个被写聚合的调用方预期基线版本（新聚合为 0）。
        """
        if not events:
            return []

        with self._lock_path.open("a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                stored = self.load()
                current: dict[str, int] = defaultdict(int)
                known_ids: set[str] = set()
                for record in stored:
                    current[record["aggregate_id"]] = record["version"]
                    known_ids.add(record["event_id"])

                for aggregate_id, expected in expected_versions.items():
                    actual = current[aggregate_id]
                    if actual != expected:
                        raise Conflict(aggregate_id, expected, actual)

                next_version = dict(current)
                stamped: list[dict] = []
                seq = len(stored)
                for event in events:
                    if event["event_id"] in known_ids:
                        raise DuplicateEvent(f"事件已存在: {event['event_id']}")
                    aggregate_id = event["aggregate_id"]
                    expected_next = next_version[aggregate_id] + 1
                    if event["version"] != expected_next:
                        raise Conflict(aggregate_id, event["version"] - 1, next_version[aggregate_id])
                    record = dict(event, seq=seq)
                    stamped.append(record)
                    next_version[aggregate_id] = event["version"]
                    known_ids.add(event["event_id"])
                    seq += 1

                with self.path.open("a", encoding="utf-8") as fh:
                    for record in stamped:
                        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                return stamped
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    # ---- 调试/维护 ------------------------------------------------------

    def truncate_for_tests(self) -> None:
        if self.path.exists():
            self.path.unlink()
