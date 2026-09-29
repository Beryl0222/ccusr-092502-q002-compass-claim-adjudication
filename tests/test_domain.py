"""领域与应用服务测试：围绕司南争议的典型策展流程。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src import domain as d
from src.errors import Conflict, GateRejected, NotFound, ValidationFailure
from src.service import ClaimService
from src.storage import EventStore

CST = timezone(timedelta(hours=8))


class Clock:
    """每调用一次前进 1 毫秒，保证事件时间严格有序、可重现。"""

    def __init__(self) -> None:
        self.t = datetime(2026, 9, 1, 9, 0, 0, tzinfo=CST)

    def __call__(self) -> str:
        self.t += timedelta(milliseconds=1)
        return self.t.isoformat()


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.store = EventStore(os.path.join(self.dir, "events.jsonl"))
        self.clock = Clock()
        self.svc = ClaimService(self.store, clock=self.clock)

    # ---- 便捷构造 ------------------------------------------------------

    def approved_claim(self, claim_id: str, *, kind: str, tier: str,
                       statement: str, discipline: str, **kw) -> dict:
        self.svc.register_claim(
            claim_id=claim_id, kind=kind, tier=tier, statement=statement,
            source_ref=kw.get("source_ref", ""), registered_by=kw.get("registered_by", "编辑"),
        )
        if kw.get("evidence"):
            for ev in kw["evidence"]:
                self.svc.link_evidence(claim_id, **ev)
        self.svc.record_review(claim_id, discipline=discipline, outcome="APPROVED",
                               reviewer=kw.get("reviewer", f"{discipline}-专家"))
        return self.svc.get_claim(claim_id)

    def released_label(self, label_id: str, claims: list[str], *,
                       dispute_note: str = "", title: str = "司南展签") -> dict:
        self.svc.draft_label(label_id=label_id, title=title, author="策展人")
        for cid in claims:
            self.svc.cite_claim(label_id, claim_id=cid)
        self.svc.record_review(label_id, discipline="museology", outcome="APPROVED",
                               reviewer="展陈专家")
        return self.svc.release_label(label_id, dispute_note=dispute_note)


class RegistrationTest(ServiceCase):
    def test_claim_requires_known_kind_and_tier(self) -> None:
        with self.assertRaises(ValidationFailure) as ctx:
            self.svc.register_claim(kind="weblog", statement="x", tier="HISTORICAL_RECORD")
        self.assertIn("未知主张类型", ctx.exception.errors[0])
        with self.assertRaises(ValidationFailure):
            self.svc.register_claim(kind="scholar_opinion", statement="x", tier="GUESS")

    def test_eight_claim_kinds_registered_in_contract(self) -> None:
        import json
        contract_path = os.path.join(os.path.dirname(__file__), "..", "contracts", "domain.json")
        with open(contract_path, encoding="utf-8") as fh:
            contract = json.load(fh)
        self.assertEqual(set(contract["claim_kinds"]), {
            "literature_excerpt", "artifact_dating", "reconstruction_plan",
            "experiment_record", "scholar_opinion", "translation",
            "exhibit_label", "education_activity",
        })

    def test_concurrent_creation_conflict(self) -> None:
        self.svc.register_claim(claim_id="claim-x", kind="scholar_opinion",
                                statement="甲的说法", tier="DISPUTED")
        with self.assertRaises(Conflict) as ctx:
            self.svc.register_claim(claim_id="claim-x", kind="scholar_opinion",
                                    statement="乙并发的说法", tier="DISPUTED")
        self.assertEqual((ctx.exception.expected, ctx.exception.actual), (0, 1))

    def test_stale_expected_version_conflict(self) -> None:
        self.svc.register_claim(claim_id="claim-a", kind="experiment_record",
                                statement="初次实验", tier="EXPERIMENTAL_SUPPORT")
        self.svc.link_evidence("claim-a", tier="EXPERIMENTAL_SUPPORT", summary="第一次附证据")
        with self.assertRaises(Conflict):
            # 手里拿的是版本 1，但聚合已到版本 2
            self.svc.link_evidence("claim-a", tier="EXPERIMENTAL_SUPPORT",
                                   summary="并发的第二次附证据", expected_version=1)


class ReleaseGateTest(ServiceCase):
    def test_editor_cannot_release_without_specialty_review(self) -> None:
        self.svc.register_claim(claim_id="c1", kind="artifact_dating",
                                statement="汉墓出土勺形器断为东汉",
                                tier="PHYSICAL_EVIDENCE", source_ref="M12:3")
        self.svc.draft_label(label_id="l1", title="司南", author="编辑")
        self.svc.cite_claim("l1", claim_id="c1")
        self.svc.record_review("l1", discipline="museology", outcome="APPROVED")
        with self.assertRaises(GateRejected) as ctx:
            self.svc.release_label("l1")
        self.assertTrue(any("archaeology" in e for e in ctx.exception.errors))

    def test_wrong_discipline_does_not_count(self) -> None:
        # 物理学专家的批准不能代替考古专业对断代结论负责
        self.approved_claim("c1", kind="artifact_dating", tier="PHYSICAL_EVIDENCE",
                            statement="断为东汉", discipline="physics")
        self.svc.draft_label(label_id="l1", title="司南")
        self.svc.cite_claim("l1", claim_id="c1")
        self.svc.record_review("l1", discipline="museology", outcome="APPROVED")
        with self.assertRaises(GateRejected) as ctx:
            self.svc.release_label("l1")
        self.assertTrue(any("archaeology" in e for e in ctx.exception.errors))

    def test_no_citation_no_release(self) -> None:
        self.svc.draft_label(label_id="l1", title="空展签")
        self.svc.record_review("l1", discipline="museology", outcome="APPROVED")
        with self.assertRaises(GateRejected) as ctx:
            self.svc.release_label("l1")
        self.assertIn("至少需要引用一条主张", ctx.exception.errors[0])


class DisputeTest(ServiceCase):
    def _build_conflicting_pair(self) -> None:
        # 传统说：《韩非子》《论衡》文献 -> 司南为磁性指向器
        self.approved_claim(
            "lit", kind="literature_excerpt", tier="HISTORICAL_RECORD",
            statement="《论衡·是应》“司南之杓”之司南为磁性指向器",
            discipline="philology", source_ref="论衡·是应",
            evidence=[{"tier": "HISTORICAL_RECORD", "kind": "古籍原文",
                       "citation": "论衡·是应", "summary": "司南之杓，投之于地，其柢指南"}],
        )
        # 争议说：学者认为文献语境不足以证明磁性、实物亦无磁勺出土
        self.approved_claim(
            "opp", kind="scholar_opinion", tier="DISPUTED",
            statement="“司南”在汉代语境可能指指南车或抽象权柄，磁勺说缺乏实物支撑",
            discipline="history_of_technology",
            evidence=[{"tier": "PHYSICAL_EVIDENCE", "kind": "考古核查",
                       "summary": "迄今未见汉代天然磁石勺形指向器出土实例"}],
        )
        self.svc.link_relation("opp", relation="contradicts", to_claim_id="lit",
                               note="磁性说与非磁性说并存")

    def test_conflicts_coexist_and_report_both_sides(self) -> None:
        self._build_conflicting_pair()
        claims = {c["claim_id"]: c for c in self.svc.list_claims()}
        self.assertEqual(claims["lit"]["status"], "ACTIVE")
        self.assertEqual(claims["opp"]["status"], "ACTIVE")
        report = self.svc.label_evidence_report  # 先准备引用再看报告
        self.svc.draft_label(label_id="l1", title="司南是什么")
        self.svc.cite_claim("l1", claim_id="lit")
        self.svc.cite_claim("l1", claim_id="opp")
        draft_report = self.svc.label_evidence_report("l1")
        contradiction = next(d for d in draft_report["disputes"] if d["type"] == "contradiction")
        sides = {s["claim_id"] for s in contradiction["sides"]}
        self.assertEqual(sides, {"lit", "opp"})
        evidence_nodes = [n for n in draft_report["evidence_graph"]["nodes"]
                          if n["node_type"] == "evidence"]
        self.assertTrue(evidence_nodes)
        # 证据材料不与文献原文、学者观点混作同一级事实
        self.assertIn("PHYSICAL_EVIDENCE", {n["evidence_tier"] for n in evidence_nodes})

    def test_disputed_release_requires_concurrent_presentation_note(self) -> None:
        self._build_conflicting_pair()
        self.svc.draft_label(label_id="l1", title="司南是什么")
        self.svc.cite_claim("l1", claim_id="lit")
        self.svc.cite_claim("l1", claim_id="opp")
        self.svc.record_review("l1", discipline="museology", outcome="APPROVED")
        with self.assertRaises(GateRejected) as ctx:
            self.svc.release_label("l1")
        self.assertTrue(any("争议并呈" in e for e in ctx.exception.errors))
        # 写明并呈说明后可以发布——冲突主张并存，编辑不替学界定论
        released = self.svc.release_label(
            "l1", dispute_note="磁性说与非磁性说学术界尚无定论，本展两说并陈")
        self.assertEqual(released["status"], "PUBLISHED")
        snapshot = self.svc.get_label_release("l1", 1)
        self.assertEqual(len(snapshot["disputes"]), 2)  # 一条 tier_disputed + 一条 contradiction


class ImpactTest(ServiceCase):
    def _build_chain(self) -> None:
        # A: 断代主张；B: 复原方案 derived_from A；C: 与 A 无关的文献摘引
        self.approved_claim("A", kind="artifact_dating", tier="PHYSICAL_EVIDENCE",
                            statement="勺形器与地盘同出，断为东汉",
                            discipline="archaeology", source_ref="M12:3")
        self.approved_claim("B", kind="reconstruction_plan", tier="EXPERIMENTAL_SUPPORT",
                            statement="按 1:1 复原天然磁石勺并置于地盘",
                            discipline="history_of_technology")
        self.svc.link_relation("B", relation="derived_from", to_claim_id="A")
        self.approved_claim("C", kind="literature_excerpt", tier="HISTORICAL_RECORD",
                            statement="《韩非子·有度》“司南”条目摘引",
                            discipline="philology")
        self.released_label("L1", ["B"], title="磁勺复原展签")
        self.released_label("L2", ["C"], title="文献墙展签")

    def test_dependency_closure_follows_derivation_not_support(self) -> None:
        self._build_chain()
        # 另一条实验只是"支持" A，不构成依赖
        self.approved_claim("D", kind="experiment_record", tier="EXPERIMENTAL_SUPPORT",
                            statement="摩擦系数测量", discipline="physics")
        self.svc.link_relation("D", relation="supports", to_claim_id="A")
        lib = d.library_at(self.store.load())
        self.assertEqual(d.dependency_closure(lib, "A"), {"A", "B"})

    def test_supersede_reopens_only_truly_dependent_labels(self) -> None:
        self._build_chain()
        preview = self.svc.impact_preview("A")
        self.assertEqual(preview["dependency_closure"], ["A", "B"])
        reopened = {x["label_id"] for x in preview["reopened_labels"]}
        unaffected = {x["label_id"] for x in preview["unaffected_published_labels"]}
        self.assertEqual(reopened, {"L1"})
        self.assertEqual(unaffected, {"L2"})

        result = self.svc.supersede_claim(
            "A", reason="热释光复测改断为西晋；原东汉结论撤回", by="考古组",
            statement="勺形器与地盘同出，热释光复测改断为西晋")
        new_a_id = result["new_claim"]["claim_id"]
        self.assertEqual({x["label_id"] for x in result["reopened_labels"]}, {"L1"})

        old = self.svc.get_claim("A")
        self.assertEqual(old["status"], "SUPERSEDED")
        self.assertTrue(old["superseded_by"])
        # 旧主张及其证据、审议过程仍在
        self.assertTrue(old["evidence"] or old["reviews"])
        self.assertEqual(len(self.svc.get_label_release("L1", 1)["citations"]), 1)

        l2 = next(l for l in self.svc.list_labels() if l["label_id"] == "L2")
        self.assertEqual(l2["status"], "PUBLISHED")
        l1 = next(l for l in self.svc.list_labels() if l["label_id"] == "L1")
        self.assertEqual(l1["status"], "REOPENED")
        self.assertEqual(l1["reopen_history"][0]["trigger_claim_id"], "A")

    def test_reopened_label_needs_fresh_museology_review(self) -> None:
        self._build_chain()
        result = self.svc.supersede_claim(
            "A", reason="改断西晋", by="考古组", statement="热释光复测改断为西晋")
        new_a = result["new_claim"]["claim_id"]
        # 新断代主张必须重新过考古专业（旧批准挂在旧主张上，不随勘误转移）
        self.svc.record_review(new_a, discipline="archaeology", outcome="APPROVED",
                               reviewer="考古专家（复核）")
        # 更新引用：撤回依赖旧断代的 B，改引新 A
        self.svc.withdraw_citation("L1", "B", reason="复原方案依赖旧断代，一并撤回")
        self.svc.cite_claim("L1", claim_id=new_a)
        with self.assertRaises(GateRejected) as ctx:
            self.svc.release_label("L1", dispute_note="断代更新后两说仍并存")
        self.assertTrue(any("打回" in e for e in ctx.exception.errors))

        self.svc.record_review("L1", discipline="museology", outcome="APPROVED",
                               reviewer="展陈专家（复审）")
        v2 = self.svc.release_label("L1", dispute_note="断代据热释光结果更新")
        self.assertEqual(v2["current_release_version"], 2)
        # 第 1 版快照原样保留，可供追溯
        v1 = self.svc.get_label_release("L1", 1)
        self.assertEqual(v1["citations"][0]["claim_id"], "B")
        self.assertEqual(v1["evidence_graph"]["nodes"][0]["tier"], "EXPERIMENTAL_SUPPORT")

    def test_superseded_claim_cannot_anchor_new_release(self) -> None:
        self._build_chain()
        self.svc.supersede_claim("A", reason="改断西晋", by="考古组")
        self.svc.draft_label(label_id="L3", title="新展签")
        self.svc.cite_claim("L3", claim_id="A")
        self.svc.record_review("L3", discipline="museology", outcome="APPROVED")
        with self.assertRaises(GateRejected) as ctx:
            self.svc.release_label("L3")
        self.assertTrue(any("已被勘误" in e for e in ctx.exception.errors))


class AuditAndRetentionTest(ServiceCase):
    def test_revoke_review_keeps_deliberation_record(self) -> None:
        self.approved_claim("c1", kind="experiment_record", tier="EXPERIMENTAL_SUPPORT",
                            statement="磁石勺可稳定指南", discipline="physics",
                            reviewer="物理专家甲")
        review_id = self.svc.get_claim("c1")["reviews"][0]["review_id"]
        self.svc.revoke_review("c1", review_id, reason="实验记录发现磁化强度数据造假",
                               revoked_by="学术委员会")
        view = self.svc.get_claim("c1")
        self.assertEqual(len(view["reviews"]), 1)  # 审议过程没有删除
        self.assertFalse(view["reviews"][0]["active"])
        self.assertIn("造假", view["reviews"][0]["revoke_reason"])

        self.svc.draft_label(label_id="l1", title="实验展签")
        self.svc.cite_claim("l1", claim_id="c1")
        self.svc.record_review("l1", discipline="museology", outcome="APPROVED")
        with self.assertRaises(GateRejected) as ctx:
            self.svc.release_label("l1")
        self.assertTrue(any("physics" in e for e in ctx.exception.errors))

    def test_archive_keeps_every_release(self) -> None:
        self.approved_claim("c1", kind="literature_excerpt", tier="HISTORICAL_RECORD",
                            statement="司南条目", discipline="philology")
        self.released_label("l1", ["c1"])
        self.svc.archive_label("l1", reason="巡展撤展", by="馆长")
        snap = self.svc.get_label_release("l1", 1)
        self.assertEqual(snap["title"], "司南展签")
        with self.assertRaises(ValidationFailure):
            self.svc.revise_label("l1", body="试图改写已撤展签")
        events = self.store.load()
        self.assertTrue(any(e["event_type"] == "TEXT_ARCHIVED" for e in events))

    def test_withdrawn_citation_stays_in_history(self) -> None:
        self.approved_claim("c1", kind="translation", tier="HISTORICAL_RECORD",
                            statement="旧译：杓即磁勺", discipline="philology")
        self.svc.draft_label(label_id="l1", title="译文展签")
        self.svc.cite_claim("l1", claim_id="c1")
        self.svc.withdraw_citation("l1", "c1", reason="译文修订")
        view = self.svc._label_view("l1")
        self.assertEqual(len(view["citations"]), 1)
        self.assertFalse(view["citations"][0]["active"])
        self.assertEqual(view["citations"][0]["withdrawn_reason"], "译文修订")
        self.assertEqual(view["active_citation_ids"], [])


class TimeTravelTest(ServiceCase):
    def test_state_replay_at_any_historical_moment(self) -> None:
        self.approved_claim("c1", kind="literature_excerpt", tier="HISTORICAL_RECORD",
                            statement="司南古文", discipline="philology")
        t_before_label = self.clock()
        self.released_label("l1", ["c1"])
        t_after = self.clock()

        past = self.svc.state_at(t_before_label)
        self.assertIn("c1", past["claims"])
        self.assertNotIn("l1", past["labels"])

        later = self.svc.state_at(t_after)
        self.assertEqual(later["labels"]["l1"]["release_versions"], [1])

        # 再勘误：历史时点的第 1 版状态不受影响
        self.svc.supersede_claim("c1", reason="异文校勘改字", by="文献组")
        past2 = self.svc.state_at(t_after)
        self.assertEqual(past2["claims"]["c1"]["status"], "ACTIVE")
        now = self.svc.get_claim("c1")
        self.assertEqual(now["status"], "SUPERSEDED")

    def test_event_log_is_append_only(self) -> None:
        self.approved_claim("c1", kind="scholar_opinion", tier="DISPUTED",
                            statement="一说", discipline="history_of_technology")
        review_id = self.svc.get_claim("c1")["reviews"][0]["review_id"]
        self.svc.revoke_review("c1", review_id, reason="撤回")
        events = self.store.load()
        versions = [(e["aggregate_id"], e["version"]) for e in events]
        self.assertEqual(versions, [("c1", 1), ("c1", 2), ("c1", 3)])
        self.assertEqual([e["seq"] for e in events], [0, 1, 2])


class EvidenceReportTest(ServiceCase):
    def test_graph_contains_claims_evidence_and_edges(self) -> None:
        self.approved_claim("a", kind="artifact_dating", tier="PHYSICAL_EVIDENCE",
                            statement="断代", discipline="archaeology",
                            evidence=[{"tier": "PHYSICAL_EVIDENCE", "kind": "发掘报告",
                                       "citation": "M12", "summary": "出土单位记录"}])
        self.approved_claim("b", kind="experiment_record", tier="EXPERIMENTAL_SUPPORT",
                            statement="复原实验", discipline="physics")
        self.svc.link_relation("b", relation="supports", to_claim_id="a")
        self.svc.draft_label(label_id="l1", title="图展签")
        self.svc.cite_claim("l1", claim_id="a")
        report = self.svc.label_evidence_report("l1")
        node_ids = {n["id"] for n in report["evidence_graph"]["nodes"]}
        self.assertIn("a", node_ids)
        self.assertIn("b", node_ids)  # 关系邻居带入
        self.assertTrue(any(nid.startswith("evidence:") for nid in node_ids))
        edge_types = {(e["source"], e["type"], e["target"]) for e in report["evidence_graph"]["edges"]}
        has_evidence_edges = {t for t in edge_types if t[1] == "has_evidence"}
        self.assertTrue(has_evidence_edges)
        self.assertEqual({t[0] for t in has_evidence_edges}, {"a"})
        self.assertTrue(all(t[2].startswith("evidence:") for t in has_evidence_edges))
        self.assertIn(("b", "supports", "a"), edge_types)

    def test_missing_release_returns_not_found(self) -> None:
        self.svc.draft_label(label_id="l1", title="x")
        with self.assertRaises(NotFound):
            self.svc.get_label_release("l1", 9)


if __name__ == "__main__":
    unittest.main()
