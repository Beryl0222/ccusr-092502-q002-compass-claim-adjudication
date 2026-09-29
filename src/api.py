"""HTTP 接口（标准库实现，无第三方依赖）。

并发更新由调用方在请求体里带 ``expected_version``（聚合当前版本，新建传 0）；
存储层检测到不一致时返回 409，客户端重读后可重试。
"""
from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import (
    Conflict,
    DomainError,
    DuplicateEvent,
    GateRejected,
    NotFound,
    ValidationFailure,
)
from .service import ClaimService
from .storage import EventStore


def _error_status(exc: DomainError) -> int:
    if isinstance(exc, NotFound):
        return HTTPStatus.NOT_FOUND
    if isinstance(exc, (Conflict, DuplicateEvent)):
        return HTTPStatus.CONFLICT
    if isinstance(exc, GateRejected):
        return HTTPStatus.UNPROCESSABLE_ENTITY
    if isinstance(exc, ValidationFailure):
        return HTTPStatus.BAD_REQUEST
    return HTTPStatus.INTERNAL_SERVER_ERROR


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CompassClaimAPI/1.0"

    # ---- 框架辅助 ------------------------------------------------------

    def _send_json(self, status: int, body: dict | list) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationFailure("请求体不是合法 JSON")
        if not isinstance(value, dict):
            raise ValidationFailure("请求体必须是 JSON 对象")
        return value

    def _handle(self, fn) -> None:
        try:
            fn()
        except DomainError as exc:
            status = _error_status(exc)
            payload: dict = {"error": exc.code, "message": exc.message}
            if exc.details:
                payload["details"] = exc.details
            self._send_json(status, payload)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接挂死
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": "internal_error", "message": str(exc)})

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        if os.environ.get("COMPASS_API_LOG"):
            super().log_message(fmt, *args)

    @property
    def service(self) -> ClaimService:
        return self.server.service  # type: ignore[attr-defined]

    # ---- 路由 ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle(self._route_get)

    def do_POST(self) -> None:  # noqa: N802
        self._handle(self._route_post)

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle(self._route_delete)

    def _parts(self) -> list[str]:
        return [p for p in urlparse(self.path).path.split("/") if p]

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items()}

    def _route_get(self) -> None:
        parts = self._parts()
        q = self._query()
        svc = self.service

        if parts == ["claims"]:
            self._send_json(200, {"claims": svc.list_claims()})
        elif len(parts) == 2 and parts[0] == "claims":
            claim = svc.get_claim(parts[1])
            self._send_json(200, claim)
        elif len(parts) == 3 and parts[0] == "claims" and parts[2] == "impact":
            self._send_json(200, svc.impact_preview(parts[1]))
        elif parts == ["labels"]:
            self._send_json(200, {"labels": svc.list_labels()})
        elif len(parts) == 2 and parts[0] == "labels":
            labels = {l["label_id"]: l for l in svc.list_labels()}
            if parts[1] not in labels:
                raise NotFound(f"展签不存在: {parts[1]}")
            self._send_json(200, labels[parts[1]])
        elif len(parts) == 4 and parts[0] == "labels" and parts[2] == "releases":
            self._send_json(200, svc.get_label_release(parts[1], int(parts[3])))
        elif len(parts) == 3 and parts[0] == "labels" and parts[2] == "report":
            rv = int(q["release_version"]) if q.get("release_version") else None
            self._send_json(200, svc.label_evidence_report(parts[1], rv))
        elif parts == ["state"]:
            if "as_of" not in q:
                raise ValidationFailure("state 查询需要 as_of 参数（ISO 8601 含时区）")
            self._send_json(200, svc.state_at(q["as_of"]))
        elif parts == ["events"]:
            self._send_json(200, {"events": svc.event_log()})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": self.path})

    def _route_post(self) -> None:
        parts = self._parts()
        body = self._read_json()
        svc = self.service

        if parts == ["claims"]:
            self._send_json(201, svc.register_claim(
                kind=body["kind"], statement=body["statement"], tier=body["tier"],
                source_ref=body.get("source_ref", ""),
                registered_by=body.get("registered_by", ""),
                expected_version=body.get("expected_version", 0),
                claim_id=body.get("claim_id"),
            ))
        elif len(parts) == 3 and parts[0] == "claims" and parts[2] == "supersede":
            self._send_json(200, svc.supersede_claim(
                parts[1], reason=body.get("reason", ""), by=body.get("by", ""),
                statement=body.get("statement"), tier=body.get("tier"),
                source_ref=body.get("source_ref"),
                expected_version=body.get("expected_version"),
            ))
        elif len(parts) == 3 and parts[0] == "claims" and parts[2] == "relations":
            self._send_json(200, svc.link_relation(
                parts[1], relation=body["relation"], to_claim_id=body["to_claim_id"],
                note=body.get("note", ""), expected_version=body.get("expected_version"),
            ))
        elif len(parts) == 3 and parts[0] == "claims" and parts[2] == "evidence":
            self._send_json(200, svc.link_evidence(
                parts[1], tier=body["tier"], kind=body.get("kind", ""),
                citation=body.get("citation", ""), summary=body.get("summary", ""),
                linked_by=body.get("linked_by", ""), evidence_id=body.get("evidence_id"),
                expected_version=body.get("expected_version"),
            ))
        elif len(parts) == 3 and parts[0] == "subjects" and parts[2] == "reviews":
            self._send_json(200, svc.record_review(
                parts[1], discipline=body["discipline"], outcome=body["outcome"],
                reviewer=body.get("reviewer", ""), note=body.get("note", ""),
                expected_version=body.get("expected_version"),
            ))
        elif len(parts) == 5 and parts[0] == "subjects" and parts[2] == "reviews" \
                and parts[4] == "revoke":
            self._send_json(200, svc.revoke_review(
                parts[1], parts[3], reason=body.get("reason", ""),
                revoked_by=body.get("revoked_by", ""),
                expected_version=body.get("expected_version"),
            ))
        elif parts == ["labels"]:
            self._send_json(201, svc.draft_label(
                title=body["title"], body=body.get("body", ""),
                author=body.get("author", ""), label_id=body.get("label_id"),
                expected_version=body.get("expected_version", 0),
            ))
        elif len(parts) == 3 and parts[0] == "labels" and parts[2] == "citations":
            self._send_json(200, svc.cite_claim(
                parts[1], claim_id=body["claim_id"], how_used=body.get("how_used", ""),
                expected_version=body.get("expected_version"),
            ))
        elif len(parts) == 3 and parts[0] == "labels" and parts[2] == "revise":
            self._send_json(200, svc.revise_label(
                parts[1], title=body.get("title"), body=body.get("body"),
                note=body.get("note", ""), by=body.get("by", ""),
                expected_version=body.get("expected_version"),
            ))
        elif len(parts) == 3 and parts[0] == "labels" and parts[2] == "release":
            self._send_json(200, svc.release_label(
                parts[1], dispute_note=body.get("dispute_note", ""),
                expected_version=body.get("expected_version"),
            ))
        elif len(parts) == 3 and parts[0] == "labels" and parts[2] == "archive":
            self._send_json(200, svc.archive_label(
                parts[1], reason=body.get("reason", ""), by=body.get("by", ""),
                expected_version=body.get("expected_version"),
            ))
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": self.path})

    def _route_delete(self) -> None:
        parts = self._parts()
        body = self._read_json()
        svc = self.service
        # DELETE /labels/{id}/citations/{claim_id} —— 撤回引用，记录保留
        if len(parts) == 4 and parts[0] == "labels" and parts[2] == "citations":
            self._send_json(200, svc.withdraw_citation(
                parts[1], parts[3], reason=body.get("reason", ""),
                expected_version=body.get("expected_version"),
            ))
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": self.path})


def build_server(store_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = ClaimService(EventStore(store_path))  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="罗盘展陈主张裁决库 API")
    parser.add_argument("--store", default="data/events.jsonl", help="事件日志路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = build_server(args.store, args.host, args.port)
    print(f"主张裁决库接口已启动：http://{args.host}:{args.port}  日志：{args.store}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
