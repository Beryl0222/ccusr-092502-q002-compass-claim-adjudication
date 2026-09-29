"""主张库领域服务：登记、复核、勘误、展签发布与证据再现。

所有状态都由只增事件流重放得到（见 :class:`src.store.EventStore`），
本模块不保存任何可原地改写的业务事实：

* 八类来源各自独立登记，互不抬级（文献摘引、藏品断代、复原方案、
  实验记录、学者观点、翻译、展签、教育活动）；
* 每条主张标注四级证据层级，冲突主张可并存；
* 对外发布前，引用闭包中的每条主张必须通过其所属专业的复核；
* 勘误沿论证依赖闭包传播，只把真正依赖旧主张的在展展签送回复审；
* 每次发布留存完整快照；撤销只追加结论，不删除审议过程；
* 任一时点的状态均可按事件发生时间重放重现。
"""
from __future__ import annotations

import threading
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from src.store import EventStore, VersionConflict  # noqa: F401  (VersionConflict 对外重导出)

# ---------------------------------------------------------------- 领域词表

CLAIM_KINDS = (
    "document_quote",      # 文献摘引：古籍原文逐字摘引
    "artifact_dating",     # 藏品断代：出土/传世实物的断代结论
    "reconstruction",      # 复原方案：后世复原制作方案
    "experiment",          # 实验记录：可行性/重复性实验
    "scholar_opinion",     # 学者观点：个人或学派论断
    "translation",         # 翻译：今译/外译及其解释
    "exhibit_text",        # 展签：对外陈列文本（聚合单独管理，此值留作词表）
    "education_activity",  # 教育活动：教育项目所引用结论（聚合单独管理）
)

# 证据层级。前三级为正向支撑，DISPUTED 为显式存疑。
EVIDENCE_TIERS = (
    "DOCUMENTARY",  # 史料记载
    "ARTIFACT",     # 实物证据
    "EXPERIMENTAL", # 实验支持
    "DISPUTED",     # 仍存争议
)

REVIEW_DECISIONS = ("approved", "changes_requested", "rejected", "revoked")

EVENT_TYPES = (
    "CLAIM_REGISTERED",
    "EVIDENCE_LINKED",
    "REVIEW_RECORDED",
    "CLAIM_SUPERSEDED",
    "CLAIM_WITHDRAWN",
    "EXHIBIT_CREATED",
    "EXHIBIT_REVISED",
    "TEXT_RELEASED",
    "IMPACT_REOPENED",
    "IMPACT_RESOLVED",
    "TEXT_WITHDRAWN",
    "ACTIVITY_REGISTERED",
)


class DomainError(ValueError):
    """业务规则不允许的操作。"""


