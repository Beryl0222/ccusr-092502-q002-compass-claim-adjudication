"""只增事件日志与按聚合版本的乐观并发控制。

日志为单行 JSON（JSONL）。每次追加都在进程内锁与文件锁双重保护下
重新核对该聚合的当前版本：expected_version 与当前版本不一致即拒绝，
从而让并发更新在提交点暴露冲突，而不是后写覆盖先写。
"""
from __future__ import annotations

import fcntl
import json
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.envelope import validate_event


class VersionConflict(RuntimeError):
    """提交的基准版本已过期，调用方须重读后重试。"""

    def __init__(self, aggregate_id: str, expected: int, current: int) -> None:
        super().__init__(
            f"聚合 {aggregate_id} 版本冲突：期望基于 v{expected}，当前已是 v{current}"
        )
        self.aggregate_id = aggregate_id
        self.expected_version = expected
        self.current_version = current


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: str | Path, allowed_events: Iterable[str]) -> None:
        self.path = Path(path)
        self.allowed_events = set(allowed_events)
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._versions: dict[str, int] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events = self._load()

    def _read_disk(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.path.exists():
            return events
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    def _rebuild_versions(self) -> None:
        self._versions = {}
        for record in self._events:
            agg = record["aggregate_id"]
            self._versions[agg] = max(self._versions.get(agg, 0), record["version"])

    def _load(self) -> list[dict[str, Any]]:
        events = self._read_disk()
        for record in events:
            agg = record["aggregate_id"]
            self._versions[agg] = max(self._versions.get(agg, 0), record["version"])
        return events

    def append(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        expected_version: int,
        *,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """以 expected_version 为基准追加事件；冲突时抛 VersionConflict。

        expected_version=0 且聚合不存在表示新建；否则必须等于当前版本。
        """
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("expected_version 必须是整数")
        with self._lock:
            current = self._versions.get(aggregate_id, 0)
            if expected_version != current:
                raise VersionConflict(aggregate_id, expected_version, current)
            record = {
                "event_id": event_id or f"{event_type.lower()}-{aggregate_id}-v{current + 1}",
                "event_type": event_type,
                "occurred_at": occurred_at or utcnow_iso(),
                "aggregate_id": aggregate_id,
                "version": current + 1,
                "payload": payload,
            }
            # 先校验再触碰文件，非法事件不得产生空日志文件。
            errors = validate_event(record, self.allowed_events)
            if errors:
                raise ValueError("; ".join(errors))
            with self.path.open("a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                # 持文件锁后以磁盘为准重放：覆盖其他进程的并发追加。
                disk_events = self._read_disk()
                disk_current = max(
                    (e["version"] for e in disk_events
                     if e["aggregate_id"] == aggregate_id),
                    default=0,
                )
                if disk_current != current:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    self._events = disk_events
                    self._rebuild_versions()
                    raise VersionConflict(aggregate_id, expected_version, disk_current)
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            # 合并磁盘上可能由其他进程写入的事件，再纳入本次事件。
            self._events = disk_events
            self._events.append(record)
            self._rebuild_versions()
            return dict(record)

    def events(self, until: str | None = None) -> list[dict[str, Any]]:
        """返回事件副本；until 给定时只含发生时间不晚于该点的事件。"""
        if until is None:
            return [dict(e) for e in self._events]
        return [dict(e) for e in self._events if e["occurred_at"] <= until]

    def version_of(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)
