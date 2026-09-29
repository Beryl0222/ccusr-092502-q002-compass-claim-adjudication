"""领域投影与业务规则。

所有状态都由追加事件归约而来，因此：
- 任意历史时点的状态可用 ``library_at`` 重现；
- 撤销只追加"失效标记"事件，审议过程永不删除；
- 曾经发布的展签连同当时证据冻结在发布快照里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 领域枚举（与 contracts/domain.json 保持一致）
# ---------------------------------------------------------------------------

CLAIM_KINDS = {
    "literature_excerpt",   # 文献摘引
    "artifact_dating",      # 藏品断代
    "reconstruction_plan",  # 复原方案
    "experiment_record",    # 实验记录
    "scholar_opinion",      # 学者观点
    "translation",          # 翻译
    "exhibit_label",        # 展签结论
    "education_activity",   # 教育活动结论
}

EVIDENCE_TIERS = {
    "HISTORICAL_RECORD",    # 史料记载
    "PHYSICAL_EVIDENCE",    # 实物证据
    "EXPERIMENTAL_SUPPORT", # 实验支持
    "DISPUTED",             # 仍存争议
}

RELATIONS = {"supports", "contradicts", "depends_on", "derived_from", "translates", "dates"}
DISCIPLINES = {"philology", "archaeology", "history_of_technology", "physics", "museology", "education"}

# 每类主张对外定论前必须取得的对应专业复核
REQUIRED_DISCIPLINE: dict[str, str] = {
    "literature_excerpt": "philology",
    "artifact_dating": "archaeology",
    "reconstruction_plan": "history_of_technology",
    "experiment_record": "physics",
    "scholar_opinion": "history_of_technology",
    "translation": "philology",
    "exhibit_label": "museology",
    "education_activity": "education",
}

DEPENDENCY_RELATIONS = {"depends_on", "derived_from", "translates", "dates"}


# ---------------------------------------------------------------------------
# 读模型
# ---------------------------------------------------------------------------

@dataclass
class Review:
    review_id: str
    discipline: str
    reviewer: str
    outcome: str
    note: str
    decided_at: str
    active: bool = True
    revoke_reason: str | None = None
    revoked_by: str | None = None
    revoked_at: str | None = None


@dataclass
class Evidence:
    evidence_id: str
    kind: str
    citation: str
    tier: str
    summary: str
    linked_by: str
    linked_at: str


@dataclass
class Claim:
    claim_id: str
    kind: str
    statement: str
    tier: str
    source_ref: str
    registered_by: str
    registered_at: str
    version: int = 1
    status: str = "ACTIVE"  # ACTIVE / SUPERSEDED
    superseded_by: str | None = None
    supersede_reason: str | None = None
    superseded_at: str | None = None
    relations: list[dict] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)


@dataclass
class Citation:
    claim_id: str
    how_used: str
    cited_at: str
    active: bool = True
    withdrawn_reason: str | None = None
    withdrawn_at: str | None = None


@dataclass
class ReopenRecord:
    trigger_claim_id: str
    reason: str
    affected_release_version: int
    reopened_at: str


@dataclass
class Label:
    label_id: str
    title: str
    body: str
    author: str
    drafted_at: str
    version: int = 1
    status: str = "DRAFT"  # DRAFT / PUBLISHED / REOPENED / ARCHIVED
    citations: list[Citation] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    releases: list[dict] = field(default_factory=list)
    revisions: list[dict] = field(default_factory=list)
    archive_reason: str | None = None
    archived_at: str | None = None
    reopen_history: list[ReopenRecord] = field(default_factory=list)

    @property
    def active_citations(self) -> list[Citation]:
        return [c for c in self.citations if c.active]

    @property
    def current_version(self) -> int | None:
        return self.releases[-1]["release_version"] if self.releases else None


@dataclass
class Library:
    claims: dict[str, Claim] = field(default_factory=dict)
    labels: dict[str, Label] = field(default_factory=dict)
    last_seq: int = -1


# ---------------------------------------------------------------------------
# 归约
# ---------------------------------------------------------------------------

def _as_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def library_at(events: Iterable[dict], as_of: datetime | None = None) -> Library:
    """把事件流归约成读模型；as_of 给出时只采用该时点之前（含）的事件。"""
    lib = Library()
    for event in sorted(events, key=lambda e: e["seq"]):
        if as_of is not None and _as_dt(event["occurred_at"]) > as_of:
            continue
        _apply(lib, event)
    return lib


def _apply(lib: Library, event: dict) -> None:
    etype = event["event_type"]
    p = event["payload"]
    agg = event["aggregate_id"]
    ts = event["occurred_at"]

    if etype == "CLAIM_REGISTERED":
        lib.claims[agg] = Claim(
            claim_id=agg,
            kind=p["kind"],
            statement=p["statement"],
            tier=p["tier"],
            source_ref=p.get("source_ref", ""),
            registered_by=p.get("registered_by", ""),
            registered_at=ts,
            version=event["version"],
        )

    elif etype == "CLAIM_SUPERSEDED":
        claim = lib.claims[agg]
        claim.status = "SUPERSEDED"
        claim.superseded_by = p["new_claim_id"]
        claim.supersede_reason = p["reason"]
        claim.superseded_at = ts
        claim.version = event["version"]

    elif etype == "CLAIM_RELATION_LINKED":
        claim = lib.claims[agg]
        claim.relations.append({
            "relation": p["relation"],
            "to_claim_id": p["to_claim_id"],
            "note": p.get("note", ""),
            "linked_at": ts,
        })
        claim.version = event["version"]

    elif etype == "EVIDENCE_LINKED":
        claim = lib.claims[agg]
        claim.evidence.append(Evidence(
            evidence_id=p["evidence_id"],
            kind=p.get("kind", ""),
            citation=p.get("citation", ""),
            tier=p["tier"],
            summary=p.get("summary", ""),
            linked_by=p.get("linked_by", ""),
            linked_at=ts,
        ))
        claim.version = event["version"]

    elif etype == "REVIEW_RECORDED":
        review = Review(
            review_id=p["review_id"],
            discipline=p["discipline"],
            reviewer=p.get("reviewer", ""),
            outcome=p["outcome"],
            note=p.get("note", ""),
            decided_at=ts,
        )
        if agg in lib.claims:
            lib.claims[agg].reviews.append(review)
            lib.claims[agg].version = event["version"]
        elif agg in lib.labels:
            lib.labels[agg].reviews.append(review)
            lib.labels[agg].version = event["version"]
        else:
            raise KeyError(f"复核对象不存在: {agg}")

    elif etype == "REVIEW_REVOKED":
        subject = lib.claims.get(agg) or lib.labels.get(agg)
        target = next((r for r in subject.reviews if r.review_id == p["review_id"]), None)
        if target is None:
            raise KeyError(f"复核记录不存在: {p['review_id']}")
        # 只置失效，审议记录本身保留
        target.active = False
        target.revoke_reason = p["reason"]
        target.revoked_by = p.get("revoked_by", "")
        target.revoked_at = ts
        subject.version = event["version"]

    elif etype == "TEXT_DRAFTED":
        lib.labels[agg] = Label(
            label_id=agg,
            title=p["title"],
            body=p.get("body", ""),
            author=p.get("author", ""),
            drafted_at=ts,
            version=event["version"],
        )

    elif etype == "TEXT_CLAIM_CITED":
        label = lib.labels[agg]
        label.citations.append(Citation(
            claim_id=p["claim_id"],
            how_used=p.get("how_used", ""),
            cited_at=ts,
        ))
        label.version = event["version"]

    elif etype == "TEXT_CITATION_WITHDRAWN":
        label = lib.labels[agg]
        target = next((c for c in label.citations if c.claim_id == p["claim_id"] and c.active), None)
        if target is None:
            raise KeyError(f"展签没有有效引用主张: {p['claim_id']}")
        target.active = False
        target.withdrawn_reason = p.get("reason", "")
        target.withdrawn_at = ts
        label.version = event["version"]

    elif etype == "TEXT_REVISED":
        label = lib.labels[agg]
        label.title = p.get("title", label.title)
        label.body = p.get("body", label.body)
        label.revisions.append({"revised_at": ts, "note": p.get("note", ""),
                                "by": p.get("by", "")})
        # 已撤销归档的展签不能靠修订复活
        if label.status != "ARCHIVED":
            label.status = "DRAFT"
        label.version = event["version"]

    elif etype == "TEXT_RELEASED":
        label = lib.labels[agg]
        label.status = "PUBLISHED"
        label.releases.append(p["snapshot"])
        label.version = event["version"]

    elif etype == "TEXT_ARCHIVED":
        label = lib.labels[agg]
        label.status = "ARCHIVED"
        label.archive_reason = p.get("reason", "")
        label.archived_at = ts
        label.version = event["version"]

    elif etype == "IMPACT_REOPENED":
        label = lib.labels[agg]
        label.status = "REOPENED"
        label.reopen_history.append(ReopenRecord(
            trigger_claim_id=p["trigger_claim_id"],
            reason=p.get("reason", ""),
            affected_release_version=p["affected_release_version"],
            reopened_at=ts,
        ))
        label.version = event["version"]

    else:
        raise ValueError(f"未知事件类型: {etype}")

    lib.last_seq = event["seq"]


# ---------------------------------------------------------------------------
# 依赖闭包与影响面
# ---------------------------------------------------------------------------

def dependency_closure(lib: Library, root_id: str) -> set[str]:
    """返回真正以 root_id 为基础的主张集合（含自身）。

    沿 depends_on / derived_from / translates / dates 正向追踪：
    若 A depends_on B，则 A 真正依赖 B。勘误 B 时，引用 A 或 B 的展签都受影响。
    冲突（contradicts）与支持（supports）不算依赖，不扩大复审面。
    """
    seen: set[str] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for claim in lib.claims.values():
            if claim.status != "ACTIVE":
                continue
            for rel in claim.relations:
                if rel["relation"] in DEPENDENCY_RELATIONS and rel["to_claim_id"] == current:
                    stack.append(claim.claim_id)
    return seen


def affected_labels(lib: Library, root_id: str) -> list[Label]:
    """已发布展签中，引用落在依赖闭包内的，才需要送回复审。"""
    closure = dependency_closure(lib, root_id)
    result: list[Label] = []
    for label in lib.labels.values():
        if not label.releases or label.status == "ARCHIVED":
            continue
        cited = {c.claim_id for c in label.citations if c.active}
        if cited & closure:
            result.append(label)
    return result


# ---------------------------------------------------------------------------
# 复核与争议查询
# ---------------------------------------------------------------------------

def active_approvals(claim: Claim, discipline: str | None = None) -> list[Review]:
    return [
        r for r in claim.reviews
        if r.active and r.outcome == "APPROVED" and (discipline is None or r.discipline == discipline)
    ]


def contradictors(lib: Library, claim_id: str) -> list[Claim]:
    """与某主张现存冲突的、仍有效的另一方主张（双向查边）。"""
    result: dict[str, Claim] = {}
    me = lib.claims.get(claim_id)
    if me is not None:
        for rel in me.relations:
            if rel["relation"] == "contradicts":
                other = lib.claims.get(rel["to_claim_id"])
                if other and other.status == "ACTIVE":
                    result[other.claim_id] = other
    for claim in lib.claims.values():
        if claim.status != "ACTIVE":
            continue
        for rel in claim.relations:
            if rel["relation"] == "contradicts" and rel["to_claim_id"] == claim_id:
                result[claim.claim_id] = claim
    return list(result.values())


def dispute_summary(lib: Library, claim_ids: list[str]) -> list[dict]:
    """汇总一组主张（如展签引用）面临的未决争议。"""
    disputes: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for cid in claim_ids:
        claim = lib.claims.get(cid)
        if claim is None:
            continue
        if claim.tier == "DISPUTED":
            key = tuple(sorted((cid, "SELF:DISPUTED")))
            if key not in seen_pairs:
                seen_pairs.add(key)
                disputes.append({
                    "type": "tier_disputed",
                    "claim_id": cid,
                    "statement": claim.statement,
                    "detail": "该主张证据层级登记为“仍存争议”，不能作为定论展出",
                })
        for other in contradictors(lib, cid):
            key = tuple(sorted((cid, other.claim_id)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            disputes.append({
                "type": "contradiction",
                "claim_ids": [cid, other.claim_id],
                "sides": [
                    _claim_side(claim),
                    _claim_side(other),
                ],
                "detail": "两条主张相互冲突，可并存，但展出时须并呈争议",
            })
    return disputes


def _claim_side(claim: Claim) -> dict:
    required = REQUIRED_DISCIPLINE.get(claim.kind)
    approved = bool(active_approvals(claim, required)) if required else True
    return {
        "claim_id": claim.claim_id,
        "statement": claim.statement,
        "tier": claim.tier,
        "kind": claim.kind,
        "specialty_approved": approved,
        "status": claim.status,
    }


# ---------------------------------------------------------------------------
# 发布门禁
# ---------------------------------------------------------------------------

def release_gate(lib: Library, label: Label, dispute_note: str = "") -> tuple[list[str], list[dict]]:
    """返回 (阻断错误, 争议清单)。无阻断错误才允许发布。"""
    errors: list[str] = []
    disputes: list[dict] = []

    if label.status == "ARCHIVED":
        errors.append(f"展签 {label.label_id} 已撤销归档，不能重新发布")

    if not label.active_citations:
        errors.append("展签至少需要引用一条主张，禁止无证据定论")

    seen: set[str] = set()
    ordered_ids: list[str] = []
    for c in label.active_citations:
        if c.claim_id not in seen:
            seen.add(c.claim_id)
            ordered_ids.append(c.claim_id)

    for cid in ordered_ids:
        claim = lib.claims.get(cid)
        if claim is None:
            errors.append(f"引用的主张不存在: {cid}")
            continue
        if claim.status == "SUPERSEDED":
            errors.append(
                f"主张 {cid} 已被勘误/重断代（{claim.supersede_reason}），"
                "不能以现行结论身份发布；如作历史说法展示须明确标注"
            )
        required = REQUIRED_DISCIPLINE.get(claim.kind)
        if required and not active_approvals(claim, required):
            errors.append(
                f"主张 {cid}（{claim.kind}）缺少 {required} 专业的有效批准复核，编辑不能代为定论"
            )

    disputes = dispute_summary(lib, ordered_ids)
    if disputes and not dispute_note.strip():
        errors.append("引用主张存在未决争议，发布时必须填写争议并呈说明（dispute_note）")

    if not active_approvals(label, "museology"):
        errors.append(f"展签 {label.label_id} 缺少 museology（展陈专业）的有效批准复核")

    # 被勘误/重断代打回的展签，必须取得晚于最近一次打回的展陈复核才能重新上线；
    # 已带新复核重新发布后（发布时间晚于打回时间）自动解除
    if label.reopen_history:
        last_reopen = max(_as_dt(r.reopened_at) for r in label.reopen_history)
        last_release = max((_as_dt(r["released_at"]) for r in label.releases), default=None)
        if last_release is None or last_reopen > last_release:
            fresh = [
                r for r in active_approvals(label, "museology")
                if _as_dt(r.decided_at) > last_reopen
            ]
            if not fresh:
                errors.append(
                    f"展签 {label.label_id} 因上游勘误被打回复审（最近打回 {last_reopen.isoformat()}），"
                    "需要展陈专业在打回之后重新批准"
                )

    return errors, disputes


# ---------------------------------------------------------------------------
# 发布快照与证据图
# ---------------------------------------------------------------------------

def build_release_snapshot(lib: Library, label: Label, release_version: int,
                           released_at: str, disputes: list[dict],
                           dispute_note: str) -> dict:
    """冻结当时的说法与证据，保证“曾经展出的说法及当时证据必须保留”。"""
    citations: list[dict] = []
    seen: set[str] = set()
    for c in label.active_citations:
        if c.claim_id in seen:
            continue
        seen.add(c.claim_id)
        claim = lib.claims.get(c.claim_id)
        citations.append({
            "claim_id": c.claim_id,
            "how_used": c.how_used,
            "claim_version": claim.version if claim else None,
            "statement": claim.statement if claim else None,
            "kind": claim.kind if claim else None,
            "tier": claim.tier if claim else None,
            "source_ref": claim.source_ref if claim else None,
            "evidence": [
                {
                    "evidence_id": e.evidence_id,
                    "kind": e.kind,
                    "citation": e.citation,
                    "tier": e.tier,
                    "summary": e.summary,
                }
                for e in (claim.evidence if claim else [])
            ],
            "active_approvals": [
                {"discipline": r.discipline, "reviewer": r.reviewer, "review_id": r.review_id}
                for r in claim.reviews if r.active and r.outcome == "APPROVED"
            ] if claim else [],
        })

    snapshot = {
        "release_version": release_version,
        "released_at": released_at,
        "title": label.title,
        "body": label.body,
        "author": label.author,
        "dispute_note": dispute_note,
        "citations": citations,
        "disputes": disputes,
        "evidence_graph": build_evidence_graph(lib, [c["claim_id"] for c in citations]),
    }
    return snapshot


def build_evidence_graph(lib: Library, cited_ids: list[str]) -> dict:
    """为一版展签生成证据图：主张、证据材料、彼此关系/冲突。"""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_claim(claim: Claim, cited: bool) -> None:
        if claim.claim_id in nodes:
            if cited:
                nodes[claim.claim_id]["cited"] = True
            return
        nodes[claim.claim_id] = {
            "id": claim.claim_id,
            "node_type": "claim",
            "label": claim.statement,
            "kind": claim.kind,
            "tier": claim.tier,
            "status": claim.status,
            "cited": cited,
        }

    for cid in cited_ids:
        claim = lib.claims.get(cid)
        if claim is None:
            nodes[cid] = {"id": cid, "node_type": "claim", "missing": True, "cited": True}
            continue
        add_claim(claim, cited=True)
        for ev in claim.evidence:
            ev_id = f"evidence:{ev.evidence_id}"
            nodes.setdefault(ev_id, {
                "id": ev_id,
                "node_type": "evidence",
                "label": ev.summary or ev.citation,
                "evidence_tier": ev.tier,
                "citation": ev.citation,
                "cited": False,
            })
            edges.append({"source": claim.claim_id, "target": ev_id, "type": "has_evidence"})
        for rel in claim.relations:
            other = lib.claims.get(rel["to_claim_id"])
            if other is not None:
                add_claim(other, cited=False)
            edges.append({
                "source": claim.claim_id,
                "target": rel["to_claim_id"],
                "type": rel["relation"],
                "note": rel["note"],
            })

    # 反向边（其他主张指向被引主张，例如别人的 contradicts/supports）
    for claim in lib.claims.values():
        for rel in claim.relations:
            if rel["to_claim_id"] in cited_ids:
                add_claim(claim, cited=False)
                edges.append({"source": claim.claim_id, "target": rel["to_claim_id"],
                              "type": rel["relation"], "note": rel["note"]})

    return {"nodes": list(nodes.values()), "edges": _dedup_edges(edges)}


def _dedup_edges(edges: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in edges:
        key = (e["source"], e["target"], e["type"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def claim_to_dict(claim: Claim) -> dict:
    return {
        "claim_id": claim.claim_id,
        "version": claim.version,
        "kind": claim.kind,
        "statement": claim.statement,
        "tier": claim.tier,
        "evidence_tier_label": _tier_label(claim.tier),
        "source_ref": claim.source_ref,
        "registered_by": claim.registered_by,
        "registered_at": claim.registered_at,
        "status": claim.status,
        "superseded_by": claim.superseded_by,
        "supersede_reason": claim.supersede_reason,
        "superseded_at": claim.superseded_at,
        "required_discipline": REQUIRED_DISCIPLINE.get(claim.kind),
        "relations": claim.relations,
        "evidence": [e.__dict__ for e in claim.evidence],
        "reviews": [r.__dict__ for r in claim.reviews],
    }


def label_to_dict(label: Label) -> dict:
    return {
        "label_id": label.label_id,
        "version": label.version,
        "title": label.title,
        "body": label.body,
        "author": label.author,
        "status": label.status,
        "drafted_at": label.drafted_at,
        "citations": [c.__dict__ for c in label.citations],
        "active_citation_ids": [c.claim_id for c in label.citations if c.active],
        "reviews": [r.__dict__ for r in label.reviews],
        "revisions": label.revisions,
        "reopen_history": [r.__dict__ for r in label.reopen_history],
        "release_versions": [r["release_version"] for r in label.releases],
        "current_release_version": label.current_version,
        "archive_reason": label.archive_reason,
        "archived_at": label.archived_at,
    }


_TIER_LABELS = {
    "HISTORICAL_RECORD": "史料记载",
    "PHYSICAL_EVIDENCE": "实物证据",
    "EXPERIMENTAL_SUPPORT": "实验支持",
    "DISPUTED": "仍存争议",
}


def _tier_label(tier: str) -> str:
    return _TIER_LABELS.get(tier, tier)
