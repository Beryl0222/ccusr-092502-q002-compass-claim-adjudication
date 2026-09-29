"""HTTP 接口端到端测试。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.api import create_server
from src.service import EVENT_TYPES
from src.store import EventStore


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        store = EventStore(Path(self.tmp.name) / "log.jsonl", EVENT_TYPES)
        self.server = create_server("127.0.0.1", 0, store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow(self) -> None:
        s, b = self.request("POST", "/claims", {
            "kind": "document_quote", "statement": "司南之杓，其柢指南",
            "discipline": "历史文献学", "claim_id": "c1"})
        self.assertEqual(s, 200)

        s, b = self.request("POST", "/claims", {
            "kind": "artifact_dating", "statement": "旧断代",
            "discipline": "考古学", "claim_id": "c2"})
        self.assertEqual(s, 200)

        # 输入校验。
        s, b = self.request("POST", "/claims",
                            {"kind": "nope", "statement": "x", "discipline": "d"})
        self.assertEqual(s, 400)
        s, b = self.request("POST", "/claims",
                            {"kind": "document_quote", "discipline": "d"})
        self.assertEqual(s, 400)

        s, _ = self.request("POST", "/claims/c1/reviews",
                            {"discipline": "历史文献学", "decision": "approved",
                             "reviewer": "甲"})
        self.assertEqual(s, 200)
        s, b = self.request("POST", "/claims/c2/reviews",
                            {"discipline": "科技史", "decision": "approved",
                             "reviewer": "越界"})
        self.assertEqual(s, 422)
        s, _ = self.request("POST", "/claims/c2/reviews",
                            {"discipline": "考古学", "decision": "approved",
                             "reviewer": "乙"})
        self.assertEqual(s, 200)

        s, b = self.request("POST", "/exhibits",
                            {"label": "司南", "refs": ["c1", "c2"],
                             "exhibit_id": "e1"})
        self.assertEqual(s, 200)

        s, b = self.request("POST", "/exhibits/e1/release", {"released_by": "策展人"})
        self.assertEqual(s, 200)
        self.assertEqual(b["release_no"], 1)

        s, b = self.request("GET", "/exhibits/e1/evidence-graph?release_no=1")
        self.assertEqual(s, 200)
        self.assertEqual({n["id"] for n in b["nodes"]}, {"c1", "c2"})

        s, b = self.request("GET", "/exhibits/e1/dispute-summary")
        self.assertEqual(s, 200)
        self.assertTrue(b["can_publish"])

        # 新断代自动打回。
        s, _ = self.request("POST", "/claims", {
            "kind": "artifact_dating", "statement": "新断代",
            "discipline": "考古学", "supersedes": ["c2"], "claim_id": "c3"})
        self.assertEqual(s, 200)
        s, b = self.request("GET", "/exhibits/e1")
        self.assertEqual(s, 200)
        self.assertEqual(b["status"], "reopened")

        # 加注决议需要新主张复核。
        s, b = self.request("POST", "/exhibits/e1/resolve",
                            {"decision": "kept_with_annotation", "note": "并列"})
        self.assertEqual(s, 422)
        s, _ = self.request("POST", "/claims/c3/reviews",
                            {"discipline": "考古学", "decision": "approved",
                             "reviewer": "组"})
        self.assertEqual(s, 200)
        s, _ = self.request("POST", "/exhibits/e1/resolve",
                            {"decision": "kept_with_annotation", "note": "并列加注",
                             "by": "编委会"})
        self.assertEqual(s, 200)
        s, b = self.request("POST", "/exhibits/e1/release", {"released_by": "策展人"})
        self.assertEqual(s, 200)
        self.assertEqual(b["release_no"], 2)

        # 历史时点：2000 年时什么都不存在。
        s, b = self.request("GET", "/state?as_of=2000-01-01T00:00:00Z")
        self.assertEqual(s, 200)
        self.assertEqual(b["claims"], {})

    def test_conflict_returns_409_with_current_version(self) -> None:
        self.request("POST", "/claims", {
            "kind": "document_quote", "statement": "x",
            "discipline": "历史文献学", "claim_id": "c1"})
        self.request("POST", "/claims/c1/evidence",
                     {"kind": "书影", "citation": "甲本"})
        s, b = self.request("POST", "/claims/c1/evidence",
                            {"kind": "书影", "citation": "乙本",
                             "expected_version": 1})
        self.assertEqual(s, 409)
        self.assertEqual(b["current_version"], 2)

    def test_not_found(self) -> None:
        s, _ = self.request("GET", "/claims/ghost")
        self.assertEqual(s, 404)
        s, _ = self.request("GET", "/exhibits/ghost/evidence-graph")
        self.assertEqual(s, 404)
        s, b = self.request("POST", "/claims/ghost/withdraw", {"reason": "x"})
        self.assertEqual(s, 404)

    def test_bad_json(self) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/claims",
            data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_activity_readiness(self) -> None:
        self.request("POST", "/claims", {
            "kind": "scholar_opinion", "statement": "活动结论",
            "discipline": "科技史", "claim_id": "c1"})
        s, b = self.request("POST", "/activities",
                            {"title": "手工课", "refs": ["c1"],
                             "activity_id": "a1"})
        self.assertEqual(s, 200)
        s, b = self.request("GET", "/activities/a1/readiness")
        self.assertEqual(s, 200)
        self.assertFalse(b["ready"])


if __name__ == "__main__":
    unittest.main()
