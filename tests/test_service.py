"""主张库领域规则测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.service import (
    CLAIM_KINDS,
    EVIDENCE_TIERS,
    ClaimService,
    DomainError,
    NotFound,
)
from src.store import EventStore, VersionConflict

from src.service import EVENT_TYPES


class ServiceTestCase(unittest.TestCase):
    def new_service(self) -> ClaimService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = EventStore(Path(tmp.name) / "log.jsonl", EVENT_TYPES)
        return ClaimService(store)

    def setUp(self) -> None:
        self.svc = self.new_service()


class ClaimRegistrationTest(ServiceTestCase):

    def test_default_tiers_match_source_kind(self) -> None:
        c_doc = self.svc.register_claim("document_quote", "原文", "历史文献学")
        c_art = self.svc.register_claim("artifact_dating", "断代", "考古学")
        c_exp = self.svc.register_claim("experiment", "实验", "实验物理")
        c_op = self.svc.register_claim("scholar_opinion", "观点", "科技史")
        c_rec = self.svc.register_claim("reconstruction", "复原", "科技史")
        c_tr = self.svc.register_claim("translation", "今译", "历史文献学")
        tiers = {cid: self.svc.get_claim(cid)["tier"]
                 for cid in (c_doc, c_art, c_exp, c_op, c_rec, c_tr)}
        self.assertEqual(tiers[c_doc], "DOCUMENTARY")
        self.assertEqual(tiers[c_art], "ARTIFACT")
        self.assertEqual(tiers[c_exp], "EXPERIMENTAL")
        self.assertEqual(tiers[c_op], "DISPUTED")
        self.assertEqual(tiers[c_rec], "DISPUTED")
        self.assertEqual(tiers[c_tr], "DOCUMENTARY")

    def test_conflicting_claims_coexist(self) -> None:
        a = self.svc.register_claim("scholar_opinion", "磁性说", "科技史")
        b = self.svc.register_claim("scholar_opinion", "非磁性说", "科技史",
                                    tier="DISPUTED", conflicts_with=[a], note="争议")
        self.assertTrue(self.svc.get_claim(a))
        self.assertEqual(self.svc.get_claim(b)["conflicts_with"], [a])

    def test_explicit_disputed_requires_target_or_note(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.register_claim("scholar_opinion", "存疑", "科技史", tier="DISPUTED")

    def test_unknown_kind_tier_and_empty_fields_rejected(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.register_claim("exhibit_text", "展签不是来源主张", "策展")
        with self.assertRaises(DomainError):
            self.svc.register_claim("document_quote", "原文", "历史文献学", tier="X")
        with self.assertRaises(DomainError):
            self.svc.register_claim("document_quote", "  ", "历史文献学")
        with self.assertRaises(DomainError):
            self.svc.register_claim("document_quote", "原文", " ")

    def test_dependency_must_exist_and_be_alive(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.register_claim("scholar_opinion", "依赖幽灵", "科技史",
                                    depends_on=["ghost"])


class ReviewGateTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.claim = self.svc.register_claim(
            "artifact_dating", "汉墓磁性勺断代", "考古学")

    def test_review_discipline_must_match_claim(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.record_review(self.claim, "科技史", "approved", "越界者")

    def test_matching_discipline_review_records(self) -> None:
        self.svc.record_review(self.claim, "考古学", "approved", "考古复核人")
        state = self.svc.state()
        self.assertEqual(state["reviews"][f"review:{self.claim}:考古学"]["decision"],
                         "approved")

    def test_latest_review_wins_but_history_kept(self) -> None:
        self.svc.record_review(self.claim, "考古学", "changes_requested", "甲")
        self.svc.record_review(self.claim, "考古学", "approved", "甲")
        events = [e for e in self.svc.store.events()
                  if e["event_type"] == "REVIEW_RECORDED"]
        self.assertEqual(len(events), 2)
        self.assertEqual(self.svc.state()["reviews"][
            f"review:{self.claim}:考古学"]["decision"], "approved")

    def test_no_review_after_withdraw_but_record_remains(self) -> None:
        self.svc.record_review(self.claim, "考古学", "approved", "甲")
        self.svc.withdraw_claim(self.claim, "证据不足")
        with self.assertRaises(DomainError):
            self.svc.record_review(self.claim, "考古学", "revoked", "甲")
        self.assertIn(f"review:{self.claim}:考古学", self.svc.state()["reviews"])

    def test_reviewer_required(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.record_review(self.claim, "考古学", "approved", "  ")


class PublicationGateTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.c1 = self.svc.register_claim("document_quote", "文献原文", "历史文献学")
        self.c2 = self.svc.register_claim("artifact_dating", "实物断代", "考古学")
        self.ex = self.svc.create_exhibit_text("展签", [self.c1, self.c2])

    def approve(self, cid: str) -> None:
        self.svc.record_review(cid, self.svc.get_claim(cid)["discipline"],
                               "approved", "复核人")

    def test_release_blocked_until_all_closure_approved(self) -> None:
        self.approve(self.c1)
        with self.assertRaises(DomainError):
            self.svc.release_exhibit_text(self.ex)
        self.approve(self.c2)
        result = self.svc.release_exhibit_text(self.ex)
        self.assertEqual(result["release_no"], 1)

    def test_release_blocked_when_review_not_approved(self) -> None:
        self.approve(self.c1)
        self.svc.record_review(self.c2, "考古学", "rejected", "复核人")
        with self.assertRaises(DomainError) as ctx:
            self.svc.release_exhibit_text(self.ex)
        self.assertIn("rejected", str(ctx.exception))

    def test_transitive_dependency_needs_review(self) -> None:
        root = self.svc.register_claim(
            "scholar_opinion", "综合性结论", "科技史", depends_on=[self.c2])
        self.svc.revise_exhibit_text(self.ex, [root])
        self.approve(root)
        # c2 是 root 的传递依赖，未复核，门禁仍应失败。
        with self.assertRaises(DomainError):
            self.svc.release_exhibit_text(self.ex)
        self.approve(self.c2)
        self.assertEqual(self.svc.release_exhibit_text(self.ex)["release_no"], 1)

    def test_release_keeps_immutable_snapshot(self) -> None:
        self.approve(self.c1)
        self.approve(self.c2)
        self.svc.release_exhibit_text(self.ex)
        graph_v1 = self.svc.evidence_graph(self.ex, release_no=1)
        self.assertEqual({n["id"] for n in graph_v1["nodes"]}, {self.c1, self.c2})
        # 发布之后撤销主张，v1 快照不变。
        self.svc.withdraw_claim(self.c2, "后来被否定")
        graph_v1_again = self.svc.evidence_graph(self.ex, release_no=1)
        self.assertFalse(any(n["withdrawn"] for n in graph_v1_again["nodes"]))


class CorrectionImpactTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.doc = self.svc.register_claim("document_quote", "文献", "历史文献学")
        self.old = self.svc.register_claim("artifact_dating", "旧断代", "考古学")
        self.root = self.svc.register_claim(
            "scholar_opinion", "建立在旧断代上的结论", "科技史",
            depends_on=[self.old])
        for cid in (self.doc, self.old, self.root):
            self.svc.record_review(cid, self.svc.get_claim(cid)["discipline"],
                                   "approved", "复核人")
        self.affected = self.svc.create_exhibit_text("受影响展签", [self.doc, self.root])
        self.unrelated = self.svc.create_exhibit_text("无关展签", [self.doc])
        self.draft = self.svc.create_exhibit_text("草稿展签", [self.root])
        self.svc.release_exhibit_text(self.affected)
        self.svc.release_exhibit_text(self.unrelated)

    def test_correction_reopens_only_dependent_published_exhibits(self) -> None:
        new = self.svc.register_claim(
            "artifact_dating", "新断代", "考古学", supersedes=[self.old])
        self.assertEqual(self.svc.get_exhibit_text(self.affected)["status"], "reopened")
        self.assertEqual(
            self.svc.get_exhibit_text(self.unrelated)["status"], "published")
        # 草稿展签保持草稿，不产生"打回"事件。
        self.assertEqual(self.svc.get_exhibit_text(self.draft)["status"], "draft")
        history = self.svc.get_exhibit_text(self.affected)["reopen_history"][-1]
        self.assertEqual(history["affected_claims"], [self.old])
        self.assertEqual(history["triggered_by"], new)

    def test_affected_chain_traces_dependency_path(self) -> None:
        self.svc.register_claim(
            "artifact_dating", "新断代", "考古学", supersedes=[self.old])
        chain = (self.svc.get_exhibit_text(self.affected)
                 ["reopen_history"][-1]["affected_chains"][self.old])
        self.assertEqual(chain, [self.root, self.old])

    def test_release_blocked_while_reopened(self) -> None:
        self.svc.register_claim(
            "artifact_dating", "新断代", "考古学", supersedes=[self.old])
        with self.assertRaises(DomainError):
            self.svc.release_exhibit_text(self.affected)

    def test_annotation_resolution_requires_new_claim_approval(self) -> None:
        new = self.svc.register_claim(
            "artifact_dating", "新断代", "考古学", supersedes=[self.old])
        with self.assertRaises(DomainError):
            self.svc.resolve_reopened(self.affected, "kept_with_annotation",
                                      "并列加注")
        self.svc.record_review(new, "考古学", "approved", "新复核组")
        self.svc.resolve_reopened(self.affected, "kept_with_annotation",
                                  "新旧并列加注")
        # 加注后门禁放行并生成 v2 快照。
        result = self.svc.release_exhibit_text(self.affected)
        self.assertEqual(result["release_no"], 2)
        graph = self.svc.evidence_graph(self.affected)
        self.assertIsNotNone(
            next(n for n in graph["nodes"] if n["id"] == self.old)["annotation"])

    def test_revise_resolution_then_revise_and_release(self) -> None:
        new = self.svc.register_claim(
            "artifact_dating", "新断代", "考古学", supersedes=[self.old])
        self.svc.record_review(new, "考古学", "approved", "新复核组")
        self.svc.resolve_reopened(self.affected, "revise", "改用新断代")
        self.svc.revise_exhibit_text(self.affected, [self.doc, new])
        self.svc.release_exhibit_text(self.affected)
        graph = self.svc.evidence_graph(self.affected, release_no=2)
        node_ids = {n["id"] for n in graph["nodes"]}
        self.assertIn(new, node_ids)
        self.assertNotIn(self.old, node_ids)

    def test_reopened_again_clears_old_annotation(self) -> None:
        new = self.svc.register_claim(
            "artifact_dating", "新断代", "考古学", supersedes=[self.old])
        self.svc.record_review(new, "考古学", "approved", "组")
        self.svc.resolve_reopened(self.affected, "kept_with_annotation", "加注")
        self.svc.release_exhibit_text(self.affected)
        # 旧断代再次被新研究勘误：展签再次打回，旧加注失效。
        newer = self.svc.register_claim(
            "artifact_dating", "对1957器的第三轮检测结论", "考古学",
            supersedes=[self.old])
        ex = self.svc.get_exhibit_text(self.affected)
        self.assertEqual(ex["status"], "reopened")
        self.assertEqual(ex["annotated"], {})
        with self.assertRaises(DomainError):
            self.svc.release_exhibit_text(self.affected)
        self.assertEqual(ex["reopen_history"][-1]["triggered_by"], newer)


class WithdrawImpactTest(ServiceTestCase):
    def test_withdraw_reopens_dependent_exhibits_but_keeps_deliberation(self) -> None:
        svc = self.new_service()
        c = svc.register_claim("experiment", "实验结论", "实验物理")
        svc.record_review(c, "实验物理", "approved", "复核人")
        ex = svc.create_exhibit_text("实验展签", [c])
        svc.release_exhibit_text(ex)
        svc.withdraw_claim(c, "实验无法重复")
        self.assertEqual(svc.get_exhibit_text(ex)["status"], "reopened")
        history = svc.get_exhibit_text(ex)["reopen_history"][-1]
        self.assertEqual(history["withdrawn_claims"], [c])
        self.assertIn(f"review:{c}:实验物理", svc.state()["reviews"])
        # 旧版快照仍完整。
        self.assertIn(c, {n["id"] for n in
                          svc.evidence_graph(ex, release_no=1)["nodes"]})

    def test_shared_underlying_dependency_does_not_over_reopen(self) -> None:
        """仅共享更底层依赖、并不引用被勘误主张的展签不应被打回。"""
        svc = self.new_service()
        base = svc.register_claim("document_quote", "共享古籍原文", "历史文献学")
        old = svc.register_claim("artifact_dating", "旧断代", "考古学",
                                 depends_on=[base])
        sibling = svc.register_claim("scholar_opinion", "另一结论", "科技史",
                                     depends_on=[base])
        for cid in (base, old, sibling):
            svc.record_review(cid, svc.get_claim(cid)["discipline"],
                              "approved", "组")
        ex_old = svc.create_exhibit_text("旧断代展签", [old])
        ex_sib = svc.create_exhibit_text("兄弟结论展签", [sibling])
        svc.release_exhibit_text(ex_old)
        svc.release_exhibit_text(ex_sib)
        svc.register_claim("artifact_dating", "新断代", "考古学",
                           supersedes=[old])
        self.assertEqual(svc.get_exhibit_text(ex_old)["status"], "reopened")
        self.assertEqual(svc.get_exhibit_text(ex_sib)["status"], "published")


class HistoryReplayTest(ServiceTestCase):
    def test_state_at_any_point_in_time(self) -> None:
        svc = self.new_service()
        svc.register_claim("document_quote", "早", "历史文献学",
                           claim_id="c1", occurred_at="2026-01-01T09:00:00+08:00")
        svc.register_claim("document_quote", "晚", "历史文献学",
                           claim_id="c2", occurred_at="2026-06-01T09:00:00+08:00")
        early = svc.state(as_of="2026-02-01T00:00:00+08:00")
        self.assertIn("c1", early["claims"])
        self.assertNotIn("c2", early["claims"])
        self.assertIn("c2", svc.state(as_of=None)["claims"])

    def test_reopen_after_withdraw_exhibit(self) -> None:
        svc = self.new_service()
        c = svc.register_claim("document_quote", "原文", "历史文献学")
        svc.record_review(c, "历史文献学", "approved", "组")
        ex = svc.create_exhibit_text("展签", [c])
        svc.release_exhibit_text(ex)
        svc.withdraw_exhibit_text(ex, "专题结束")
        self.assertEqual(svc.get_exhibit_text(ex)["status"], "withdrawn")
        with self.assertRaises(DomainError):
            svc.release_exhibit_text(ex)
        # 历史版本仍可查。
        self.assertEqual(svc.evidence_graph(ex, release_no=1)["release_no"], 1)


class ActivityTest(ServiceTestCase):
    def test_readiness_shares_gate(self) -> None:
        svc = self.new_service()
        c = svc.register_claim("scholar_opinion", "活动要用的结论", "科技史")
        act = svc.register_activity("司南手工课", [c])
        self.assertFalse(svc.activity_readiness(act)["ready"])
        svc.record_review(c, "科技史", "approved", "组")
        self.assertTrue(svc.activity_readiness(act)["ready"])

    def test_not_found(self) -> None:
        svc = self.new_service()
        with self.assertRaises(NotFound):
            svc.get_claim("nope")
        with self.assertRaises(NotFound):
            svc.evidence_graph("nope")


class OptimisticLockTest(ServiceTestCase):
    def test_stale_expected_version_on_register_conflicts(self) -> None:
        svc = self.new_service()
        svc.register_claim("document_quote", "原文", "历史文献学",
                           claim_id="c1")
        with self.assertRaises(VersionConflict):
            svc.register_claim("document_quote", "重复占用同一 ID",
                               "历史文献学", claim_id="c1")


if __name__ == "__main__":
    unittest.main()
