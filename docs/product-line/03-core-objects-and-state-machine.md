# 核心对象与状态机事实基线

- 状态：`implementation-candidate`
- 日期：2026-08-01
- 目标链路：`Intent → Task → Run → Artifact → Review → Approval → Delivery → Experience`

## 1. 原则

这条链路是产品语义，不要求立即创建八张新表。当前数据库对象继续作为事实源；缺失的产品对象先用不可变关联和读模型补齐，避免重建一套平行运行时。

聊天消息是交互记录，不是 Task、Run、Artifact、审批、Credits 或交付状态的权威来源。

## 2. 当前对象映射

| 产品对象 | 当前实现 | 当前状态/能力 | 主要缺口 |
|---|---|---|---|
| Intent | `Task.intent`、`origin_type`、工作台输入和 Deliverable goal/spec | 工作台确认后进入稳定 Task；确认前仍是页面草稿 | 尚未拆成独立 Intent 表，澄清历史主要留在会话 |
| Task | `Task` + `/api/work` | `pending/doing/done/failed`；含 tenant、origin、executor、Group、work statement、确认指纹与幂等字段 | 仍保留一个真实 `agent_id` 作为 Runtime owner；普通 Task 产物未必是正式交付 |
| Run | `AgentRun` 与 Group runtime | 有持久运行、事件、模型路由快照；工作台用 correlation 和 Deliverable 关联聚合 | 直接 Agent chat 的历史 Run 只能在可追溯关联存在时投影 |
| Artifact | Workspace 文件 + `DeliverableArtifactRevision` | 候选/批准/拒绝/被替代，含 hash、版本和路径 | 普通 Task 产物未必登记为 Artifact |
| Review | `DeliverableQualityReview` 与 assignments/evidence | `open/passed/blocked/incomplete/superseded` | 目前主要覆盖图片、视频、PPT |
| Approval | Deliverable approval policy/actions、Tool approvals | 人工批准、请求修改、工具审批 | 业务批准与工具执行批准需要产品上区分 |
| Delivery | `DeliverableRequest.status/current_stage`、聊天结果和下载 | 有输出确认、正式 Artifact，并由 Work index 跨 Agent 聚合 | Task `done` 仍不等于正式交付；这是刻意保留的边界 |
| Experience | `ExperienceEntry` | `draft/published/retired`，可保存 `source_task_id` / `source_deliverable_request_id`，人工发布后 Agent 可检索 | 来源链接已具备，真实发布/权限浏览器回归待完成 |

## 3. 规范对象合同

### 3.1 Intent

表达用户希望完成的业务结果，至少保留：

- `intent_id`；
- 发起用户与租户；
- 来源入口和来源会话；
- 原始文本和附件引用；
- 识别后的 `work_type`；
- 尚未确认的约束和澄清记录；
- 提议执行者与选择原因。

Intent 不是可计费执行；只有确认工作说明后才创建/推进 Task 和 Deliverable。

### 3.2 Task

Task 是已经确认的责任与工作范围，至少关联：

- `intent_id`；
- `responsible_subject_type/id`，允许 assistant、agent、temporary_expert、group；
- 工作合同/模板版本；
- 输入、预期结果、完成标准、审批策略；
- 来源会话和可见范围；
- 当前业务状态。

现有 `Task.agent_id` 保留为真实 Runtime owner；Group 另外固定 `group_id`、session 和有序参与者快照，临时专家固定角色快照。前端不能伪造 Agent ID，服务端会在租户、权限、Group 成员和可执行状态上重新解析。

### 3.3 Run

Run 是一次可执行尝试。每次接受执行时固定：

- 责任主体、Runtime 类型、session；
- Skill/Tool/grant 解析结果；
- 套餐、Credits 预留和审批检查；
- Provider/model 路由快照；
- 输入快照、幂等键和来源 Task；
- 阶段事件、外部副作用和对账状态。

重试创建新 Run 或明确的 stage attempt，不覆盖旧证据。

### 3.4 Artifact

Artifact 是可检查的产物记录，不等于一段聊天文字。至少包含：

- 所属 Task/Deliverable/Run；
- Workspace/storage 路径；
- 类型、MIME、大小、内容 hash；
- revision 与 parent revision；
- 生成阶段、Provider receipt 引用；
- 候选、批准、拒绝、替代状态。

### 3.5 Review 与 Approval

- `Review` 回答“产物是否满足质量和事实合同”。可以包含自动检查与独立人工检查，两类证据不可混为一谈。
- `Approval` 回答“有权的人是否允许继续付费、外部执行、发布或正式交付”。
- 质量通过不自动等于业务批准；业务批准也不能覆盖缺失或损坏的 Artifact。

### 3.6 Delivery

Delivery 是正式交付事件，必须固定：