class NotFound(LookupError):
    """聚合或主张不存在。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- 服务

class ClaimService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._wlock = threading.RLock()

    # ------------------------------------------------------------ 内部工具

    def _append(self, event_type: str, aggregate_id: str, payload: dict[str, Any],
                expected_version: int | None = None, *, occurred_at: str | None = None,
                event_id: str | None = None) -> dict[str, Any]:
        if expected_version is None:
            expected_version = self.store.version_of(aggregate_id)
        return self.store.append(
            event_type, aggregate_id, payload, expected_version,
            occurred_at=occurred_at, event_id=event_id,
        )

    def state(self, as_of: str | None = None) -> dict[str, Any]:
        """重放得到（某历史时点的）完整状态。as_of 为含时区的 ISO 时间。"""
        return _reduce(self.store.events(as_of))

    def _check_ids(self, state: dict[str, Any], ids: tuple[str, ...] | list[str],
                   label: str, *, allow_missing: bool = False) -> None:
        missing = [i for i in ids if i not in state["claims"]]
        if missing:
            raise DomainError(f"{label}指向不存在的主张: {missing}")
        if not allow_missing:
            withdrawn = [i for i in ids if state["claims"][i]["withdrawn"]]
            if withdrawn:
                raise DomainError(f"{label}指向已撤销的主张: {withdrawn}")

    # ------------------------------------------------------------ 主张登记

    def register_claim(
        self,
        kind: str,
        statement: str,
        discipline: str,
        *,
        tier: str | None = None,
        source_ref: str | None = None,
        registered_by: str | None = None,
        depends_on: list[str] | tuple[str, ...] = (),
        conflicts_with: list[str] | tuple[str, ...] = (),
        supersedes: list[str] | tuple[str, ...] = (),
        note: str = "",
        claim_id: str | None = None,
        expected_version: int = 0,
        occurred_at: str | None = None,
    ) -> str:
        """登记一条主张。claim 种类限定为六类"来源型"主张。

        依赖、冲突、勘误关系在登记时固定；supersedes 表示本主张是对
        所列旧主张的新断代/勘误，登记后自动把旧主张标记为被勘误，
        并重开真正依赖它的在展展签。
        """
        if kind not in CLAIM_KINDS or kind in ("exhibit_text", "education_activity"):
            raise DomainError(f"未知主张类型: {kind}")
        if not statement or not statement.strip():
            raise DomainError("主张陈述不能为空")
        if not discipline or not discipline.strip():
            raise DomainError("必须标明所属专业（discipline）")
        tier_explicit = tier is not None
        if tier is None:
            tier = _default_tier(kind)
        if tier not in EVIDENCE_TIERS:
            raise DomainError(f"未知证据层级: {tier}")
        if tier_explicit and tier == "DISPUTED" and not conflicts_with and not note:
            raise DomainError("显式标注“仍存争议”时须给出争议对象或争议说明")

        with self._wlock:
            state = self.state()
            self._check_ids(state, depends_on, "论证依赖")
            self._check_ids(state, conflicts_with, "冲突关系")
            self._check_ids(state, supersedes, "勘误对象")
            claim_id = claim_id or _new_id("claim")
            payload = {
                "kind": kind,
                "statement": statement,
                "source_ref": source_ref,
                "tier": tier,
                "discipline": discipline,
                "registered_by": registered_by,
                "depends_on": list(depends_on),
                "conflicts_with": list(conflicts_with),
                "supersedes": list(supersedes),
                "note": note,
            }
            self._append("CLAIM_REGISTERED", claim_id, payload, expected_version,
                         occurred_at=occurred_at)
            if supersedes:
                self._apply_correction(claim_id, list(supersedes), occurred_at=occurred_at)
            return claim_id

    def link_evidence(self, claim_id: str, kind: str, citation: str, *,
                      linked_by: str | None = None, note: str = "",
                      expected_version: int | None = None,
                      occurred_at: str | None = None) -> None:
        """为主张挂接具体证据材料（拓片、照片、数据表、藏书页等）。"""
        if not kind.strip() or not citation.strip():
            raise DomainError("证据类型与出处不能为空")
        with self._wlock:
            state = self.state()
            if claim_id not in state["claims"]:
                raise NotFound(f"主张不存在: {claim_id}")
            self._append("EVIDENCE_LINKED", claim_id, {
                "evidence_id": _new_id("evi"),
                "kind": kind, "citation": citation, "note": note,
                "linked_by": linked_by,
            }, expected_version, occurred_at=occurred_at)

    def record_review(self, claim_id: str, discipline: str, decision: str,
                      reviewer: str, note: str = "", *,
                      expected_version: int | None = None,
                      occurred_at: str | None = None) -> str:
        """记录一次专业复核。同一通道的多次决议只增，最近一次为当前结论。

        decision=revoked 用于撤销此前的通过；审议全过程不可删除。
        """
        if decision not in REVIEW_DECISIONS:
            raise DomainError(f"未知复核结论: {decision}")
        if not reviewer or not reviewer.strip():
            raise DomainError("复核人不能为空")
        with self._wlock:
            state = self.state()
            claim = state["claims"].get(claim_id)
            if claim is None:
                raise NotFound(f"主张不存在: {claim_id}")
            if claim["withdrawn"]:
                raise DomainError("主张已撤销，不再接受复核（审议记录仍保留）")
            if discipline != claim["discipline"]:
                raise DomainError(
                    f"复核专业“{discipline}”与主张所属专业“{claim['discipline']}”不符，"
                    "须由对应专业复核"
                )
            channel = f"review:{claim_id}:{discipline}"
            self._append("REVIEW_RECORDED", channel, {
                "claim_id": claim_id,
                "discipline": discipline,
                "decision": decision,
                "reviewer": reviewer,
                "note": note,
            }, expected_version, occurred_at=occurred_at)
            return channel

    def withdraw_claim(self, claim_id: str, reason: str, *, by: str | None = None,
                       expected_version: int | None = None,
                       occurred_at: str | None = None) -> None:
        """撤销一条主张。展签历史快照与复核审议均不受影响、不得删除。"""
        if not reason or not reason.strip():
            raise DomainError("撤销必须给出理由")
        with self._wlock:
            if claim_id not in self.state()["claims"]:
                raise NotFound(f"主张不存在: {claim_id}")
            self._append("CLAIM_WITHDRAWN", claim_id,
                         {"reason": reason, "by": by}, expected_version,
                         occurred_at=occurred_at)
            self._reopen_affected(claim_id, [claim_id],
                                  f"所依据的主张已撤销：{reason}",
                                  occurred_at=occurred_at)

    # ------------------------------------------------------------ 勘误影响面

    def _apply_correction(self, new_claim_id: str, old_ids: list[str], *,
                          occurred_at: str | None = None) -> None:
        """对旧主张追加被勘误事件，并把受影响的在展展签送回复审。

        调用方须持有 ``_wlock``。
        """
        state = self.state()
        for old in old_ids:
            payload = {"superseded_by": new_claim_id,
                       "reason": state["claims"][new_claim_id]["statement"]}
            self._append("CLAIM_SUPERSEDED", old, payload,
                         self.store.version_of(old), occurred_at=occurred_at)
        self._reopen_affected(new_claim_id, old_ids,
                              "引用闭包中的主张已被新断代/勘误取代",
                              superseded_ids=old_ids, occurred_at=occurred_at)

    def _reopen_affected(self, triggered_by: str | None, target_ids: list[str],
                         reason: str, *, superseded_ids: list[str] | None = None,
                         occurred_at: str | None = None) -> None:
        """把引用闭包真正覆盖 target_ids 的在展展签（含"加注继续展出"）打回。

        只比对最近一次发布快照的引用闭包；草稿、已撤展和不相关展签不受影响。
        调用方须持有 ``_wlock``。
        """
        state = self.state()
        # 目标集就是被勘误/撤销的主张本身：展签引用闭包已含全部传递依赖，
        # 再向下展开反而会误伤"仅共享某个底层依赖"的无关展签。
        targets = set(target_ids)
        for exhibit_id, ex in state["exhibits"].items():
            if ex["status"] not in ("published", "resolved"):
                continue
            assert ex["current_release"] is not None
            affected = sorted(
                set(ex["current_release"]["snapshot"]["refs_closure"]) & targets
            )
            if not affected:
                continue
            chains = {a: _find_chain(state, ex["current_release"]["refs"], a)
                      for a in affected}
            chains = {a: chain for a, chain in chains.items() if chain}
            payload = {
                "triggered_by": triggered_by,
                "affected_claims": sorted(affected),
                "affected_chains": chains,
                "reason": reason,
            }
            if superseded_ids is not None:
                payload["superseded_claims"] = list(superseded_ids)
                payload["withdrawn_claims"] = []
            else:
                payload["superseded_claims"] = []
                payload["withdrawn_claims"] = list(target_ids)
            self._append("IMPACT_REOPENED", exhibit_id, payload,
                         self.store.version_of(exhibit_id), occurred_at=occurred_at)

    # ------------------------------------------------------------ 展签生命周期

    def create_exhibit_text(self, label: str, refs: list[str], *,
                            created_by: str | None = None, note: str = "",
                            exhibit_id: str | None = None, expected_version: int = 0,
                            occurred_at: str | None = None) -> str:
        if not label or not label.strip():
            raise DomainError("展签标题不能为空")
        with self._wlock:
            state = self.state()
            self._check_ids(state, refs, "展签引用", allow_missing=True)
            exhibit_id = exhibit_id or _new_id("exhibit")
            self._append("EXHIBIT_CREATED", exhibit_id, {
                "label": label, "refs": list(refs),
                "created_by": created_by, "note": note,
            }, expected_version, occurred_at=occurred_at)
            return exhibit_id

    def revise_exhibit_text(self, exhibit_id: str, refs: list[str], *,
                            reason: str = "", by: str | None = None,
                            expected_version: int | None = None,
                            occurred_at: str | None = None) -> None:
        """改定展签引用集合。已发布展签改定后回到草稿，已发布版本原样保留。"""
        with self._wlock:
            state = self.state()
            if exhibit_id not in state["exhibits"]:
                raise NotFound(f"展签不存在: {exhibit_id}")
            self._check_ids(state, refs, "展签引用", allow_missing=True)
            self._append("EXHIBIT_REVISED", exhibit_id,
                         {"refs": list(refs), "reason": reason, "by": by},
                         expected_version, occurred_at=occurred_at)

    def release_exhibit_text(self, exhibit_id: str, *, released_by: str | None = None,
                             expected_version: int | None = None,
                             occurred_at: str | None = None) -> dict[str, Any]:
        """发布门禁：引用闭包内每条主张须通过对应专业复核，且未撤销/未被勘误。

        通过后留存完整快照（主张、复核、证据材料在发布时点的状态），
        作为"当时怎么说、依据是什么"的永久凭证。
        """
        with self._wlock:
            state = self.state()
            ex = state["exhibits"].get(exhibit_id)
            if ex is None:
                raise NotFound(f"展签不存在: {exhibit_id}")
            if ex["status"] == "withdrawn":
                raise DomainError("展签已撤展，不能再发布")
            blockers = _blockers(state, ex["refs"], ex.get("annotated"))
            if blockers:
                raise DomainError("发布门禁未通过: " + "；".join(blockers))
            if ex["current_release"] is None:
                release_no = 1
            else:
                release_no = ex["current_release"]["release_no"] + 1
            at = occurred_at or _now()
            snapshot = _snapshot(state, ex["refs"], released_at=at,
                                 annotated=ex.get("annotated"))
            event = self._append("TEXT_RELEASED", exhibit_id, {
                "release_no": release_no,
                "refs": list(ex["refs"]),
                "released_by": released_by,
                "blocker_report": {"blockers": []},
                "snapshot": snapshot,
            }, expected_version, occurred_at=at)
            return {"release_no": release_no, "event_id": event["event_id"],
                    "released_at": at}

    def reopen_exhibit_text(self, exhibit_id: str, reason: str, *,
                            by: str | None = None, expected_version: int | None = None,
                            occurred_at: str | None = None) -> None:
        """手动把在展展签拉回复审（勘误触发由系统自动完成）。"""
        if not reason.strip():
            raise DomainError("复审必须填写作法")
        with self._wlock:
            state = self.state()
            ex = state["exhibits"].get(exhibit_id)
            if ex is None:
                raise NotFound(f"展签不存在: {exhibit_id}")
            if ex["status"] not in ("published", "resolved"):
                raise DomainError("仅在展（含加注继续展出）展签可以送回复审")
            self._append("IMPACT_REOPENED", exhibit_id, {
                "triggered_by": None, "superseded_claims": [],
                "affected_claims": [], "affected_chains": {},
                "reason": f"手动复审：{reason}", "by": by,
            }, expected_version, occurred_at=occurred_at)

    def resolve_reopened(self, exhibit_id: str, decision: str, note: str, *,
                         by: str | None = None, expected_version: int | None = None,
                         occurred_at: str | None = None) -> None:
        """登记编委会对一次复审的决议，审议过程永久保留。

        * ``revise``：改定引用集合，随后须 revise 并重新走发布门禁；
        * ``kept_with_annotation``：新旧说法并列、加注继续展出。此时被勘误
          主张仍保留在引用中，但要求取代它的新主张已通过对应专业复核，
          编辑无权自行把争议说法当定论发布。
        """
        if decision not in ("revise", "kept_with_annotation"):
            raise DomainError("未知复审决议（应为 revise / kept_with_annotation）")
        if not note.strip():
            raise DomainError("复审决议必须写出说明")
        with self._wlock:
            state = self.state()
            ex = state["exhibits"].get(exhibit_id)
            if ex is None:
                raise NotFound(f"展签不存在: {exhibit_id}")
            if ex["status"] != "reopened":
                raise DomainError("展签当前不在复审状态")
            last_reopen = ex["reopen_history"][-1]
            covered = list(last_reopen["affected_claims"])
            if decision == "kept_with_annotation":
                missing_approval: list[str] = []
                for old in covered:
                    claim = state["claims"].get(old)
                    new_id = claim and claim["superseded_by"]
                    if not new_id:
                        continue
                    new_claim = state["claims"][new_id]
                    ch = f"review:{new_id}:{new_claim['discipline']}"
                    review = state["reviews"].get(ch)
                    if not review or review["decision"] != "approved":
                        missing_approval.append(new_id)
                if missing_approval:
                    raise DomainError(
                        "新旧并列继续展出前，取代性新主张须先通过对应专业复核: "
                        + ", ".join(sorted(set(missing_approval)))
                    )
            self._append("IMPACT_RESOLVED", exhibit_id, {
                "reopen_at": last_reopen["at"],
                "decision": decision,
                "covered_claims": covered,
                "triggered_by": last_reopen["triggered_by"],
                "note": note, "by": by,
            }, expected_version, occurred_at=occurred_at)

    def withdraw_exhibit_text(self, exhibit_id: str, reason: str, *,
                              by: str | None = None, expected_version: int | None = None,
                              occurred_at: str | None = None) -> None:
        """撤展。历史发布版本与全部审议保留。"""
        if not reason.strip():
            raise DomainError("撤展必须给出理由")
        with self._wlock:
            state = self.state()
            if exhibit_id not in state["exhibits"]:
                raise NotFound(f"展签不存在: {exhibit_id}")
            self._append("TEXT_WITHDRAWN", exhibit_id,
                         {"reason": reason, "by": by}, expected_version,
                         occurred_at=occurred_at)

    # ------------------------------------------------------------ 教育活动

    def register_activity(self, title: str, refs: list[str], *,
                          by: str | None = None, note: str = "",
                          activity_id: str | None = None, expected_version: int = 0,
                          occurred_at: str | None = None) -> str:
        """登记教育活动所引用的结论；其对外就绪状态与展签走同一套门禁。"""
        if not title or not title.strip():
            raise DomainError("活动标题不能为空")
        with self._wlock:
            state = self.state()
            self._check_ids(state, refs, "活动引用", allow_missing=True)
            activity_id = activity_id or _new_id("activity")
            self._append("ACTIVITY_REGISTERED", activity_id, {
                "title": title, "refs": list(refs), "by": by, "note": note,
            }, expected_version, occurred_at=occurred_at)
            return activity_id

    def activity_readiness(self, activity_id: str) -> dict[str, Any]:
        state = self.state()
        activity = state["activities"].get(activity_id)
        if activity is None:
            raise NotFound(f"教育活动不存在: {activity_id}")
        blockers = _blockers(state, activity["refs"])
        return {"activity_id": activity_id, "ready": not blockers, "blockers": blockers}

    # ------------------------------------------------------------ 查询面

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        claim = self.state()["claims"].get(claim_id)
        if claim is None:
            raise NotFound(f"主张不存在: {claim_id}")
        return deepcopy(claim)

    def get_exhibit_text(self, exhibit_id: str) -> dict[str, Any]:
        ex = self.state()["exhibits"].get(exhibit_id)
        if ex is None:
            raise NotFound(f"展签不存在: {exhibit_id}")
        return deepcopy(ex)

    def evidence_graph(self, exhibit_id: str, *, release_no: int | None = None,
                       as_of: str | None = None) -> dict[str, Any]:
        """生成展签某版本的证据图。

        * ``release_no``：直接取该次发布快照，图结构永久定格；
        * 不给 ``release_no`` 而给 ``as_of``：重放该时点状态，取当时最新
          发布版本（若当时尚为草稿，则按草稿引用闭包构图）；
        * 都不给：取当前状态。
        """
        state = self.state(as_of)
        ex = state["exhibits"].get(exhibit_id)
        if ex is None:
            raise NotFound(f"展签不存在: {exhibit_id}")

        if release_no is not None:
            release = _find_release(ex, release_no)
            return _graph_from_snapshot(exhibit_id, ex, release)

        if ex["current_release"] is not None:
            return _graph_from_live(state, ex)
        return _graph_from_live(state, ex, draft=True)

    def dispute_summary(self, exhibit_id: str, *, release_no: int | None = None,
                        as_of: str | None = None) -> dict[str, Any]:
        """生成展签某版本的争议摘要与发布门禁结论。"""
        graph = self.evidence_graph(exhibit_id, release_no=release_no, as_of=as_of)
        return _summarize(graph)


# ---------------------------------------------------------------- 默认层级

def _default_tier(kind: str) -> str:
    return {
        "document_quote": "DOCUMENTARY",
        "artifact_dating": "ARTIFACT",
        "reconstruction": "DISPUTED",   # 复原方案本身不是原始证据
        "experiment": "EXPERIMENTAL",
        "scholar_opinion": "DISPUTED",  # 观点不能自我抬级为事实
        "translation": "DOCUMENTARY",
    }[kind]


# ---------------------------------------------------------------- 闭包/路径

def _closure(state: dict[str, Any], roots: list[str] | tuple[str, ...]) -> set[str]:
    """沿 depends_on 展开论证依赖闭包。"""
    seen: set[str] = set()
    queue = deque(roots)
    while queue:
        cid = queue.popleft()
        if cid in seen:
            continue
        seen.add(cid)
        claim = state["claims"].get(cid)
        if claim:
            queue.extend(claim["depends_on"])
    return seen


def _find_chain(state: dict[str, Any], roots: list[str], target: str) -> list[str]:
    """从某个直接引用节点出发，找一条到 target 的 depends_on 路径（含两端）。"""
    for root in roots:
        if root == target:
            return [root]
        prev: dict[str, str] = {root: root}
        queue = deque([root])
        while queue:
            cur = queue.popleft()
            claim = state["claims"].get(cur)
            if not claim:
                continue
            for nxt in claim["depends_on"]:
                if nxt in prev:
                    continue
                prev[nxt] = cur
                if nxt == target:
                    path = [nxt]
                    while path[-1] != root:
                        path.append(prev[path[-1]])
                    path.reverse()
                    return path
                queue.append(nxt)
    return []


# ---------------------------------------------------------------- 发布门禁

def _blockers(state: dict[str, Any], refs: list[str],
              annotated: dict[str, Any] | None = None) -> list[str]:
    """发布门禁。annotated 中的旧主张已由编委会决议"新旧并列加注"，放行。"""
    annotated = annotated or {}
    blockers: list[str] = []
    missing = sorted(r for r in refs if r not in state["claims"])
    if missing:
        blockers.append(f"引用了不存在的主张: {missing}")
    closure = _closure(state, [r for r in refs if r in state["claims"]])
    for cid in sorted(closure):
        claim = state["claims"][cid]
        if claim["withdrawn"]:
            blockers.append(f"主张 {cid} 已撤销（{claim['statement'][:24]}…）")
            continue
        if claim["superseded_by"] and cid not in annotated:
            blockers.append(
                f"主张 {cid} 已被 {claim['superseded_by']} 勘误取代"
                f"（{claim['statement'][:24]}…），该展签尚未完成复审"
            )
        review = state["reviews"].get(f"review:{cid}:{claim['discipline']}")
        if not review:
            blockers.append(
                f"主张 {cid} 未经“{claim['discipline']}”专业复核"
            )
        elif review["decision"] != "approved":
            blockers.append(
                f"主张 {cid} 的“{claim['discipline']}”复核结论为 "
                f"{review['decision']}（{review['reviewer']}）"
            )
    return blockers


def _snapshot(state: dict[str, Any], refs: list[str], *, released_at: str,
              annotated: dict[str, Any] | None = None) -> dict[str, Any]:
    closure = sorted(_closure(state, refs))
    claims = {cid: deepcopy(state["claims"][cid]) for cid in closure}
    reviews: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    annotations = {cid: deepcopy(info) for cid, info in (annotated or {}).items()
                   if cid in closure}
    for cid in closure:
        ch = f"review:{cid}:{state['claims'][cid]['discipline']}"
        if ch in state["reviews"]:
            reviews[cid] = deepcopy(state["reviews"][ch])
        if state["evidence"].get(cid):
            evidence[cid] = deepcopy(state["evidence"][cid])
    edges = []
    for cid in closure:
        for dep in state["claims"][cid]["depends_on"]:
            if dep in claims:
                edges.append({"from": cid, "to": dep, "relation": "depends_on"})
    return {
        "released_at": released_at,
        "refs": list(refs),
        "refs_closure": closure,
        "claims": claims,
        "reviews": reviews,
        "evidence": evidence,
        "edges": edges,
        "annotations": annotations,
    }


# ---------------------------------------------------------------- 图与摘要

TIER_LABELS = {
    "DOCUMENTARY": "史料记载",
    "ARTIFACT": "实物证据",
    "EXPERIMENTAL": "实验支持",
    "DISPUTED": "仍存争议",
}
KIND_LABELS = {
    "document_quote": "文献摘引",
    "artifact_dating": "藏品断代",
    "reconstruction": "复原方案",
    "experiment": "实验记录",
    "scholar_opinion": "学者观点",
    "translation": "翻译",
}


def _graph_from_live(state: dict[str, Any], ex: dict[str, Any], *,
                     draft: bool = False) -> dict[str, Any]:
    refs = ex["refs"]
    closure_ids = sorted(_closure(state, refs))
    nodes: list[dict[str, Any]] = []
    for cid in closure_ids:
        c = state["claims"][cid]
        ch = f"review:{cid}:{c['discipline']}"
        review = state["reviews"].get(ch)
        nodes.append({
            "id": cid, "kind": c["kind"],
            "kind_label": KIND_LABELS.get(c["kind"], c["kind"]),
            "tier": c["tier"], "tier_label": TIER_LABELS[c["tier"]],
            "statement": c["statement"], "source_ref": c["source_ref"],
            "discipline": c["discipline"],
            "registered_at": c["registered_at"],
            "withdrawn": c["withdrawn"],
            "superseded_by": c["superseded_by"],
            "conflicts_with": list(c["conflicts_with"]),
            "review": (None if review is None else
                       {"decision": review["decision"],
                        "reviewer": review["reviewer"], "at": review["at"],
                        "note": review["note"]}),
            "evidence_items": deepcopy(state["evidence"].get(cid, [])),
            "directly_referenced": cid in refs,
            "annotation": deepcopy(ex.get("annotated", {}).get(cid)),
        })
    edges: list[dict[str, Any]] = []
    for c in closure_ids:
        for dep in state["claims"][c]["depends_on"]:
            if dep in closure_ids:
                edges.append({"from": c, "to": dep, "relation": "depends_on"})
    # 冲突对去重（双方可能互相登记冲突关系）。
    pairs = {tuple(sorted((c, d))): {"a": c, "b": d}
             for c in closure_ids
             for d in state["claims"][c]["conflicts_with"]
             if d in closure_ids}
    return {
        "basis": "draft" if draft else ("current" if ex["status"] != "published"
                                        else "current_release"),
        "exhibit_id": ex["id"], "label": ex["label"], "status": ex["status"],
        "release_no": ex["current_release"]["release_no"]
                      if ex["current_release"] else None,
        "released_at": ex["current_release"]["snapshot"]["released_at"]
                       if ex["current_release"] else None,
        "refs": list(refs),
        "nodes": nodes,
        "edges": edges,
        "conflict_pairs": sorted(pairs.values(), key=lambda p: (p["a"], p["b"])),
        "tier_counts": _tier_counts(nodes),
    }


def _tier_counts(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {t: 0 for t in EVIDENCE_TIERS}
    for n in nodes:
        counts[n["tier"]] += 1
    return counts


def _find_release(ex: dict[str, Any], release_no: int) -> dict[str, Any]:
    for rel in ex["releases"]:
        if rel["release_no"] == release_no:
            return rel
    raise NotFound(f"展签 {ex['id']} 不存在发布版本 v{release_no}")


def _graph_from_snapshot(exhibit_id: str, ex: dict[str, Any],
                         release: dict[str, Any]) -> dict[str, Any]:
    snap = release["snapshot"]
    nodes: list[dict[str, Any]] = []
    for cid in snap["refs_closure"]:
        c = snap["claims"][cid]
        review = snap["reviews"].get(cid)
        nodes.append({
            "id": cid, "kind": c["kind"],
            "kind_label": KIND_LABELS.get(c["kind"], c["kind"]),
            "tier": c["tier"], "tier_label": TIER_LABELS[c["tier"]],
            "statement": c["statement"], "source_ref": c["source_ref"],
            "discipline": c["discipline"],
            "registered_at": c["registered_at"],
            "withdrawn": c["withdrawn"],
            "superseded_by": c["superseded_by"],
            "conflicts_with": list(c["conflicts_with"]),
            "review": (None if review is None else
                       {"decision": review["decision"],
                        "reviewer": review["reviewer"], "at": review["at"],
                        "note": review["note"]}),
            "evidence_items": deepcopy(snap["evidence"].get(cid, [])),
            "directly_referenced": cid in snap["refs"],
            "annotation": deepcopy(snap.get("annotations", {}).get(cid)),
        })
    pairs = {tuple(sorted((c, d))): {"a": c, "b": d}
             for c in snap["claims"]
             for d in snap["claims"][c]["conflicts_with"]
             if d in snap["claims"]}
    return {
        "basis": f"release:{release['release_no']}",
        "exhibit_id": exhibit_id, "label": ex["label"],
        "status": ex["status"],
        "release_no": release["release_no"],
        "released_at": snap["released_at"],
        "refs": list(snap["refs"]),
        "nodes": nodes,
        "edges": deepcopy(snap["edges"]),
        "conflict_pairs": [pairs[k] for k in sorted(pairs)],
        "tier_counts": _tier_counts(nodes),
    }


def _summarize(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    unresolved: list[str] = []
    disputed: list[dict[str, Any]] = []
    for n in graph["nodes"]:
        review = n["review"]
        if review is None or review["decision"] != "approved":
            unresolved.append(n["id"])
        if n["tier"] == "DISPUTED":
            disputed.append({
                "claim_id": n["id"], "statement": n["statement"],
                "kind_label": n["kind_label"],
                "review": None if review is None else review["decision"],
                "conflicts_with": [
                    {"claim_id": other,
                     "statement": nodes[other]["statement"] if other in nodes else None,
                     "tier_label": nodes[other]["tier_label"] if other in nodes else None}
                    for other in n["conflicts_with"]
                ],
            })
    conflicts = []
    for pair in graph["conflict_pairs"]:
        a, b = nodes[pair["a"]], nodes[pair["b"]]
        conflicts.append({
            "claims": [
                {"claim_id": a["id"], "statement": a["statement"],
                 "tier_label": a["tier_label"], "kind_label": a["kind_label"],
                 "review": None if a["review"] is None else a["review"]["decision"]},
                {"claim_id": b["id"], "statement": b["statement"],
                 "tier_label": b["tier_label"], "kind_label": b["kind_label"],
                 "review": None if b["review"] is None else b["review"]["decision"]},
            ],
        })
    annotated = {n["id"]: n["annotation"] for n in graph["nodes"] if n.get("annotation")}
    superseded = [n["id"] for n in graph["nodes"]
                  if n["superseded_by"] and n["id"] not in annotated]
    withdrawn = [n["id"] for n in graph["nodes"] if n["withdrawn"]]
    blockers: list[str] = []
    if unresolved:
        blockers.append("以下主张未经对应专业复核通过: " + ", ".join(sorted(unresolved)))
    if superseded:
        blockers.append("以下主张已被勘误取代: " + ", ".join(sorted(superseded)))
    if withdrawn:
        blockers.append("以下主张已撤销: " + ", ".join(sorted(withdrawn)))
    return {
        "exhibit_id": graph["exhibit_id"], "label": graph["label"],
        "basis": graph["basis"], "release_no": graph["release_no"],
        "tier_counts": graph["tier_counts"],
        "conflicts": conflicts,
        "disputed_claims": disputed,
        "unreviewed_or_unapproved": sorted(unresolved),
        "superseded_in_scope": sorted(superseded),
        "withdrawn_in_scope": sorted(withdrawn),
        "annotated_in_scope": sorted(annotated),
        "can_publish": not blockers,
        "blockers": blockers,
        "note": ("证据层级不同的来源已分级并存在相互冲突的结论；"
                 "对外文本只能在对应专业复核通过后发布，争议须并列呈现。"),
    }


# ---------------------------------------------------------------- 事件归约

def _reduce(events: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "claims": {},
        "evidence": {},
        "reviews": {},
        "exhibits": {},
        "activities": {},
    }
    for e in events:
        etype, agg, p = e["event_type"], e["aggregate_id"], e["payload"]
        at = e["occurred_at"]
        if etype == "CLAIM_REGISTERED":
            state["claims"][agg] = {
                "id": agg,
                "kind": p["kind"],
                "statement": p["statement"],
                "source_ref": p.get("source_ref"),
                "tier": p["tier"],
                "discipline": p["discipline"],
                "registered_by": p.get("registered_by"),
                "registered_at": at,
                "depends_on": list(p.get("depends_on", [])),
                "conflicts_with": list(p.get("conflicts_with", [])),
                "supersedes": list(p.get("supersedes", [])),
                "superseded_by": None,
                "withdrawn": False,
                "withdraw_reason": None,
                "note": p.get("note", ""),
                "version": e["version"],
            }
        elif etype == "EVIDENCE_LINKED":
            state["evidence"].setdefault(agg, []).append({
                "evidence_id": p["evidence_id"],
                "kind": p["kind"],
                "citation": p["citation"],
                "note": p.get("note", ""),
                "linked_by": p.get("linked_by"),
                "at": at,
            })
        elif etype == "REVIEW_RECORDED":
            state["reviews"][agg] = {
                "claim_id": p["claim_id"],
                "discipline": p["discipline"],
                "decision": p["decision"],
                "reviewer": p["reviewer"],
                "note": p.get("note", ""),
                "at": at,
                "version": e["version"],
            }
        elif etype == "CLAIM_SUPERSEDED":
            if agg in state["claims"]:
                state["claims"][agg]["superseded_by"] = p["superseded_by"]
        elif etype == "CLAIM_WITHDRAWN":
            if agg in state["claims"]:
                state["claims"][agg]["withdrawn"] = True
                state["claims"][agg]["withdraw_reason"] = p["reason"]
        elif etype == "EXHIBIT_CREATED":
            state["exhibits"][agg] = {
                "id": agg, "label": p["label"],
                "refs": list(p["refs"]),
                "status": "draft",
                "current_release": None,
                "releases": [],
                "reopen_history": [],
                "resolution_history": [],
                "annotated": {},
                "created_at": at,
            }
        elif etype == "EXHIBIT_REVISED":
            ex = state["exhibits"][agg]
            ex["refs"] = list(p["refs"])
            ex["status"] = "draft"
            # 改定后只保留仍处于新引用闭包内的"并列加注"决议。
            kept_closure = _closure(state, ex["refs"])
            ex["annotated"] = {k: v for k, v in ex["annotated"].items()
                               if k in kept_closure}
        elif etype == "TEXT_RELEASED":
            release = {
                "release_no": p["release_no"],
                "refs": list(p["refs"]),
                "released_by": p.get("released_by"),
                "at": at,
                "snapshot": p["snapshot"],
            }
            ex = state["exhibits"][agg]
            ex["releases"].append(release)
            ex["current_release"] = release
            ex["status"] = "published"
        elif etype == "IMPACT_REOPENED":
            ex = state["exhibits"][agg]
            ex["status"] = "reopened"
            # 再次进入复审后，旧的"加注并列"决议失效，须重新作出决议。
            ex["annotated"] = {}
            ex["reopen_history"].append({
                "at": at,
                "triggered_by": p.get("triggered_by"),
                "superseded_claims": list(p.get("superseded_claims", [])),
                "withdrawn_claims": list(p.get("withdrawn_claims", [])),
                "affected_claims": list(p.get("affected_claims", [])),
                "affected_chains": p.get("affected_chains", {}),
                "reason": p.get("reason", ""),
            })
        elif etype == "IMPACT_RESOLVED":
            ex = state["exhibits"][agg]
            entry = {"at": at, "decision": p["decision"], "note": p["note"],
                     "by": p.get("by"), "reopen_at": p["reopen_at"],
                     "covered_claims": list(p.get("covered_claims", []))}
            ex["resolution_history"].append(entry)
            if p["decision"] == "kept_with_annotation":
                for cid in p.get("covered_claims", []):
                    claim = state["claims"].get(cid)
                    ex["annotated"][cid] = {
                        "since": at, "by": p.get("by"),
                        "superseded_by": claim["superseded_by"] if claim else None,
                        "note": p["note"],
                    }
            # 加注并列决议作出后，展签回到在展状态；发布门禁凭 annotated 放行，
            # 是否重新出快照由随后的 TEXT_RELEASED 表达。
            ex["status"] = "resolved"
        elif etype == "TEXT_WITHDRAWN":
            ex = state["exhibits"][agg]
            ex["status"] = "withdrawn"
            ex["withdraw_reason"] = p["reason"]
        elif etype == "ACTIVITY_REGISTERED":
            state["activities"][agg] = {
                "id": agg, "title": p["title"],
                "refs": list(p["refs"]),
                "registered_at": at,
                "note": p.get("note", ""),
            }
    return state
