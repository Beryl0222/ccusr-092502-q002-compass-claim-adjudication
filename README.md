# 罗盘展陈主张裁决库

科技馆指南针专题展的**可运行后端主张库**。它把策展链条上不同性质的东西分开登记、
分级标注，让"古籍原文、墓葬实物、后世复原、实验是否可行"不再在展签里被写成
同一级别的事实；相互冲突的结论可以并存，但未经对应专业复核，编辑不能对外定论。

## 它解决什么

- **分级而不是混称**：文献摘引、藏品断代、复原方案、实验记录、学者观点、翻译、
  展签、教育活动所引用的结论分开登记；每条主张标注四级证据层级之一：
  `DOCUMENTARY`（史料记载）、`ARTIFACT`（实物证据）、`EXPERIMENTAL`（实验支持）、
  `DISPUTED`（仍存争议）。复原方案与学者观点默认为"仍存争议"，复核通过也不会
  把观点抬成实物证据。
- **冲突可并存，定论有门禁**：互相冲突的主张（如司南"磁性说／非磁性说"）同时在库；
  展签发布前，其引用闭包内**每一条**主张都必须通过**所属专业**的复核
  （考古学的断代不能由科技史代签）。
- **勘误只打回真正受影响者**：新断代或撤销沿展签发布时的引用闭包定位影响面，
  只有真正依赖旧主张的**在展**展签被送回复审，并给出受影响主张与依赖链；
  草稿、已撤展、仅共享底层材料的展签不受影响。
- **历史不可擦写**：业务事实只增不改。更正表现为 `CLAIM_SUPERSEDED`，撤销表现为
  `CLAIM_WITHDRAWN`；撤销不删除复核审议，每次发布留存完整证据快照。
  曾经怎么说、当时依据什么，永久可查。
- **并发可检测**：每个聚合有独立版本号，更新须带基准版本；过期提交返回
  `409 Version Conflict` 与当前版本。进程内用锁、多进程用文件锁。
- **任意时点可重现**：所有状态由只增事件流重放，研究者可重放任意历史时点。

## 目录

- `contracts/domain.json` —— 聚合、事件、来源类型、证据层级与基础信封合同。
- `src/envelope.py` —— 事件信封字段与时间格式校验。
- `src/store.py` —— 只增 JSONL 事件日志、乐观并发、时点过滤。
- `src/service.py` —— 领域规则：登记、复核门禁、勘误影响面、发布快照、
  证据图、争议摘要、时点重放。
- `src/api.py` —— 标准库 HTTP 接口。
- `scripts/sinan_scenario.py` —— 司南争议端到端情景（固定业务时间，可复跑）。
- `tests/` —— 合同、存储并发、领域规则、HTTP 接口测试。

## 运行

只需 Python 3.11+ 标准库：

```bash
python3 -m unittest discover -s tests          # 全部测试
python3 -m compileall -q src tests scripts     # 编译检查
python3 scripts/sinan_scenario.py              # 司南端到端情景（临时日志）
python3 -m src.api --store data/events.jsonl   # 启动 HTTP 服务（默认 127.0.0.1:8080）
```

## 接口

写接口可在 JSON 体里带 `expected_version`（乐观锁）与 `occurred_at`（重放/迁移用）。

| 方法与路径 | 作用 |
| --- | --- |
| `POST /claims` | 登记主张。`kind` ∈ document_quote / artifact_dating / reconstruction / experiment / scholar_opinion / translation；`tier` 可显式指定；`depends_on` / `conflicts_with` / `supersedes` 登记关系 |
| `POST /claims/{id}/evidence` | 挂接证据材料（书页、出土档案、实验数据表等） |
| `POST /claims/{id}/reviews` | 专业复核：`discipline` 必须等于主张所属专业；`decision` ∈ approved / changes_requested / rejected / revoked，多次决议只增 |
| `POST /claims/{id}/withdraw` | 撤销主张（须给理由）；自动打回依赖它的在展展签，审议记录保留 |
| `POST /exhibits` | 建展签草稿，给出直接引用的主张 `refs` |
| `POST /exhibits/{id}/revise` | 改定引用；已发布版本原样保留，展签回到草稿 |
| `POST /exhibits/{id}/release` | 发布门禁；通过后留存完整快照，返回 `release_no` |
| `POST /exhibits/{id}/reopen` | 手动送回复审 |
| `POST /exhibits/{id}/resolve` | 复审决议：`revise`（随后改定）或 `kept_with_annotation`（新旧并列加注；取代性新主张须已通过对应专业复核） |
| `POST /exhibits/{id}/withdraw` | 撤展，历史版本保留 |
| `GET /exhibits/{id}/evidence-graph?release_no=&as_of=` | 证据图：节点（含层级、专业、复核、证据材料）、依赖边、冲突对、层级计数 |
| `GET /exhibits/{id}/dispute-summary?release_no=&as_of=` | 争议摘要：冲突对、存争议主张、未通过复核项、能否发布与阻断原因 |
| `POST /activities` / `GET /activities/{id}/readiness` | 教育活动登记及其对外就绪检查（同一套门禁） |
| `GET /state?as_of=...` | 重放任意历史时点的完整状态 |

`release_no` 取该次发布的定格快照（即使主张后来被勘误或撤销）；不传则取当前状态，
`as_of` 取该时点的最新发布。

### 错误约定

- `400` 请求结构问题；`404` 聚合不存在；
- `422` 业务规则拒绝（跨专业复核、门禁未过、状态不符等）；
- `409` 版本冲突，响应含 `aggregate_id / expected_version / current_version`，
  调用方重读后用新版本重试。

## 数据模型要点

- 事件信封：`event_id, event_type, occurred_at（含时区）, aggregate_id, version, payload`。
  `version` 是**该聚合自身**的递增序号；复核写在 `review:{claim}:{discipline}` 通道上，
  多次复核只增、最近一次为当前结论。
- 展签聚合状态：`draft → published → reopened → resolved → published`，
  以及终态 `withdrawn`；`releases[]` 保存历次发布快照，`reopen_history[]`、
  `resolution_history[]` 保存全部打回与决议过程。
- 发布快照含当时引用闭包内的主张、复核结论、证据材料与依赖边——
  这是"当时怎么说、当时证据是什么"的凭证。

## 一个最短例子

```bash
python3 -m src.api --store /tmp/demo.jsonl &
curl -s localhost:8080/claims -d '{"kind":"document_quote",
  "statement":"司南之杓，投之于地，其柢指南","discipline":"历史文献学","claim_id":"c1"}'
curl -s localhost:8080/claims/c1/reviews -d '{"discipline":"历史文献学",
  "decision":"approved","reviewer":"文献学复核人"}'
curl -s localhost:8080/exhibits -d '{"label":"司南","refs":["c1"],"exhibit_id":"e1"}'
curl -s localhost:8080/exhibits/e1/release -d '{"released_by":"策展组"}'
curl -s "localhost:8080/exhibits/e1/dispute-summary"
```

## 边界

本服务是领域后端：只增事件日志可直接作为单节点持久化（写入带进程锁与文件锁），
生产部署可把 `EventStore` 换成等价的事务性事件库而不动领域规则；
认证、授权身份与展签排版不在本仓库范围内，`reviewer / by` 字段记录责任人而非鉴权。