- 被批准的 Artifact revision；
- 接收者/可见范围；
- 交付渠道和时间；
- 责任 Agent/Group；
- 业务批准 receipt；
- 最终状态与后续修改入口。

“文件已创建”不是 Delivery。只有可访问、可预览/下载且满足所需审批的 Artifact 才可交付。

### 3.7 Experience

Experience 是从成功或失败工作中提炼、经人工审核后可复用的知识，不是任务日志复制。它保留来源 Task/Run/Delivery 引用、适用条件、失效信号和审阅人。

## 4. 统一业务状态机

### 4.1 顶层状态

| 状态 | 含义 | 允许的主要动作 |
|---|---|---|
| `capturing` | 正在理解 Intent | 补充约束、取消 |
| `awaiting_confirmation` | 工作说明、费用或执行者待确认 | 确认、修改、取消 |
| `ready` | Task 已持久化且预检通过 | 启动、取消 |
| `running` | 至少一个 Run 正在执行 | 查看进度、在安全点取消 |
| `waiting_review` | 已有候选 Artifact，等待质量检查 | 分配 reviewer、提交检查、修改 |
| `waiting_approval` | 质量合同允许继续，等待业务批准 | 批准、请求修改 |
| `ready_to_deliver` | Artifact revision 和批准均已固定 | 确认交付 |
| `delivered` | 已正式交付 | 下载、创建修订、沉淀经验 |
| `changes_requested` | 指定阶段需要修改 | 创建局部重做 Run |
| `failed` | 当前尝试失败且需要干预 | 安全重试、换路线、取消 |
| `cancelled` | 用户或政策终止 | 只读查看、重新创建 |

### 4.2 对现有状态的映射

| 统一状态 | `Task.status` | `DeliverableRequest.status/current_stage` | Review |
|---|---|---|---|
| `ready` | `pending` | `ready/brief_confirmed` | — |
| `running` | `doing` | `running/*` | — |
| `waiting_review` | — | `running` 或 `waiting_approval/output_review` | `open` |
| `waiting_approval` | — | `waiting_approval/*` | `passed` 或满足策略 |
| `ready_to_deliver` | — | `waiting_approval/output_review` 且全部门禁通过 | `passed` |
| `delivered` | `done` 仅表示任务完成，不足以单独证明交付 | `succeeded/delivered` 语义 | sealed receipt |
| `failed` | `failed` | `failed` | `blocked/incomplete` |

工作台状态是服务端读模型的归一化结果，不能反向覆盖原模型状态。

## 5. 关键转换规则

1. Provider 调用前必须已有稳定 Task/Deliverable、幂等键和 Credits 预留。
2. Provider 明确拒绝且未接受任务时，才允许切换等价路线。
3. Provider 是否接受不确定时进入 reconciliation，不能再次付费生成。
4. 修改请求创建新 revision/stage attempt；只重做失败页、镜头、图层或转换阶段。
5. Artifact hash 变化会使旧 Review `superseded`，不能沿用旧批准。
6. Review reviewer 必须满足租户、活跃身份、独立身份和人数策略；创建者不能充当独立 reviewer。
7. Delivery 必须绑定具体 approved revision；后续修改产生新 revision 和新交付事件。
8. Experience 只能由人工发布；Agent 自动提炼只能产生 draft。

## 6. 必须保留的关联

每个工作项必须可以追踪以下链路：

```text
tenant/user
  ↕
intent origin ── task owner ── run/session
                           ├── credits reservation/ledger
                           ├── provider acceptance/reconciliation
                           └── artifacts/revisions
                                     ├── reviews/evidence
                                     ├── approvals
                                     ├── delivery receipt
                                     └── experience draft/published entry
```

## 7. 当前实现与剩余门禁

1. 已建立租户级 Work read model；前端工作台和 Dashboard 不再逐 Agent 拼接任务状态。
2. 已为 Task additive 增加 tenant、intent、origin、executor、Group、work statement、confirmation 和客户端幂等关联，旧 Agent Task API 保持兼容。
3. Work projection 已把 `Task done` 和 `Deliverable delivered` 分开显示，并提供权威对象深链。
4. 临时专家以 Task/Run 级角色快照实现，不创建可见长期员工；Group Task 使用真实 Group runtime correlation。
5. OKR 进度可选择已完成 Task，或已成功且存在批准 Artifact 的 Deliverable，并保存不可变证据快照；未完成工作和未批准产物被服务端拒绝。
6. 仍需完成整库测试、迁移 smoke、浏览器对象链核验和独立代码/架构评审；这些通过前不能称为本地业务流已证明。

## 8. 完成标准

- 任意入口发起的任务都能追溯到稳定 Intent、Task、Run 和责任主体。
- 产物、检查、批准、交付和经验都有独立事实，不依赖 Agent 自述。
- 刷新、断线、重试和切换页面不会丢失或重复付费执行。
- 工作台只聚合，不复制、重排或越权修改运行时状态。
