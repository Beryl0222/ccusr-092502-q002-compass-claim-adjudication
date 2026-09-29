"""应用服务：把用例翻译成追加事件，所有写操作走乐观并发控制。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from . import domain as d
from .errors import GateRejected, NotFound, ValidationFailure
from .storage import EventStore


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ClaimService:
    def __init__(self, store: EventStore, clock: Any = None) -> None:
        self.store = store
        self.clock = clock or now_iso

    # ---- 内部工具 ------------------------------------------------------

    def _library(self) -> d.Library:
        return d.library_at(self.store.load())

    def _get_claim(self, lib: d.Library, claim_id: str) -> d.Claim:
        claim = lib.claims.get(claim_id)
        if claim is None:
            raise NotFound(f"主张不存在: {claim_id}")
        return claim

    def _get_label(self, lib: d.Library, label_id: str) -> d.Label:
        label = lib.labels.get(label_id)
        if label is None:
            raise NotFound(f"展签不存在: {label_id}")
        return label

    def _event(self, event_type: str, aggregate_id: str, version: int, payload: dict) -> dict:
        return {
            "event_id": _new_id("evt"),
            "event_type": event_type,
            "occurred_at": self.clock(),
            "aggregate_id": aggregate_id,
            "version": version,
            "payload": payload,
        }

    # ---- 主张 ----------------------------------------------------------

    def register_claim(self, *, kind: str, statement: str, tier: str,
                       source_ref: str = "", registered_by: str = "",
                       expected_version: int = 0, claim_id: str | None = None) -> dict:
        errors: list[str] = []
        if kind not in d.CLAIM_KINDS:
            errors.append(f"未知主张类型: {kind}")
        if tier not in d.EVIDENCE_TIERS:
            errors.append(f"未知证据层级: {tier}")
        if not statement or not statement.strip():
            errors.append("statement 不能为空")
        if errors:
            raise ValidationFailure("主张登记校验失败", errors)

        claim_id = claim_id or _new_id("claim")
        event = self._event("CLAIM_REGISTERED", claim_id, 1, {
            "kind": kind,
            "statement": statement,
            "tier": tier,
            "source_ref": source_ref,
            "registered_by": registered_by,
        })
        self.store.append(event, expected_version=expected_version)
        return self._claim_view(claim_id)

    def supersede_claim(self, claim_id: str, *, reason: str, by: str = "",
                        statement: str | None = None, tier: str | None = None,
                        source_ref: str | None = None,
                        expected_version: int | None = None) -> dict:
        """勘误/重断代：登记新的修正主张并把旧主张标记为被取代。

        新主张是独立的新主张，必须重新取得对应专业复核；
        旧主张与当时证据不删除；只把真正依赖旧主张的已发布展签送回复审。
        """
        lib = self._library()
        old = self._get_claim(lib, claim_id)
        if old.status == "SUPERSEDED":
            raise ValidationFailure("主张已被取代，不能再次勘误", [f"{claim_id} 已处于 SUPERSEDED"])
        if tier is not None and tier not in d.EVIDENCE_TIERS:
            raise ValidationFailure("证据层级非法", [f"未知层级: {tier}"])
        base = expected_version if expected_version is not None else old.version

        new_id = _new_id("claim")
        new_event = self._event("CLAIM_REGISTERED", new_id, 1, {
            "kind": old.kind,
            "statement": statement if statement is not None else old.statement,
            "tier": tier if tier is not None else old.tier,
            "source_ref": source_ref if source_ref is not None else old.source_ref,
            "registered_by": by,
            "supersedes": claim_id,
        })
        mark_event = self._event("CLAIM_SUPERSEDED", claim_id, base + 1, {
            "new_claim_id": new_id,
            "reason": reason,
            "by": by,
        })

        # 影响面：只有已发布、且有效引用落入依赖闭包的展签才复审
        impacted = d.affected_labels(lib, claim_id)
        reopen_events: list[dict] = []
        expected_versions: dict[str, int] = {
            new_id: 0,
            claim_id: base,
        }
        for label in impacted:
            release_version = label.current_version or 0
            reopen_events.append(self._event("IMPACT_REOPENED", label.label_id, label.version + 1, {
                "trigger_claim_id": claim_id,
                "new_claim_id": new_id,
                "reason": reason,
                "affected_release_version": release_version,
            }))
            expected_versions[label.label_id] = label.version

        self.store.append_batch([new_event, mark_event, *reopen_events], expected_versions)
        return {
            "old_claim": self._claim_view(claim_id),
            "new_claim": self._claim_view(new_id),
            "reopened_labels": [
                {"label_id": l.label_id, "affected_release_version": l.current_version}
                for l in impacted
            ],
        }

    def link_relation(self, claim_id: str, *, relation: str, to_claim_id: str,
                      note: str = "", expected_version: int | None = None) -> dict:
        lib = self._library()
        claim = self._get_claim(lib, claim_id)
        if relation not in d.RELATIONS:
            raise ValidationFailure("关系类型非法", [f"未知关系: {relation}"])
        self._get_claim(lib, to_claim_id)
        base = expected_version if expected_version is not None else claim.version
        event = self._event("CLAIM_RELATION_LINKED", claim_id, base + 1, {
            "relation": relation,
            "to_claim_id": to_claim_id,
            "note": note,
        })
        self.store.append(event, expected_version=base)
        return self._claim_view(claim_id)

    def link_evidence(self, claim_id: str, *, tier: str, kind: str = "",
                      citation: str = "", summary: str = "",
                      linked_by: str = "", evidence_id: str | None = None,
                      expected_version: int | None = None) -> dict:
        lib = self._library()
        claim = self._get_claim(lib, claim_id)
        if tier not in d.EVIDENCE_TIERS:
            raise ValidationFailure("证据层级非法", [f"未知层级: {tier}"])
        base = expected_version if expected_version is not None else claim.version
        event = self._event("EVIDENCE_LINKED", claim_id, base + 1, {
            "evidence_id": evidence_id or _new_id("ev"),
            "kind": kind,
            "citation": citation,
            "tier": tier,
            "summary": summary,
            "linked_by": linked_by,
        })
        self.store.append(event, expected_version=base)
        return self._claim_view(claim_id)

    # ---- 复核与撤销 ----------------------------------------------------

    def record_review(self, subject_id: str, *, discipline: str, outcome: str,
                      reviewer: str = "", note: str = "",
                      expected_version: int | None = None) -> dict:
        lib = self._library()
        subject = lib.claims.get(subject_id) or lib.labels.get(subject_id)
        if subject is None:
            raise NotFound(f"复核对象不存在: {subject_id}")
        if discipline not in d.DISCIPLINES:
            raise ValidationFailure("专业领域非法", [f"未知领域: {discipline}"])
        if outcome not in {"APPROVED", "CHANGES_REQUESTED", "REJECTED"}:
            raise ValidationFailure("复核结论非法", [f"未知结论: {outcome}"])
        base = expected_version if expected_version is not None else subject.version
        event = self._event("REVIEW_RECORDED", subject_id, base + 1, {
            "review_id": _new_id("rev"),
            "discipline": discipline,
            "reviewer": reviewer,
            "outcome": outcome,
            "note": note,
        })
        self.store.append(event, expected_version=base)
        return self._subject_view(subject_id)

    def revoke_review(self, subject_id: str, review_id: str, *, reason: str,
                      revoked_by: str = "", expected_version: int | None = None) -> dict:
        """撤销复核：只标记失效，审议过程保留。"""
        lib = self._library()
        subject = lib.claims.get(subject_id) or lib.labels.get(subject_id)
        if subject is None:
            raise NotFound(f"复核对象不存在: {subject_id}")
        target = next((r for r in subject.reviews if r.review_id == review_id), None)
        if target is None:
            raise NotFound(f"复核记录不存在: {review_id}")
        base = expected_version if expected_version is not None else subject.version
        event = self._event("REVIEW_REVOKED", subject_id, base + 1, {
            "review_id": review_id,
            "reason": reason,
            "revoked_by": revoked_by,
        })
        self.store.append(event, expected_version=base)
        return self._subject_view(subject_id)

    # ---- 展签 ----------------------------------------------------------

    def draft_label(self, *, title: str, body: str = "", author: str = "",
                    label_id: str | None = None, expected_version: int = 0) -> dict:
        if not title or not title.strip():
            raise ValidationFailure("展签标题不能为空")
        label_id = label_id or _new_id("label")
        event = self._event("TEXT_DRAFTED", label_id, 1, {
            "title": title,
            "body": body,
            "author": author,
        })
        self.store.append(event, expected_version=expected_version)
        return self._label_view(label_id)

    def cite_claim(self, label_id: str, *, claim_id: str, how_used: str = "",
                   expected_version: int | None = None) -> dict:
        lib = self._library()
        label = self._get_label(lib, label_id)
        self._get_claim(lib, claim_id)
        base = expected_version if expected_version is not None else label.version
        event = self._event("TEXT_CLAIM_CITED", label_id, base + 1, {
            "claim_id": claim_id,
            "how_used": how_used,
        })
        self.store.append(event, expected_version=base)
        return self._label_view(label_id)

    def withdraw_citation(self, label_id: str, claim_id: str, *, reason: str,
                          expected_version: int | None = None) -> dict:
        lib = self._library()
        label = self._get_label(lib, label_id)
        base = expected_version if expected_version is not None else label.version
        event = self._event("TEXT_CITATION_WITHDRAWN", label_id, base + 1, {
            "claim_id": claim_id,
            "reason": reason,
        })
        self.store.append(event, expected_version=base)
        return self._label_view(label_id)

    def revise_label(self, label_id: str, *, title: str | None = None, body: str | None = None,
                     note: str = "", by: str = "", expected_version: int | None = None) -> dict:
        lib = self._library()
        label = self._get_label(lib, label_id)
        if label.status == "ARCHIVED":
            raise ValidationFailure("已撤销归档的展签不能修改", [label_id])
        base = expected_version if expected_version is not None else label.version
        event = self._event("TEXT_REVISED", label_id, base + 1, {
            "title": title, "body": body, "note": note, "by": by,
        })
        self.store.append(event, expected_version=base)
        return self._label_view(label_id)

    def release_label(self, label_id: str, *, dispute_note: str = "",
                      expected_version: int | None = None) -> dict:
        """发布展签：门禁通过才冻结快照；编辑不能越过专业复核对外定论。"""
        lib = self._library()
        label = self._get_label(lib, label_id)
        base = expected_version if expected_version is not None else label.version
        errors, disputes = d.release_gate(lib, label, dispute_note)
        if errors:
            raise GateRejected(errors, disputes)

        release_version = (label.current_version or 0) + 1
        released_at = self.clock()
        snapshot = d.build_release_snapshot(
            lib, label, release_version, released_at, disputes, dispute_note
        )
        event = self._event("TEXT_RELEASED", label_id, base + 1, {"snapshot": snapshot})
        self.store.append(event, expected_version=base)
        return self._label_view(label_id)

    def archive_label(self, label_id: str, *, reason: str, by: str = "",
                      expected_version: int | None = None) -> dict:
        """撤销展签：改状态并留档，所有发布版本与审议过程保留。"""
        lib = self._library()
        label = self._get_label(lib, label_id)
        if label.status == "ARCHIVED":
            raise ValidationFailure("展签已归档", [label_id])
        base = expected_version if expected_version is not None else label.version
        event = self._event("TEXT_ARCHIVED", label_id, base + 1, {
            "reason": reason, "by": by,
        })
        self.store.append(event, expected_version=base)
        return self._label_view(label_id)

    # ---- 查询 ----------------------------------------------------------

    def _claim_view(self, claim_id: str) -> dict:
        lib = self._library()
        return d.claim_to_dict(self._get_claim(lib, claim_id))

    def _subject_view(self, subject_id: str) -> dict:
        lib = self._library()
        if subject_id in lib.claims:
            return d.claim_to_dict(lib.claims[subject_id])
        return d.label_to_dict(self._get_label(lib, subject_id))

    def _label_view(self, label_id: str) -> dict:
        lib = self._library()
        return d.label_to_dict(self._get_label(lib, label_id))

    def list_claims(self) -> list[dict]:
        lib = self._library()
        return [d.claim_to_dict(c) for c in lib.claims.values()]

    def get_claim(self, claim_id: str) -> dict:
        lib = self._library()
        return d.claim_to_dict(self._get_claim(lib, claim_id))

    def list_labels(self) -> list[dict]:
        lib = self._library()
        return [d.label_to_dict(l) for l in lib.labels.values()]

    def get_label_release(self, label_id: str, release_version: int) -> dict:
        """读取某一版已发布展签：返回冻结快照，不受后续勘误影响。"""
        lib = self._library()
        label = self._get_label(lib, label_id)
        for release in label.releases:
            if release["release_version"] == release_version:
                return release
        raise NotFound(f"展签 {label_id} 没有第 {release_version} 版发布")

    def label_evidence_report(self, label_id: str, release_version: int | None = None) -> dict:
        """策展人接口：某版展签的证据图 + 争议摘要。"""
        lib = self._library()
        label = self._get_label(lib, label_id)
        if release_version is not None:
            release = self.get_label_release(label_id, release_version)
            return {
                "label_id": label_id,
                "release_version": release_version,
                "released_at": release["released_at"],
                "dispute_note": release.get("dispute_note", ""),
                "evidence_graph": release["evidence_graph"],
                "disputes": release["disputes"],
            }
        cited_ids: list[str] = []
        seen: set[str] = set()
        for c in label.active_citations:
            if c.claim_id not in seen:
                seen.add(c.claim_id)
                cited_ids.append(c.claim_id)
        errors, disputes = d.release_gate(lib, label)
        return {
            "label_id": label_id,
            "draft": True,
            "evidence_graph": d.build_evidence_graph(lib, cited_ids),
            "disputes": d.dispute_summary(lib, cited_ids),
            "gate_errors": errors,
        }

    def impact_preview(self, claim_id: str) -> dict:
        """勘误前预演：哪些已发布展签会被送回复审，哪些不会。"""
        lib = self._library()
        self._get_claim(lib, claim_id)
        closure = sorted(d.dependency_closure(lib, claim_id))
        impacted = d.affected_labels(lib, claim_id)
        return {
            "claim_id": claim_id,
            "dependency_closure": closure,
            "reopened_labels": [
                {"label_id": l.label_id, "release_version": l.current_version}
                for l in impacted
            ],
            "unaffected_published_labels": [
                {"label_id": l.label_id, "release_version": l.current_version}
                for l in lib.labels.values()
                if l.releases and l.status != "ARCHIVED" and l not in impacted
            ],
        }

    def state_at(self, as_of: str) -> dict:
        """研究者接口：重现系统在任意历史时点的状态。"""
        moment = datetime.fromisoformat(as_of)
        lib = d.library_at(self.store.load(), moment)
        return {
            "as_of": as_of,
            "claims": {cid: d.claim_to_dict(c) for cid, c in lib.claims.items()},
            "labels": {lid: d.label_to_dict(l) for lid, l in lib.labels.items()},
        }

    def event_log(self) -> list[dict]:
        return self.store.load()
