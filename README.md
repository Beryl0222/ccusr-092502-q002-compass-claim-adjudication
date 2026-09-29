# 罗盘展陈主张裁决库

科技馆指南针专题展的**可运行后端主张库**。它把古籍摘引、藏品断代、复原方案、
实验记录、学者观点、翻译、展签、教育活动所引用的结论**分开登记**，每条主张标明
证据层级，冲突结论可以并存；未经对应专业复核，编辑不能对外定论。

实现只用 Python 标准库（≥3.11），采用**事件溯源（append-only 事件日志）**：
业务事实一旦接收就不原地改写，勘误、撤销、复审都由后续事件表达，因此可以
重现任意历史时点的状态，并永久保留曾经展出的说法与当时证据。

## 证据层级与主张类型

| 证据层级 `tier` | 含义 |
| --- | --- |
| `HISTORICAL_RECORD` | 史料记载（古籍原文、摘引、翻译只到这一级） |
| `PHYSICAL_EVIDENCE` | 实物证据（出土实物、发掘记录、测年结果） |
| `EXPERIMENTAL_SUPPORT` | 实验支持（复原可行性、重复实验） |
| `DISPUTED` | 仍存争议（不能作为定论展出，必须并陈） |

主张类型 `kind`：`literature_excerpt` 文献摘引、`artifact_dating` 藏品断代、
`reconstruction_plan` 复原方案、`experiment_record` 实验记录、`scholar_opinion` 学者观点、
`translation` 翻译、`exhibit_label` 展签结论、`education_activity` 教育活动。

古籍原文、墓葬实物、后世复原与实验结论是**不同类型的主张 + 不同层级的证据**，
展签不能把它们写成同一级事实。

## 核心规则

1. **专业复核门禁**：每类主张有对应责任专业（文献→philology、断代→archaeology、
   复原/观点→history_of_technology、实验→physics、展签→museology、教育→education）。
   缺少该专业的**有效批准**时，展签发布返回 422，编辑无法代为定论。
2. **冲突并存**：相互 `contradicts` 的主张都可保持 ACTIVE；但发布时必须填写
   `dispute_note`（争议并呈说明），接口返回争议摘要。
3. **勘误只打回真正依赖者**：勘误/重断代会登记一条新主张并把旧主张置为
   SUPERSEDED（不删除），沿 `depends_on / derived_from / translates / dates`
   计算依赖闭包，只把有效引用落入闭包的**已发布**展签置为 REOPENED；
   `supports` 与 `contradicts` 不扩大复审面。打回的展签需要**晚于打回时间**的
   展陈专业重新批准才能再上线。
4. **撤销不删除审议过程**：复核撤销只把记录置为 `active=false` 并附原因；
   展签撤销归档后历史发布版本与全部事件保留。
5. **发布即冻结快照**：每次发布把展签正文、引用主张版本、证据材料、复核人与
   证据图一起冻结，后续勘误不影响旧版本。
6. **并发版本冲突检测**：写请求携带 `expected_version`（聚合当前版本，新建为 0），
   存储层在文件锁内校验，过期写入返回 `409 version_conflict`。
7. **时间旅行**：`GET /state?as_of=...` 用同一条事件日志重放任意历史时点。

## 目录

- `contracts/domain.json` — 聚合、事件、枚举合同。
- `src/envelope.py` — 共用事件信封校验。
- `src/storage.py` — JSONL 追加存储、文件锁、乐观并发控制。
- `src/domain.py` — 归约投影、依赖闭包、发布门禁、证据图、争议摘要、发布快照。
- `src/service.py` — 应用服务（全部用例）。
- `src/api.py` — 标准库 HTTP 接口。
- `scripts/demo_sinan.py` — 司南专题端到端演示。
- `tests/` — 合同、领域、HTTP 三层测试（28 个）。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests
python3 -m compileall -q src tests scripts

# 司南场景演示（临时事件日志，直接打印全过程）
python3 scripts/demo_sinan.py

# 启动 HTTP 接口
python3 -m src.api --store data/events.jsonl --host 127.0.0.1 --port 8080
```

## HTTP 接口摘要

所有写操作接受 JSON；除新建外建议带 `expected_version` 做并发控制。

| 方法与路径 | 说明 |
| --- | --- |
| `POST /claims` | 登记主张（kind/tier/statement/source_ref） |
| `POST /claims/{id}/evidence` | 关联证据材料（独立分级，不与主张混级） |
| `POST /claims/{id}/relations` | 关联 supports/contradicts/depends_on/derived_from/translates/dates |
| `POST /claims/{id}/supersede` | 勘误/重断代：新建修正主张、旧主张留痕、依赖展签打回 |
| `GET  /claims/{id}/impact` | 勘误前影响面预演（闭包/打回/不受影响） |
| `POST /subjects/{id}/reviews` | 专业复核（主张或展签；APPROVED/CHANGES_REQUESTED/REJECTED） |
| `POST /subjects/{id}/reviews/{rid}/revoke` | 撤销复核（留痕，审议过程不删除） |
| `POST /labels` | 起草展签 |
| `POST /labels/{id}/citations` | 引用主张 |
| `DELETE /labels/{id}/citations/{claim_id}` | 撤回引用（历史保留） |
| `POST /labels/{id}/revise` | 修订展签正文 |
| `POST /labels/{id}/release` | 发布（过门禁；有争议须带 dispute_note），冻结快照 |
| `POST /labels/{id}/archive` | 撤销展签（归档留痕） |
| `GET  /labels/{id}/releases/{v}` | 取某一版冻结快照 |
| `GET  /labels/{id}/report?release_version={v}` | **策展人：证据图 + 争议摘要**（不带版本看草案门禁） |
| `GET  /state?as_of=2026-09-01T09:00:00%2B08:00` | **研究者：任意时点状态重现** |
| `GET  /events` | 全部追加事件（审计流） |

## 事件（append-only）

`CLAIM_REGISTERED` · `CLAIM_SUPERSEDED` · `CLAIM_RELATION_LINKED` ·
`EVIDENCE_LINKED` · `REVIEW_RECORDED` · `REVIEW_REVOKED` ·
`TEXT_DRAFTED` · `TEXT_CLAIM_CITED` · `TEXT_CITATION_WITHDRAWN` ·
`TEXT_REVISED` · `TEXT_RELEASED` · `TEXT_ARCHIVED` · `IMPACT_REOPENED`

事件信封必需 `event_id / event_type / occurred_at(含时区) / aggregate_id /
version(正整数) / payload`，与 `contracts/domain.json` 一致。
