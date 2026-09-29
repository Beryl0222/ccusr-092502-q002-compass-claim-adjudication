"""司南专题展端到端情景：从登记、发布、勘误打回到历史回溯。

用法::

    python3 scripts/sinan_scenario.py                 # 用临时日志，跑完即弃
    python3 scripts/sinan_scenario.py --store a.jsonl # 指定日志路径，可重复打开

脚本只使用公开服务接口，按固定业务时间写入，便于研究者逐事件核对。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.service import EVENT_TYPES, ClaimService, DomainError, VersionConflict
from src.store import EventStore


def t(hour: str) -> str:
    return f"2026-09-{hour}T09:00:00+08:00"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default=None, help="事件日志路径，缺省用临时文件")
    args = parser.parse_args()

    path = args.store or str(Path(tempfile.mkdtemp()) / "sinan.jsonl")
    svc = ClaimService(EventStore(path, EVENT_TYPES))
    out: list[str] = []

    def log(title: str, value: object = "") -> None:
        out.append(f"\n=== {title} ===")
        if value:
            out.append(json.dumps(value, ensure_ascii=False, indent=2))

    # 1) 六类来源分级登记；相互冲突的结论并存 -----------------------------
    svc.register_claim(
        "document_quote",
        "《论衡·是应》：“司南之杓，投之于地，其柢指南。”",
        "历史文献学", source_ref="论衡·是应篇",
        claim_id="doc-lunheng", occurred_at=t("20"))
    svc.register_claim(
        "translation",
        "今译：将司南这把勺投在地上，它的柄指向南方。",
        "历史文献学", source_ref="《论衡》通行今译本",
        depends_on=["doc-lunheng"], claim_id="tr-modern", occurred_at=t("20"))
    svc.register_claim(
        "artifact_dating",
        "邯郸汉墓出土勺状器被判定为天然磁石制成的司南实物（1950 年代断代）。",
        "考古学", source_ref="1957 邯郸考古简报",
        claim_id="dating-1957", occurred_at=t("21"))
    svc.register_claim(
        "scholar_opinion",
        "王振铎：司南是磁性指向器，为指南针前身。",
        "科技史", depends_on=["doc-lunheng", "dating-1957"],
        claim_id="op-magnetic", occurred_at=t("21"))
    svc.register_claim(
        "reconstruction",
        "王振铎复原方案：木胎髹漆、镶嵌天然磁石的勺，置于青铜地盘。",
        "科技史", depends_on=["op-magnetic"],
        source_ref="《中国革命博物馆陈列品》复原图",
        claim_id="recon-wang", occurred_at=t("22"))
    svc.register_claim(
        "scholar_opinion",
        "林文照等：文献只言“投之于地”，磁性勺在地盘摩擦阻力下无法指南，"
        "司南是否为磁性指向器仍存争议。",
        "科技史", tier="DISPUTED",
        conflicts_with=["op-magnetic", "dating-1957"],
        note="1980 年代以来的持续学术争议",
        claim_id="op-nonmagnetic", occurred_at=t("22"))
    svc.register_claim(
        "experiment",
        "重复实验：以天然磁石按复原方案制勺，多次投置均不能稳定指南。",
        "实验物理", source_ref="2006 重复实验数据表",
        conflicts_with=["recon-wang"],
        claim_id="exp-2006", occurred_at=t("23"))
    log("已登记 7 条主张：文献、翻译、旧断代、两派观点、复原、实验")

    # 2) 挂接证据材料并按对应专业复核 -------------------------------------
    svc.link_evidence("doc-lunheng", "古籍书页", "宋刻本《论衡》书影",
                      linked_by="资料员", occurred_at=t("24"))
    svc.link_evidence("dating-1957", "出土档案", "1957 发掘记录与器型照片",
                      linked_by="藏品部", occurred_at=t("24"))
    svc.link_evidence("exp-2006", "实验数据", "摩擦阻力与指向偏差记录表",
                      linked_by="实验组", occurred_at=t("24"))
    for cid, reviewer in [
        ("doc-lunheng", "文献学复核人"),
        ("tr-modern", "文献学复核人"),
        ("dating-1957", "考古学复核人"),
        ("op-magnetic", "科技史复核人"),
        ("recon-wang", "科技史复核人"),
    ]:
        svc.record_review(cid, svc.get_claim(cid)["discipline"], "approved",
                          reviewer, note="可作展陈依据", occurred_at=t("24"))
    # 反方观点与实验同样通过本专业复核——争议双方都可呈现。
    svc.record_review("op-nonmagnetic", "科技史", "approved",
                      "科技史复核人", note="争议一方，应并列呈现", occurred_at=t("24"))
    svc.record_review("exp-2006", "实验物理", "approved",
                      "物理实验复核人", note="实验过程可重复", occurred_at=t("24"))
    log("各主张完成所属专业复核（文献学/考古学/科技史/实验物理）")

    # 非对应专业的复核必须被拒绝。
    try:
        svc.record_review("dating-1957", "科技史", "approved", "越界复核人")
    except DomainError as exc:
        log("跨专业复核被拒绝", str(exc))

    # 3) 发布两版展签：司南主展签、无关的航海展签 --------------------------
    svc.create_exhibit_text(
        "司南——世界上最早的磁性指向器？",
        ["doc-lunheng", "tr-modern", "dating-1957", "op-magnetic",
         "op-nonmagnetic", "recon-wang", "exp-2006"],
        exhibit_id="label-sinan", created_by="策展组", occurred_at=t("25"))
    svc.create_exhibit_text(
        "罗盘与大航海", ["doc-lunheng"],
        exhibit_id="label-sea", created_by="策展组", occurred_at=t("25"))
    svc.release_exhibit_text("label-sinan", released_by="策展组", occurred_at=t("25"))
    svc.release_exhibit_text("label-sea", released_by="策展组", occurred_at=t("25"))
    log("两版展签发布；司南展签证据图（v1）",
        svc.evidence_graph("label-sinan", release_no=1)["tier_counts"])

    # 4) 新断代/勘误：只打回真正依赖旧断代的在展展签 -----------------------
    svc.register_claim(
        "artifact_dating",
        "对出土器重新清理与检测：该器为骨质勺具/占具，不支持天然磁石司南说。",
        "考古学", source_ref="2015 重新清理检测报告",
        supersedes=["dating-1957"],
        claim_id="dating-2015", occurred_at=t("26"))
    sinan = svc.get_exhibit_text("label-sinan")
    sea = svc.get_exhibit_text("label-sea")
    log("勘误后状态", {
        "司南展签": sinan["status"],
        "司南受影响主张": sinan["reopen_history"][-1]["affected_claims"],
        "司南依赖链": sinan["reopen_history"][-1]["affected_chains"],
        "航海展签（不依赖旧断代）": sea["status"],
    })

    # 打回期间强行再发布必须被门禁阻止（旧断代已被勘误）。
    try:
        svc.release_exhibit_text("label-sinan", occurred_at=t("26"))
    except DomainError as exc:
        log("打回期间发布被阻止", str(exc))

    # 5) 编委会复审：新断代须先通过考古学复核，才能“新旧并列加注”继续展出 ---
    try:
        svc.resolve_reopened("label-sinan", "kept_with_annotation",
                             "新旧断代并列", by="编委会", occurred_at=t("26"))
    except DomainError as exc:
        log("加注决议被阻止（新断代尚未经考古学复核）", str(exc))
    svc.record_review("dating-2015", "考古学", "approved",
                      "考古学复核组", note="检测证据成立", occurred_at=t("27"))
    svc.resolve_reopened(
        "label-sinan", "kept_with_annotation",
        "保留1950年代断代叙述但加注：2015年再检测结论不同，断代本身存在争议；"
        "“司南是否为磁性指向器”两说并列。",
        by="编委会", occurred_at=t("27"))
    svc.release_exhibit_text("label-sinan", released_by="策展组", occurred_at=t("27"))
    log("加注并列后重新发布（v2）",
        svc.evidence_graph("label-sinan")["release_no"])

    # 6) 策展人接口：证据图与争议摘要 --------------------------------------
    summary = svc.dispute_summary("label-sinan")
    log("争议摘要（当前版本）", {
        "tier_counts": summary["tier_counts"],
        "冲突对数": len(summary["conflicts"]),
        "加注并列的旧主张": summary["annotated_in_scope"],
        "can_publish": summary["can_publish"],
    })
    log("争议摘要（永久定格的 v1）",
        svc.dispute_summary("label-sinan", release_no=1)["tier_counts"])

    # 7) 研究者历史时点回溯 ------------------------------------------------
    as_of = svc.state(as_of="2026-09-26T00:00:00+08:00")
    log("回溯 2026-09-26 00:00（新断代尚未发生）", {
        "司南展签状态": as_of["exhibits"]["label-sinan"]["status"],
        "在展版本": as_of["exhibits"]["label-sinan"]["current_release"]["release_no"],
        "dating-2015 是否已存在": "dating-2015" in as_of["claims"],
    })

    # 8) 撤销不删除审议；并发冲突检测 --------------------------------------
    before = svc.state()
    review_kept = "review:recon-wang:科技史" in before["reviews"]
    svc.withdraw_claim("recon-wang",
                       "复原方案在重复实验中不能指南，暂不作为定论展出",
                       by="学术委员会", occurred_at=t("28"))
    after = svc.state()
    conflict = None
    try:
        svc.link_evidence("doc-lunheng", "书影", "重印本",
                          expected_version=1, occurred_at=t("28"))
    except VersionConflict as exc:
        conflict = str(exc)
    v1_nodes = {n["id"] for n in svc.evidence_graph("label-sinan", release_no=1)["nodes"]}
    log("撤销与并发", {
        "撤销前审议已存在": review_kept,
        "撤销后主张标记": after["claims"]["recon-wang"]["withdrawn"],
        "撤销原因": after["claims"]["recon-wang"]["withdraw_reason"],
        "复核审议记录仍保留": "review:recon-wang:科技史" in after["reviews"],
        "v1 快照仍含已撤销的复原方案": "recon-wang" in v1_nodes,
        "司南展签因撤销再次送回复审":
            svc.get_exhibit_text("label-sinan")["status"] == "reopened",
        "航海展签不受影响":
            svc.get_exhibit_text("label-sea")["status"] == "published",
        "并发冲突": conflict,
    })

    print("\n".join(out))
    print(f"\n事件日志已写入: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
