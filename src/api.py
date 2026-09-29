"""主张库 HTTP 接口（仅依赖标准库）。

路由概览（详见 README）：

* ``POST /claims``、``GET /claims/{id}``、``POST /claims/{id}/evidence``、
  ``POST /claims/{id}/reviews``、``POST /claims/{id}/withdraw``
* ``POST /exhibits``、``GET /exhibits/{id}``、``POST /exhibits/{id}/revise``、
  ``POST /exhibits/{id}/release``、``.../reopen``、``.../resolve``、
  ``.../withdraw``、``GET .../evidence-graph``、``GET .../dispute-summary``
* ``POST /activities``、``GET /activities/{id}/readiness``
* ``GET /state?as_of=...``：研究者重放任意历史时点

并发更新提交过期基准版本时返回 ``409``，响应体带当前版本供重试。
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from src.service import (
    CLAIM_KINDS,
    EVIDENCE_TIERS,
    REVIEW_DECISIONS,
    ClaimService,
    DomainError,
    NotFound,
)
from src.store import EventStore, VersionConflict

# 展签/活动种类不作为可登记 claim 类型暴露给 /claims。
_CLAIM_KINDS_EXPOSED = tuple(k for k in CLAIM_KINDS
                             if k not in ("exhibit_text", "education_activity"))


def create_server(host: str, port: int, store: EventStore) -> ThreadingHTTPServer:
    service = ClaimService(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaimAdjudication/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        # ------------------------------------------------------------ 基础

        def _send_json(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise _BadRequest(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise _BadRequest("请求体必须是 JSON 对象")
            return body

        def _expect(self, body: dict[str, Any], key: str) -> Any:
            if key not in body:
                raise _BadRequest(f"缺少字段: {key}")
            return body[key]

        def _opt_int(self, body: dict[str, Any], key: str) -> int | None:
            if key not in body or body[key] is None:
                return None
            value = body[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise _BadRequest(f"{key} 必须是整数")
            return value

        def _run(self, fn: Callable[[], Any], *, status: int = HTTPStatus.OK) -> None:
            try:
                result = fn()
            except _BadRequest as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except NotFound as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except DomainError as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            except VersionConflict as exc:
                self._send_json(HTTPStatus.CONFLICT, {
                    "error": str(exc),
                    "aggregate_id": exc.aggregate_id,
                    "expected_version": exc.expected_version,
                    "current_version": exc.current_version,
                })
            else:
                self._send_json(status, result if result is not None else {"ok": True})

        # ------------------------------------------------------------ 路由

        def do_GET(self) -> None:  # noqa: N802
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._run(lambda: self._route_get(parts, query))

        def do_POST(self) -> None:  # noqa: N802
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            self._run(lambda: self._route_post(parts))

        # ---- GET ----

        def _route_get(self, parts: list[str], query: dict[str, str]) -> Any:
            if parts == ["state"]:
                return service.state(as_of=query.get("as_of"))
            if len(parts) == 2 and parts[0] == "claims":
                return service.get_claim(parts[1])
            if len(parts) == 2 and parts[0] == "exhibits":
                return service.get_exhibit_text(parts[1])
            if len(parts) == 3 and parts[0] == "exhibits" and parts[2] == "evidence-graph":
                release_no = query.get("release_no")
                return service.evidence_graph(
                    parts[1],
                    release_no=int(release_no) if release_no is not None else None,
                    as_of=query.get("as_of"),
                )
            if len(parts) == 3 and parts[0] == "exhibits" and parts[2] == "dispute-summary":
                release_no = query.get("release_no")
                return service.dispute_summary(
                    parts[1],
                    release_no=int(release_no) if release_no is not None else None,
                    as_of=query.get("as_of"),
                )
            if (len(parts) == 3 and parts[0] == "activities"
                    and parts[2] == "readiness"):
                return service.activity_readiness(parts[1])
            raise NotFound(f"无此路径: /{'/'.join(parts)}")

        # ---- POST ----

        def _route_post(self, parts: list[str]) -> Any:
            body = self._read_body()
            if parts == ["claims"]:
                kind = self._expect(body, "kind")
                if kind not in _CLAIM_KINDS_EXPOSED:
                    raise _BadRequest(
                        f"kind 必须是以下之一: {', '.join(_CLAIM_KINDS_EXPOSED)}"
                    )
                if "tier" in body and body["tier"] not in EVIDENCE_TIERS:
                    raise _BadRequest(
                        f"tier 必须是以下之一: {', '.join(EVIDENCE_TIERS)}"
                    )
                claim_id = service.register_claim(
                    kind=kind,
                    statement=self._expect(body, "statement"),
                    discipline=self._expect(body, "discipline"),
                    tier=body.get("tier"),
                    source_ref=body.get("source_ref"),
                    registered_by=body.get("registered_by"),
                    depends_on=body.get("depends_on", []),
                    conflicts_with=body.get("conflicts_with", []),
                    supersedes=body.get("supersedes", []),
                    note=body.get("note", ""),
                    claim_id=body.get("claim_id"),
                    expected_version=self._opt_int(body, "expected_version") or 0,
                    occurred_at=body.get("occurred_at"),
                )
                return {"claim_id": claim_id,
                        "version": store.version_of(claim_id)}

            if len(parts) == 3 and parts[0] == "claims" and parts[2] == "evidence":
                service.link_evidence(
                    parts[1],
                    kind=self._expect(body, "kind"),
                    citation=self._expect(body, "citation"),
                    linked_by=body.get("linked_by"),
                    note=body.get("note", ""),
                    expected_version=self._opt_int(body, "expected_version"),
                    occurred_at=body.get("occurred_at"),
                )
                return {"ok": True, "version": store.version_of(parts[1])}

            if len(parts) == 3 and parts[0] == "claims" and parts[2] == "reviews":
                decision = self._expect(body, "decision")
                if decision not in REVIEW_DECISIONS:
                    raise _BadRequest(
                        f"decision 必须是以下之一: {', '.join(REVIEW_DECISIONS)}"
                    )
                channel = service.record_review(
                    parts[1],
                    discipline=self._expect(body, "discipline"),
                    decision=decision,
                    reviewer=self._expect(body, "reviewer"),
                    note=body.get("note", ""),
                    expected_version=self._opt_int(body, "expected_version"),
                    occurred_at=body.get("occurred_at"),
                )
                return {"review_channel": channel,
                        "version": store.version_of(channel)}

            if len(parts) == 3 and parts[0] == "claims" and parts[2] == "withdraw":
                service.withdraw_claim(
                    parts[1],
                    reason=self._expect(body, "reason"),
                    by=body.get("by"),
                    expected_version=self._opt_int(body, "expected_version"),
                    occurred_at=body.get("occurred_at"),
                )
                return {"ok": True, "version": store.version_of(parts[1])}

            if parts == ["exhibits"]:
                exhibit_id = service.create_exhibit_text(
                    label=self._expect(body, "label"),
                    refs=self._expect(body, "refs"),
                    created_by=body.get("created_by"),
                    note=body.get("note", ""),
                    exhibit_id=body.get("exhibit_id"),
                    expected_version=self._opt_int(body, "expected_version") or 0,
                    occurred_at=body.get("occurred_at"),
                )
                return {"exhibit_id": exhibit_id,
                        "version": store.version_of(exhibit_id)}

            if len(parts) == 3 and parts[0] == "exhibits":
                exhibit_id, action = parts[1], parts[2]
                if action == "revise":
                    service.revise_exhibit_text(
                        exhibit_id, refs=self._expect(body, "refs"),
                        reason=body.get("reason", ""), by=body.get("by"),
                        expected_version=self._opt_int(body, "expected_version"),
                        occurred_at=body.get("occurred_at"),
                    )
                elif action == "release":
                    result = service.release_exhibit_text(
                        exhibit_id, released_by=body.get("released_by"),
                        expected_version=self._opt_int(body, "expected_version"),
                        occurred_at=body.get("occurred_at"),
                    )
                    return {**result, "version": store.version_of(exhibit_id)}
                elif action == "reopen":
                    service.reopen_exhibit_text(
                        exhibit_id, reason=self._expect(body, "reason"),
                        by=body.get("by"),
                        expected_version=self._opt_int(body, "expected_version"),
                        occurred_at=body.get("occurred_at"),
                    )
                elif action == "resolve":
                    service.resolve_reopened(
                        exhibit_id, decision=self._expect(body, "decision"),
                        note=self._expect(body, "note"), by=body.get("by"),
                        expected_version=self._opt_int(body, "expected_version"),
                        occurred_at=body.get("occurred_at"),
                    )
                elif action == "withdraw":
                    service.withdraw_exhibit_text(
                        exhibit_id, reason=self._expect(body, "reason"),
                        by=body.get("by"),
                        expected_version=self._opt_int(body, "expected_version"),
                        occurred_at=body.get("occurred_at"),
                    )
                else:
                    raise NotFound(f"无此展签操作: {action}")
                return {"ok": True, "version": store.version_of(exhibit_id)}

            if parts == ["activities"]:
                activity_id = service.register_activity(
                    title=self._expect(body, "title"),
                    refs=self._expect(body, "refs"),
                    by=body.get("by"), note=body.get("note", ""),
                    activity_id=body.get("activity_id"),
                    expected_version=self._opt_int(body, "expected_version") or 0,
                    occurred_at=body.get("occurred_at"),
                )
                return {"activity_id": activity_id,
                        "version": store.version_of(activity_id)}

            raise NotFound(f"无此路径: /{'/'.join(parts)}")

    return ThreadingHTTPServer((host, port), Handler)


class _BadRequest(ValueError):
    pass


def main(argv: list[str] | None = None) -> int:
    from src.service import EVENT_TYPES

    parser = argparse.ArgumentParser(description="罗盘展陈主张裁决库服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default="data/events.jsonl",
                        help="只增事件日志路径（JSONL）")
    args = parser.parse_args(argv)

    store = EventStore(args.store, EVENT_TYPES)
    server = create_server(args.host, args.port, store)
    print(f"主张库服务监听 http://{args.host}:{args.port}（日志 {args.store}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
