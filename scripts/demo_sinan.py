"""司南专题展端到端演示（不依赖网络，直接走应用服务）。

运行：python3 scripts/demo_sinan.py
它会在临时事件日志上重演：分类型登记主张 → 分级证据 → 专业复核门禁 →
争议并呈发布 → 重断代只打回真正依赖的展签 → 撤销留痕 → 证据图/时间旅行。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.errors import Conflict, GateRejected
from src.service import ClaimService
from src.storage import EventStore

CST = timezone(timedelta(hours=8))


class Ticker:
    def __init__(self, start: str) -> None:
        self.t = datetime.fromisoformat(start)

    def __call__(self) -> str:
        self.t += timedelta(seconds=37)
        return self.t.isoformat()


def main() -> None:
    tmp = tempfile.mkdtemp()
    store = EventStore(os.path.join(tmp, "sinan.jsonl"))
    svc = ClaimService(store, clock=Ticker("2026-09-01T09:00:00+08:00"))

    def say(title: str) -> None:
        print("\n" + "=" * 72)
        print(title)
        print("=" * 72)

    def show(obj, *, max_lines: int | None = None) -> None:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
        if max_lines:
            text = "\n".join(text.splitlines()[:max_lines])
        print(textwrap.indent(text, "  "))

    # 1. 分类型登记主张 -----------------------------------------------------
    say("一、按八个类型分别登记主张，并各自标注证据层级（不混为同一级事实）")

    svc.register_claim(
        claim_id="lunheng", kind="literature_excerpt",
        statement="《论衡·是应》：“司南之杓，投之于地，其柢指南。”",
        tier="HISTORICAL_RECORD", source_ref="《论衡》卷十七·是应",
        registered_by="文献组",
    )
    svc.link_evidence("lunheng", evidence_id="ev-lunheng-text", tier="HISTORICAL_RECORD",
                      kind="古籍原文", citation="《论衡·是应》",
                      summary="传世本异文：杓/酌；其柢/其柄", linked_by="文献组")

    svc.register_claim(
        claim_id="hanfei", kind="literature_excerpt",
        statement="《韩非子·有度》：“先王立司南以端朝夕。”",
        tier="HISTORICAL_RECORD", source_ref="《韩非子·有度》",
    )

    svc.register_claim(
        claim_id="tomb-dating", kind="artifact_dating",
        statement="某东汉墓出土勺形器与式盘共出，初判年代为东汉",
        tier="PHYSICAL_EVIDENCE", source_ref="发掘简报 M12:3",
    )
    svc.link_evidence("tomb-dating", evidence_id="ev-tomb-report", tier="PHYSICAL_EVIDENCE",
                      kind="墓葬发掘记录", citation="M12:3 出土单位",
                      summary="地层与共存陶器类型学断为东汉；未做热释光复测",
                      linked_by="考古组")

    svc.register_claim(
        claim_id="spoon-restored", kind="reconstruction_plan",
        statement="按传世记述以天然磁石琢勺、置于地盘的 1:1 复原方案",
        tier="EXPERIMENTAL_SUPPORT", source_ref="后世复原方案档案 R-1952",
    )
    svc.link_relation("spoon-restored", relation="derived_from",
                      to_claim_id="tomb-dating", note="复原以出土器形为前提")
    svc.link_relation("spoon-restored", relation="depends_on",
                      to_claim_id="lunheng", note="造型与指向方式取自文献训读")

    svc.register_claim(
        claim_id="friction-exp", kind="experiment_record",
        statement="复刻磁勺在打磨地盘上可自由转动并稳定指向南方",
        tier="EXPERIMENTAL_SUPPORT", source_ref="实验报告 EXP-07",
    )
    svc.link_evidence("friction-exp", evidence_id="ev-exp-data", tier="EXPERIMENTAL_SUPPORT",
                      kind="重复性实验", citation="EXP-07",
                      summary="n=20 次，15 次稳定指南；表面摩擦与磁矩为关键条件",
                      linked_by="物理组")
    svc.link_relation("friction-exp", relation="supports", to_claim_id="spoon-restored")

    svc.register_claim(
        claim_id="view-magnetic", kind="scholar_opinion",
        statement="传统说：汉代“司南”即天然磁石制成的磁性指向器",
        tier="DISPUTED", source_ref="磁勺说论著辑要",
    )
    svc.link_relation("view-magnetic", relation="supports", to_claim_id="lunheng")

    svc.register_claim(
        claim_id="view-skeptic", kind="scholar_opinion",
        statement="质疑说：“司南”或指指南车/权柄象征，无磁勺实物出土，可行性条件苛刻",
        tier="DISPUTED", source_ref="相关商榷论文辑要",
    )
    svc.link_relation("view-skeptic", relation="contradicts", to_claim_id="view-magnetic",
                      note="磁性说与非磁性说长期并存")
    svc.link_relation("view-skeptic", relation="contradicts", to_claim_id="lunheng",
                      note="对文献训读结论的质疑，不否定原文存在")

    svc.register_claim(
        claim_id="tr-en", kind="translation",
        statement="英译文：“the south-controlling spoon”——仅译文字，不证磁性",
        tier="HISTORICAL_RECORD", source_ref="展馆英文展签译稿",
    )
    svc.link_relation("tr-en", relation="translates", to_claim_id="lunheng")

    svc.register_claim(
        claim_id="edu-make", kind="education_activity",
        statement="教育活动“做一个小司南”所引用的结论：磁化缝衣针穿浮草可指南",
        tier="EXPERIMENTAL_SUPPORT", source_ref="教育活动方案 EDU-03",
    )

    print(f"  已登记主张 {len(svc.list_claims())} 条，事件全部追加保存，不原地改写。")

    # 2. 专业复核 -----------------------------------------------------------
    say("二、各主张取得对应专业复核（编辑不能代替任何专业下定论）")
    approvals = [
        ("lunheng", "philology", "文献学专家"),
        ("hanfei", "philology", "文献学专家"),
        ("tomb-dating", "archaeology", "考古学专家"),
        ("spoon-restored", "history_of_technology", "技术史专家"),
        ("friction-exp", "physics", "物理学专家"),
        ("view-magnetic", "history_of_technology", "技术史专家"),
        ("view-skeptic", "history_of_technology", "技术史专家"),
        ("tr-en", "philology", "文献学专家"),
        ("edu-make", "education", "教育专业负责人"),
    ]
    for cid, disc, reviewer in approvals:
        svc.record_review(cid, discipline=disc, outcome="APPROVED",
                          reviewer=reviewer, note="资料与结论分级登记，同意入库")
    print("  9 条主张分别由文献/考古/技术史/物理/教育专业批准；")
    print("  批准只针对“能否这样登记”，相互冲突的观点仍然并存。")

    # 3. 起草展签并撞门禁 ---------------------------------------------------
    say("三、起草《司南》展签：先被门禁拦截，补齐复核与争议并呈后才发布")
    svc.draft_label(label_id="label-sinan", title="司南：汉代的指南之器？",
                    body="（草案）司南是指南针的前身……", author="策展人")
    for cid, how in [
        ("lunheng", "作为文献记载层级证据展出"),
        ("tomb-dating", "作为实物证据层级展出"),
        ("spoon-restored", "复原方案，明确标注为后世复原"),
        ("friction-exp", "实验支持，注明成功率与条件"),
        ("view-magnetic", "磁性说一方"),
        ("view-skeptic", "质疑说一方，与磁性说并陈"),
    ]:
        svc.cite_claim("label-sinan", claim_id=cid, how_used=how)

    try:
        svc.release_label("label-sinan")
    except GateRejected as exc:
        print("  首次发布被 422 门禁拦截：")
        for err in exc.errors:
            print(f"   - {err}")

    svc.record_review("label-sinan", discipline="museology", outcome="APPROVED",
                      reviewer="展陈专家组", note="同意两说并陈的表述")
    try:
        svc.release_label("label-sinan")
    except GateRejected as exc:
        print("\n  补了展陈复核仍被拦截（争议必须写明并呈说明）：")
        for err in exc.errors:
            print(f"   - {err}")

    svc.release_label(
        "label-sinan",
        dispute_note="司南是否为磁性指向器，学界尚无定论：本展严格区分文献记载、"
                     "实物证据、后世复原与实验结果，磁性说与质疑说并陈，不替学界定论。",
    )
    v1 = svc.get_label_release("label-sinan", 1)
    print(f"\n  已发布第 {v1['release_version']} 版（{v1['released_at']}），"
          f"冻结引用 {len(v1['citations'])} 条主张及其当时证据。")

    # 另一块不依赖断代的纯文献展签
    svc.draft_label(label_id="label-textwall", title="文献墙：先王立司南", author="策展人")
    svc.cite_claim("label-textwall", claim_id="hanfei")
    svc.record_review("label-textwall", discipline="museology", outcome="APPROVED")
    svc.release_label("label-textwall")
    print("  文献墙展签只引《韩非子》，与出土器断代无依赖关系，也已发布。")

    # 4. 新断代/勘误的影响面 -----------------------------------------------
    say("四、热释光复测改断西晋：只有真正依赖旧断代的展签被送回复审")
    preview = svc.impact_preview("tomb-dating")
    print("  影响面预演：")
    print(f"   - 依赖闭包：{preview['dependency_closure']}")
    print(f"   - 打回复审：{[x['label_id'] for x in preview['reopened_labels']]}")
    print(f"   - 不受影响：{[x['label_id'] for x in preview['unaffected_published_labels']]}")

    result = svc.supersede_claim(
        "tomb-dating",
        reason="热释光复测：标本年代改断西晋，原“东汉”断代结论勘误",
        by="考古学专家组",
        statement="勺形器热释光复测年代为西晋；与式盘共存关系需重审",
        tier="PHYSICAL_EVIDENCE",
    )
    new_id = result["new_claim"]["claim_id"]
    svc.record_review(new_id, discipline="archaeology", outcome="APPROVED",
                      reviewer="考古学专家（复测）")
    print(f"\n  旧主张 tomb-dating 标记 SUPERSEDED（不删除），新主张 {new_id} 重新过考古复核。")
    print("  label-sinan 状态：REOPENED；label-textwall 仍 PUBLISHED。")

    # 旧版本快照原样可查
    old_snap = svc.get_label_release("label-sinan", 1)
    old_tier = next(c["tier"] for c in old_snap["citations"]
                    if c["claim_id"] == "tomb-dating")
    print(f"  但已展出的第 1 版快照仍写着当时的证据层级：{old_tier}（历史不可改写）。")

    # 5. 撤销决定不删除审议过程 ---------------------------------------------
    say("五、撤销复核（发现实验数据问题）：决定撤回，审议记录保留")
    rev = next(c for c in svc.list_claims() if c["claim_id"] == "friction-exp")
    review_id = rev["reviews"][0]["review_id"]
    svc.revoke_review("friction-exp", review_id,
                      reason="原始记录复核发现磁矩测点异常，原批准撤销，待重做实验",
                      revoked_by="学术委员会")
    after = svc.get_claim("friction-exp")
    r0 = after["reviews"][0]
    print(f"  复核 {review_id}：active={r0['active']}，撤销原因：{r0['revoke_reason']}")
    print("  审议条目仍在事件流与读模型里，没有物理删除。")

    # 6. 策展人证据图与争议摘要 ---------------------------------------------
    say("六、策展人接口：第 1 版展签的证据图与争议摘要")
    report = svc.label_evidence_report("label-sinan", 1)
    claims_in_graph = [n["id"] for n in report["evidence_graph"]["nodes"]
                       if n["node_type"] == "claim"]
    ev_in_graph = [n["id"] for n in report["evidence_graph"]["nodes"]
                   if n["node_type"] == "evidence"]
    print(f"  图节点：主张 {len(claims_in_graph)} 个，证据材料 {len(ev_in_graph)} 份；")
    print(f"  边类型：{sorted({e['type'] for e in report['evidence_graph']['edges']})}")
    print("  争议摘要：")
    for item in report["disputes"]:
        if item["type"] == "contradiction":
            sides = " vs ".join(s["claim_id"] for s in item["sides"])
            print(f"   - [冲突] {sides}：{item['detail']}")
        else:
            print(f"   - [{item['type']}] {item['claim_id']}：{item['detail']}")

    # 7. 研究者时间旅行 -----------------------------------------------------
    say("七、研究者可重现任意历史时点的状态")
    from datetime import datetime as _dt
    events = store.load()
    first_label_ts = next(
        e["occurred_at"] for e in events
        if e["aggregate_id"] == "label-sinan" and e["event_type"] == "TEXT_DRAFTED"
    )
    before_label = (_dt.fromisoformat(first_label_ts) - timedelta(seconds=1)).isoformat()
    past = svc.state_at(before_label)
    print(f"  {before_label} 时点（展签起草前一秒）：主张 {len(past['claims'])} 条，"
          f"展签 {len(past['labels'])} 个。")
    today = svc.state_at("2026-09-30T00:00:00+08:00")
    print(f"  2026-09-30 时点：旧断代状态 = "
          f"{today['claims']['tomb-dating']['status']}，"
          f"展签状态 = {today['labels']['label-sinan']['status']}。")
    print("  两个时点都由同一条追加日志重放得到，不依赖任何就地更新。")

    # 8. 并发冲突演示 -------------------------------------------------------
    say("八、并发更新冲突检测")
    try:
        # 两个编辑同时基于版本 1 给《韩非子》摘引附异文
        svc.link_evidence("hanfei", tier="HISTORICAL_RECORD", kind="版本异文",
                          summary="甲先到：附上宋本异文", expected_version=1)
        svc.link_evidence("hanfei", tier="HISTORICAL_RECORD", kind="版本异文",
                          summary="乙后到但仍拿旧版本号 → 409", expected_version=1)
    except Conflict as exc:
        print(f"  {exc.message}（expected={exc.expected}, actual={exc.actual}）")
        print("  乙重读最新版本后重试即可，事件日志不会出现交叉版本。")

    say(f"演示完成。事件日志位置：{store.path}")


if __name__ == "__main__":
    main()
