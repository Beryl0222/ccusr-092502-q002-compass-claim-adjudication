"""HTTP 接口端到端测试：真实起服务，走 socket 发请求。"""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from src.api import ApiHandler
from src.storage import EventStore
from src.service import ClaimService


def _make_server(store_path: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    server.service = ClaimService(EventStore(store_path))  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._thread = thread  # type: ignore[attr-defined]
    return server


class Client:
    def __init__(self, server: ThreadingHTTPServer) -> None:
        self.host, self.port = server.server_address

    def request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=data,
                     headers={"Content-Type": "application/json"} if data else {})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        payload = json.loads(raw) if raw else {}
        return resp.status, payload


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.store_path = os.path.join(self.dir, "events.jsonl")
        self.server = _make_server(self.store_path)
        self.api = Client(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path: str, body: dict):
        return self.api.request("POST", path, body)

    def test_claim_lifecycle_and_409_on_stale_version(self) -> None:
        status, claim = self._post("/claims", {
            "claim_id": "c1", "kind": "scholar_opinion",
            "statement": "司南为磁性指向器（传统说）", "tier": "DISPUTED",
        })
        self.assertEqual(status, 201)
        self.assertEqual(claim["version"], 1)

        # 两个并发编辑都拿版本 1 做基线，后到者必须收到冲突
        s_ok, _ = self.api.request("POST", "/claims/c1/evidence", {
            "tier": "HISTORICAL_RECORD", "summary": "古籍条目", "expected_version": 1})
        self.assertEqual(s_ok, 200)
        s_conflict, body = self.api.request("POST", "/claims/c1/evidence", {
            "tier": "EXPERIMENTAL_SUPPORT", "summary": "并发附证据", "expected_version": 1})
        self.assertEqual(s_conflict, 409)
        self.assertEqual(body["error"], "version_conflict")
        self.assertEqual(body["details"]["actual"], 2)

        # 重读拿到新版本再提交即可成功
        status, fresh = self.api.request("GET", "/claims/c1")
        s_retry, _ = self.api.request("POST", "/claims/c1/evidence", {
            "tier": "EXPERIMENTAL_SUPPORT", "summary": "基于新版本补证据",
            "expected_version": fresh["version"]})
        self.assertEqual(s_retry, 200)

    def test_full_label_flow_gate_report_and_release(self) -> None:
        self._post("/claims", {
            "claim_id": "lit", "kind": "literature_excerpt",
            "statement": "《论衡·是应》司南之杓", "tier": "HISTORICAL_RECORD",
            "source_ref": "论衡·是应",
        })
        self._post("/claims/lit/evidence", {
            "tier": "HISTORICAL_RECORD", "kind": "古籍原文",
            "citation": "论衡·是应", "summary": "其柢指南"})
        self.assertEqual(self._post("/subjects/lit/reviews", {
            "discipline": "philology", "outcome": "APPROVED", "reviewer": "文献学家"})[0], 200)

        self._post("/claims", {
            "claim_id": "opp", "kind": "scholar_opinion",
            "statement": "司南可能非磁性器", "tier": "DISPUTED"})
        self.assertEqual(self._post("/subjects/opp/reviews", {
            "discipline": "history_of_technology", "outcome": "APPROVED"})[0], 200)
        self._post("/claims/opp/relations", {
            "relation": "contradicts", "to_claim_id": "lit"})

        self._post("/labels", {"label_id": "l1", "title": "司南"})
        self._post("/labels/l1/citations", {"claim_id": "lit"})
        self._post("/labels/l1/citations", {"claim_id": "opp"})

        # 没有展陈复核 + 没有争议并呈说明 → 422
        status, rejected = self._post("/labels/l1/release", {})
        self.assertEqual(status, 422)
        self.assertTrue(any("museology" in e for e in rejected["details"]["errors"]))
        self.assertTrue(any("争议并呈" in e for e in rejected["details"]["errors"]))
        self.assertTrue(rejected["details"]["disputes"])

        self._post("/subjects/l1/reviews", {
            "discipline": "museology", "outcome": "APPROVED", "reviewer": "展陈专家"})
        status, label = self._post("/labels/l1/release",
                                   {"dispute_note": "两说并存，本展不作定论"})
        self.assertEqual(status, 200)
        self.assertEqual(label["current_release_version"], 1)

        # 策展人证据图与争议摘要
        status, report = self.api.request(
            "GET", "/labels/l1/report?release_version=1")
        self.assertEqual(status, 200)
        node_types = {n["node_type"] for n in report["evidence_graph"]["nodes"]}
        self.assertEqual(node_types, {"claim", "evidence"})
        self.assertTrue(any(d["type"] == "contradiction" for d in report["disputes"]))

        # 冻结快照可直接取
        status, snap = self.api.request("GET", "/labels/l1/releases/1")
        self.assertEqual(status, 200)
        self.assertEqual(snap["dispute_note"], "两说并存，本展不作定论")

    def test_supersede_impact_and_review_revocation_audit(self) -> None:
        self._post("/claims", {"claim_id": "a", "kind": "artifact_dating",
                               "statement": "断东汉", "tier": "PHYSICAL_EVIDENCE"})
        self._post("/subjects/a/reviews", {"discipline": "archaeology", "outcome": "APPROVED"})
        self._post("/claims", {"claim_id": "b", "kind": "reconstruction_plan",
                               "statement": "磁勺复原", "tier": "EXPERIMENTAL_SUPPORT"})
        self._post("/claims/b/relations", {"relation": "derived_from", "to_claim_id": "a"})
        self._post("/subjects/b/reviews",
                   {"discipline": "history_of_technology", "outcome": "APPROVED"})
        self._post("/labels", {"label_id": "l1", "title": "复原"})
        self._post("/labels/l1/citations", {"claim_id": "b"})
        self._post("/subjects/l1/reviews", {"discipline": "museology", "outcome": "APPROVED"})
        self.assertEqual(self._post("/labels/l1/release", {})[0], 200)

        # 重断代：L1 被打回
        status, result = self._post("/claims/a/supersede", {
            "reason": "热释光改断西晋", "statement": "改断西晋"})
        self.assertEqual(status, 200)
        self.assertEqual(result["reopened_labels"][0]["label_id"], "l1")

        # 撤销旧复核：审议记录仍在
        status, claim_a = self.api.request("GET", "/claims/a")
        rev = claim_a["reviews"][0]["review_id"]
        status, revoked = self.api.request(
            "POST", f"/subjects/a/reviews/{rev}/revoke",
            {"reason": "原始测量记录存疑"})
        self.assertEqual(status, 200)
        self.assertFalse(revoked["reviews"][0]["active"])
        self.assertEqual(revoked["reviews"][0]["revoke_reason"], "原始测量记录存疑")

        # 事件流完整可审计
        status, log = self.api.request("GET", "/events")
        self.assertEqual(status, 200)
        types = [e["event_type"] for e in log["events"]]
        self.assertIn("IMPACT_REOPENED", types)
        self.assertIn("REVIEW_REVOKED", types)

    def test_state_time_travel(self) -> None:
        self._post("/claims", {"claim_id": "c1", "kind": "scholar_opinion",
                               "statement": "一说", "tier": "DISPUTED"})
        status, before = self.api.request("GET", "/state?as_of=2030-01-01T00:00:00%2B08:00")
        self.assertEqual(status, 200)
        self.assertIn("c1", before["claims"])
        status, ancient = self.api.request("GET", "/state?as_of=2000-01-01T00:00:00%2B08:00")
        self.assertEqual(ancient["claims"], {})

    def test_unknown_route_and_missing_entity(self) -> None:
        self.assertEqual(self.api.request("GET", "/claims/nope")[0], 404)
        self.assertEqual(self.api.request("GET", "/wat")[0], 404)


if __name__ == "__main__":
    unittest.main()
