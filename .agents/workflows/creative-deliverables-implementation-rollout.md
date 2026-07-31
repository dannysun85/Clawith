# 图片、视频与 PPT 完整落地和无损演进方案

## 0. 文档状态

- 日期：2026-07-26
- 状态：`local_implementation_and_benchmark_authorized`
- 当前已授权本地火山 Agent Plan 配置、受成本护栏约束的真实 Provider 调用、本地业务流和豆包同题
  Benchmark；仍不授权修改生产配置、生产灰度或发布。
- 当前完成分层：
  - `provider_verified`：Agent Plan 文字、图片、语音；MiniMax 图片、视频、语音；
  - `business_flow_proven`：Agent Plan 文字规划 + Tool Call + Agent Plan 语音持久化交付，
    MiniMax 视频 + 旁白确定性合成，以及正式图片、视频、PPT 的 brief → Runtime →
    Provider route/failover → Artifact candidate 浏览器成功流；三类候选均尚未完成真实多人质量评审
    和创建者批准；
  - `historical_benchmark_complete`：同题图片、人物广告视频和 PPT 的本地/豆包样本与缺陷对照；
    它只是一组回归锚点，不代表开放商业场景整体达标；
  - `evaluation_foundation_local`：动态场景、覆盖统计、独立 holdout commitment、生产 brief
    流式脱敏、Artifact 结构观察、清单/文件名去标识、先封存评分后解盲和 fail-closed 统一评分已本地
    落地；19 条生产候选已完成第一轮显式隐私/信息充分性审核（8 条批准、11 条需补充），但尚未完成
    滚动真实客户样本的正式多人盲评；
  - `blocked_by_provider_entitlement`：当前 Agent Plan Key 的行为级套餐为 Small；1.5 Pro 与
    Seedance 2.0/fast/mini（公开名及官方 Skill 版本化 ID）均在提交前返回 `UnsupportedModel`；
  - `tool_ready`：Seedance 1.5 Pro 的 Medium 路由、官方版本化 ID、4–12 秒/分辨率/比例/联网/
    draft/flex 能力校验、首尾帧和显式音频意图已在本地 adapter 与测试中落地；
  - `skill_ready`：图片 `volcengine-seedream-commercial`、视频
    `volcengine-seedance-commercial`、语音 `commercial-voiceover` 与 provider-neutral PPT
    `commercial-presentation` 已注册并按角色授权；本地 `Douyin Operations Manager`
    workspace 通过受管 seeder 同步这些 Skill；
  - `production_release_verified`：v1.11.9 已发布并核验 release identity、容器健康和生产 smoke；
  - `production_agent_plan_media_verified`：否，生产图片/视频媒体池仍只核验到 MiniMax，
    未获得本轮生产配置变更授权。
- 底座边界：当前 Astra v1.11.9 已吸收上游 Clawith v1.11.3；后续上游升级仍必须按
  `.agents/reference/clawith-v1.11.3-upgrade.md` 的语义合并方式执行，禁止覆盖自有 Deliverable、
  Credits、Approval、Provider 路由和质量门禁。
- 本文是图片、视频、PPT 实施阶段的强制工作流；同时遵守：
  - `.agents/rules/capability-and-agent-governance.md`
  - `.agents/workflows/add-product-capability.md`
  - `.agents/reference/creative-deliverables-capability.md`

## 1. 最终目标与完成标准

目标不是“增加三个模型按钮”，而是在不破坏现有聊天、Agent Runtime、Tool、Credits、Workspace 和审批流程的前提下，建立三个正式交付能力：

1. 图片/海报：可确认 brief、可生成多个候选、可做品牌安全合成、可验收、可局部修改。
2. 视频：可确认脚本/分镜、逐镜头持久生成、可恢复、可局部重做、可合成并交付。
3. PPT：有来源、有故事线、有版式系统、可编辑性合同明确、PPTX/PDF 一致、可按页修改。

只有同时满足以下条件，某项能力才可称为客户可用：

- 产品合同、输入、输出、质量、成本、降级和非目标已固化；
- tenant、Agent、Skill、Tool、entitlement、Provider、Credits、approval 和 idempotency 门禁完整；
- 刷新、断线、worker 重启、重复点击和 Provider 回执丢失后可以恢复；
- 最终 Artifact 有不可变快照、hash、结构检查、质量检查和批准记录；
- 真实 Provider 已验证；
- 真实浏览器完整业务流已验证；
- allowlist/canary 指标达标；
- 目标生产 release、worker、路由、Credits、监控和 Artifact 均已核验。

`代码存在`、`测试通过`、`provider_verified`、`business_flow_proven` 和 `production_verified` 必须分别报告。

## 2. 当前流程事实与不可破坏合同

### 2.1 正式交付请求

当前 `DeliverableRequest` 是 tenant-scoped、creator-scoped 的持久 brief，保存 Agent、direct chat session、workflow/version、spec、tier、审批合同和输出合同；同一请求最多关联一个 `agent_run_id` 和一个 `launch_message_id`（`backend/app/models/deliverable.py:28-143`）。

必须保留：

- `client_request_id + request_fingerprint` 的幂等创建语义；
- server-owned `approval_policy` 和 `output_contract`；
- `expected_version` 乐观并发控制；
- 请求、Agent、session、creator 和 tenant 的精确匹配；
- Artifact 只能由创建者在同 tenant、通过原 Agent 权限下载；
- 已批准或已取消请求不能被 Runtime 重放降级。

当前正式交付只允许：

- 原生 Agent；
- 当前用户自己的 direct chat；
- 同一个 tenant、Agent、session 和 creator；
- 一个确认后的 brief 由一条聊天消息启动。

创建 API 明确拒绝 group session 或不匹配会话（`backend/app/api/deliverables.py:56-73,166-239`）。本期不得顺带开放 Group 或 OpenClaw；它们必须在独立设计中解决多人审批、产物所有权和边缘节点文件一致性。

### 2.2 Workflow 与 preflight

当前 manifests 已有：

- `builtin.presentation.v1`：`agent_runtime`
- `builtin.poster.v1`：`agent_runtime`
- `builtin.video.v1`：`agent_runtime`

`GET /api/deliverables/workflows` 只返回当前 Agent 真正可启动的 workflow。视频必须同时具备
`generate_image_minimax`、`generate_video_minimax`、`generate_speech_minimax` 和
`compose_video_audio`，先生成同画幅首帧，再生成视觉片段和旁白并合成最终 MP4；图片正式工作流
只允许一次 provider-neutral 图片 Tool 调用并把实际 PNG 注册为 Artifact。PPT、图片和视频虽然
都能生成候选 Artifact，仍不得把“候选存在”误报为“质量批准完成”。

现有 preflight：

- 校验 workflow spec；
- 校验文本路由；
- PPT 校验两个转换 Tool；
- 图片/视频校验 entitlement、Agent Tool 和 provider-neutral 平台凭据池；当前运行时按
  Agent Plan → MiniMax 的安全顺序选择，并只在 Provider 接受前切换；
- 返回 Credits 估算；
- `dry_run` 返回不可启动。

必须保留 preflight 的 fail-closed 语义，并扩展为 `available/degraded/unavailable`，不能把健康检查改成仅前端提示。

### 2.3 Chat 与 Agent Runtime

前端发送正式交付时只在原聊天消息中增加 `work_request_id`，普通聊天 payload 保持不变（`frontend/src/pages/agent-detail/AgentDetailPage.tsx:4275-4312`）。

后端在同一事务路径中：

1. 校验 Agent、用户、session、model 和 tenant；
2. 锁定并准备 Deliverable；
3. 验证请求保存的 tier 与本次路由 tier 一致；
4. 用 server-owned prompt 取代自由文本作为执行合同；
5. 创建新的 Agent Run；
6. 将 Run 关联回 Deliverable。

当前明确禁止“带 Deliverable 的聊天消息恢复既有 Run”，并要求 Deliverable 启动新 Run（`backend/app/services/agent_runtime/chat_intake.py:578-607`）。此合同在 v1 路径保持不变。

Direct chat 本身还有单 lane、waiting reply 和 reconnect 约束。新交付工作流不能通过另起后台聊天 Run 绕过 lane；所有恢复都必须通过 Runtime 的 durable command/checkpoint 边界。

当前 direct lane 只会在权威 terminal checkpoint 后释放，`waiting_user/waiting_external` 不释放（`backend/app/services/agent_runtime/scheduling_lane.py:15-67`）。因此，长时间的视频 Provider 轮询或等待 outline/storyboard 审批不能长期占用 foreground chat Run，否则会阻塞用户在同一对话中的普通聊天。这是 v2 必须显式解决的流程风险。

### 2.4 前端恢复

当前前端：

- 进入 Agent direct chat 后按 session 拉取 Deliverable；
- `running` 时每 3 秒轮询；
- 刷新后从后端找最新 `ready + agent_run_id is null` 请求恢复到 composer；
- 只跟踪最新一个已关联 Run 的 Deliverable；
- 发送前把 pending request 移到 inflight，WebSocket 断线时排队并自动重发。

兼容期可启动判断当前允许 `builtin.presentation.v1@1.0.0` 和
`builtin.video.v1@1.0.0`（`frontend/src/utils/deliverables.ts`）。后续不得继续扩散硬编码；
应由服务端返回 `launchable/next_action`，但旧判断在 v1 兼容期必须继续通过。

### 2.5 图片/视频快捷入口

当前图片、视频按钮属于“快速生成”路径：

- 后端能力查询保留兼容期 MiniMax Tool 名称，但按 Agent Plan 与 MiniMax 的健康账号、
  套餐能力、quota circuit、Plan 和 Agent Tool 共同计算可用性；
- 前端只渲染 `available=true` 的按钮；
- 点击按钮只把结构化提示开头插入输入框，不自动发送（`frontend/src/pages/agent-detail/AgentDetailPage.tsx:5483-5515,8267-8296`）；
- 随后仍走普通聊天与现有媒体 Tool。

不可破坏规则：

- v2 正式交付上线前保留现有按钮、Tool 名称和普通聊天行为；
- 第一阶段只把入口文案明确为“快速生成”，不得静默改为付费正式工作流；
- 未通过业务流等价验证前，不删除或重定向 `generate_image_minimax`、`generate_video_minimax`；
- 不改变点击后“不自动发送”的现有交互。

### 2.6 Credits 与异步媒体任务

`MediaGenerationTask` 已保存 tenant、Agent、user、credential、reservation、origin session、provider/model/task id、状态、轮询、输出路径和完成通知（`backend/app/models/media_generation.py:22-124`）。

现有 Credits 已区分：

- `reserved`
- `provider_inflight`
- `settlement_ready`
- `finalized`
- `released/expired`

Provider 完成后先把确切债务写为 `settlement_ready`，再释放结果；结算有 ledger 幂等。`provider_inflight` 默认不能被普通错误路径释放（`backend/app/services/credit_service.py:351-416,427-569,590-622`）。

新工作流必须复用这套账本和 media task 语义，禁止再建一套“Deliverable 自己扣 Credits”的第二账本。

### 2.7 Artifact 与审批

当前 Artifact 只接受本次 request 目录下、来自成功 Agent Tool execution 的输出：
PPT 接受 PPTX/PDF，视频优先接受 `compose_video_audio` 的 MP4，silent 合同才允许
`generate_video_minimax` 的 MP4。系统验证文件签名、大小、路径、hash；视频额外验证时长、
分辨率、画幅、H.264/yuv420p 浏览器兼容、fast-start 和声音合同，并保存不可变快照。
Artifact revision 已支持 candidate/approved/rejected/superseded 和 parent revision
（`backend/app/models/deliverable.py:146-217`）。

2026-07-26 的真实浏览器视频任务
`6e50d404-a0f5-48f8-a4ef-a0a20eab32ca` 暴露并修复了一个生命周期缺口：Runtime 正常停止但
没有 MP4 时，旧通用分支仍会进入 `waiting_approval/output_review`。当前 presentation/video
均必须先完成 Artifact reconciliation；缺少 MP4 会落为
`failed/artifact_verification_failed` 和 `deliverable_artifact_missing`，批准按钮不会出现。

当前检查主要是结构检查，不是内容、视觉、来源和可编辑性检查。

虽然 PPT v1 manifest 声明 `outline/final` 两个批准点，但当前执行只会在 Runtime 终止并完成结构检查后进入 `output_review`；没有真正的 outline pause/resume。因此 v2 的 outline approval 是新增能力，不能误称为修复一个已经完整存在的 UI。

当前 `request_changes` 在 final review 会：

- 拒绝候选 Artifact；
- 把整个请求设为 `failed/changes_requested`；
- 提示用户重新创建 brief。

这不是目标的版本修订闭环。不能直接把原请求重新绑定另一个 Run，因为现有一对一 Run 合同和 Runtime lifecycle 都会产生歧义。

## 3. 目标架构：保留请求，新增执行批次和执行单元

### 3.1 总体原则

采用“扩展，不替换”：

```text
DeliverableRequest（稳定的客户 brief 和合同）
  └─ DeliverableExecution（一次初始执行或一次修订批次）
       └─ DeliverableExecutionUnit（outline/page/candidate/shot/compose 等最小恢复单元）
            ├─ AgentRun / AgentToolExecution（推理与 Tool 幂等事实）
            ├─ MediaGenerationTask（Provider、轮询、Credits 事实）
            └─ DeliverableArtifactRevision（不可变产物事实）
```

职责必须分离：

- Request：用户到底要什么；
- Execution：这一次按哪个合同和版本执行；
- Unit：最小可恢复/可重做的阶段、页面、候选或镜头；
- AgentRun：Agent 的推理和控制过程；
- AgentToolExecution：一次 Tool call 的幂等执行回执；
- MediaGenerationTask：Provider 接受、轮询、结果和 Credits；
- ArtifactRevision：不可变文件和验收证据。

### 3.2 新表 `deliverable_executions`

建议字段：

- `id`, `tenant_id`, `request_id`
- `execution_number`：从 1 递增
- `kind`：`initial | revision | recovery`
- `status`：`ready | running | blocked | reconciling | waiting_approval | succeeded | failed | cancelled`
- `current_stage`
- nullable `intake_run_id`, `coordinator_run_id`, `launch_message_id`（分别在实际创建后写入）
- `workflow_id`, `workflow_version`
- `contract_snapshot`：本次不可变 brief/spec/output/approval/editability snapshot
- `preflight_snapshot`：能力、路由类别、价格版本、估算、风险、确认时间
- `revision_instruction`
- `idempotency_key`, `request_fingerprint`
- `blocked_reason`, `last_error_code`
- `created_at`, `launched_at`, `completed_at`, `updated_at`

约束：

- composite FK `(tenant_id, request_id)`；
- unique `(request_id, execution_number)`；
- partial unique `intake_run_id`（非空时）；
- partial unique `coordinator_run_id`（非空时）；
- unique `launch_message_id`（非空）；
- unique `(request_id, idempotency_key)`；
- 同一 request 最多一个 `ready/running/reconciling/waiting_approval` 的活动 execution；
- execution contract 创建后不可原地修改；修改产生 revision execution。

### 3.3 新表 `deliverable_execution_units`

建议字段：

- `id`, `tenant_id`, `request_id`, `execution_id`
- `stage_key`：如 `outline`, `slide_render`, `image_candidate`, `shot_generate`, `video_compose`
- `unit_key`：如 `deck`, `slide-07`, `candidate-02`, `shot-03`
- `status`：`pending | running | blocked | reconciling | succeeded | failed | cancelled | superseded`
- `dependency_hash`：输入、参考素材、上游 Artifact 和合同的稳定 hash
- `attempt_count`
- `agent_run_id`（可空，指向处理本单元的 background/stage Run）
- `agent_tool_execution_id`（可空）
- `media_generation_task_id`（可空）
- `input_snapshot`, `result_snapshot`, `quality_evaluation`
- `last_error_code`, `next_retry_at`
- `started_at`, `completed_at`, `updated_at`

约束：

- unique `(execution_id, stage_key, unit_key)`；
- 每次外部付费提交必须有稳定的 unit idempotency key；
- `reconciling` 单元禁止新建另一 Provider task；
- dependency hash 未变时复用已成功单元；
- dependency hash 改变时只 supersede 受影响单元和下游依赖。

`DeliverableExecutionUnit` 只负责编排索引，不复制 Provider receipt 或 Credits 状态。

### 3.4 现有表的兼容扩展

`deliverable_requests`：

- 新增 nullable `current_execution_id`；
- 新增 `contract_revision`，默认 1；
- 新增 nullable `latest_preflight`；
- 保留现有 `agent_run_id`、`launch_message_id`，在 v2 中作为“当前/最新执行指针”，不作为历史事实；
- 保留现有顶层 status 枚举，不增加 `blocked/reconciling`，避免旧 API/UI/约束破坏。

顶层映射：

| Execution 状态 | 兼容 Request status | `current_stage` |
|---|---|---|
| ready + 能力不足 | `ready` | `capability_blocked` |
| running | `running` | 具体 stage |
| blocked | `ready` | `capability_blocked` 或具体 blocked stage |
| reconciling | `running` | `provider_reconciling` |
| waiting_approval | `waiting_approval` | `outline_review/composition_review/storyboard_review/output_review` |
| succeeded | `waiting_approval` 或 `succeeded` | `output_review/delivered` |
| failed | `failed` | 具体失败 stage |

`deliverable_artifact_revisions`：

- 新增 nullable `execution_id`, `unit_id`, `stage_key`, `unit_key`；
- 保留 `(request_id, artifact_key, revision_number)` 版本合同；
- `evaluation` 升级为 versioned schema，包含 structural、semantic、visual、safety、source、editability 五类结果；
- 不回写或覆盖已批准快照。

`media_generation_tasks`：

- 新增 nullable `deliverable_execution_id`, `deliverable_unit_id`；
- composite tenant 约束；
- 新 Provider 继续使用同一任务表；
- 现有 quick-generation task 的新字段保持 null。

### 3.5 零停机迁移顺序

1. Expand：先加新表、nullable columns、索引和兼容 ORM；所有 flags 关闭。
2. 双写：v1 仍是权威；v2 allowlist 请求额外写 execution/unit，不改变客户行为。
3. Backfill：只为仍活动的 v1 请求生成 execution 视图；历史已完成请求不强行重写。
4. Read switch：新前端优先读 executions，旧字段仍返回。
5. Canary：仅 allowlist tenant/Agent 创建 v2 workflow。
6. Contract：观察期后再增加更强约束；不删除 v1 字段、不重命名旧 Tool、不回滚数据库表。

旧 worker 与新数据库必须兼容；新 worker 在 flags 关闭时必须完整执行 v1。

v2 Runtime lifecycle 投影必须先通过 `execution.intake_run_id/coordinator_run_id` 或 `unit.agent_run_id` 找到 Execution，不能继续只按 `DeliverableRequest.agent_run_id` 查找。Request 上的 Run 字段只是兼容读模型，Execution/Unit 才保存多批次历史。

## 4. API 兼容方案

### 4.1 保留不变

- `GET /api/deliverables/workflows`：继续只返回真正可启动的 v1 workflows；
- `POST /api/deliverables/preflight`：保留现有字段；
- `POST /api/deliverables/requests`：保留现有 payload 和幂等；
- `GET /requests`, `GET /requests/{id}`；
- `PATCH /requests/{id}`：未启动 brief 的乐观并发编辑；
- `POST /requests/{id}/actions`：v1 行为不变；
- Artifact 下载 URL、鉴权和不可变快照校验；
- WebSocket `work_request_id`。

### 4.2 新增

`GET /api/deliverables/catalog`

- 返回可发现的 PPT/图片/视频能力；
- 每项有 `availability`, `launchable`, `reasons`, `alternatives`, `requires_reconfirm`；
- catalog 可展示 planning-only 或 blocked 能力，但不能冒充 launchable。

`POST /api/deliverables/preflight-v2`

返回：

- `availability: available | degraded | unavailable`
- `launchable`
- `normalized_spec`
- `required_confirmations`
- `estimated_credits: {min,max,pricing_version}`
- `capability_snapshot`
- `allowed_fallback_class`
- `useful_intermediate_outputs`
- `recovery_condition`

`GET /api/deliverables/requests/{id}/executions`

- 返回 execution、units、approval receipts、费用汇总和 Artifact；
- 不返回密钥、原始 Provider token、内部凭据 id 或敏感 prompt。

`POST /api/deliverables/requests/{id}/revisions`

请求：

- `expected_version`
- `client_revision_id`
- `instruction`
- 可选 `target_units`
- 可选更新后的合同字段

行为：

- 锁 request；
- 验证 Artifact/Execution 当前状态；
- 创建 revision execution；
- 只 supersede 依赖发生变化的 units；
- 返回 server-owned `launchable/next_action`；
- 不修改历史 approved/rejected Artifact。

`POST /api/deliverables/requests/{id}/approvals`

- action：`approve | request_changes | cancel`
- stage：`outline | composition | storyboard | final`
- `expected_version`
- `client_action_id`
- 可选 revision instruction

所有 approval 必须生成 durable receipt，重复提交相同 `client_action_id` 返回同一结果；不同 payload 复用 id 返回 409。

### 4.3 Runtime 接入

初始执行继续由用户在 direct chat 发送，保留 `work_request_id`，但 v2 必须把“聊天接单”和“长时间交付执行”拆开：

- foreground intake Run 只负责接单、生成/确认当前阶段的结构化成果、创建 durable execution work，然后尽快 terminal 释放 direct lane；
- 图片/视频 Provider 等待、PPT 批量渲染和后台 QA 由不占 direct lane 的 coordinator/background/stage Run 与 worker 执行；
- 后台结果通过 Deliverable 状态、Artifact card 和持久消息回到原 session；
- 不得让一个 10 分钟视频任务以 `waiting_external` 持有 direct chat lane。

v2 `prepare_deliverable_launch`：

1. 锁 Request 和活动 Execution；
2. 再做一次 live preflight；
3. 若合同、费用或质量发生实质变化，拒绝启动并要求重新确认；
4. 使用 message id 作为启动幂等；
5. 构建 server-owned provider-neutral prompt；
6. 创建 AgentRun；
7. 同一事务写 Execution/Request 当前指针。

阶段审批：

- background coordinator 可以进入 `waiting_user`，但 direct foreground intake Run 必须已经 terminal；
- approval API 验证 Runtime correlation 和 Execution；
- 可安全恢复同一个 background coordinator 时，通过 idempotent Runtime `ResumeRunCommand` 恢复；
- 已终止或合同发生变化时，创建新的 stage/revision Run；
- 前端不能提交任意 `run_id/correlation_id`；
- final 修改创建新的 revision execution，绝不复用 v1 的单 Run 历史。

媒体 Provider 完成：

- media reconciliation worker 更新 `MediaGenerationTask`；
- Deliverable reconciler 根据 task/unit 关联推进 Unit；
- 需要继续 Agent 推理时，发送 durable external-completion command；
- 不依赖浏览器持续打开，也不要求 Agent 在一个 Tool call 内反复轮询。

## 5. Provider 中立能力层

新增内部接口：

```python
class MediaProviderAdapter(Protocol):
    async def submit_image(self, request: ImageRequest) -> ProviderReceipt: ...
    async def submit_video(self, request: VideoRequest) -> ProviderReceipt: ...
    async def inspect(self, receipt: ProviderReceipt) -> ProviderStatus: ...
    async def download(self, receipt: ProviderReceipt) -> ProviderAsset: ...
    async def cancel(self, receipt: ProviderReceipt) -> ProviderCancelResult: ...
```

每个 adapter 必须声明 `CapabilityProfile`：

- modality；
- model/version；
- text-to-image、image edit、多参考、多图输出；
- text/image/reference-to-video、首尾帧、编辑、延长；
- 比例、分辨率、时长、参考图上限、文件限制；
- 同步/异步、轮询、回调、URL 有效期；
- 内容安全和地域限制；
- 价格版本、额度和并发限制；
- rejected、accepted、succeeded、failed、acceptance_unknown 的映射。

路由规则：

- 普通用户不传 provider/model；
- Router 按 capability、tier、健康、价格、合规和 allowlist 选路；
- route decision 必须快照到 Execution/Media task；
- 已提交任务不因路由配置变化而切 Provider；
- `acceptance_unknown` 必须 reconciling；
- 只有提交前不可用或明确未受理时才允许等价 fallback；
- 非等价 fallback 必须重新确认。

兼容策略：

- `generate_image_minimax`、`generate_video_minimax` 保留；
- 现有 Tool 作为 quick-generation compatibility adapter；
- 正式 Deliverable 使用 provider-neutral internal capability Tool；
- 火山 adapter 先 shadow，不加入默认路由；
- 未完成 A/B 和真实 API 验证前，不因已购买 Agent Plan 自动提高套餐承诺。
- 自动 QA 重试和候选生成必须有 server-owned 次数、Credits 和总成本上限；达到上限后进入人工选择/重新确认，禁止“为了过分数”无限付费重跑。

### 5.1 故障决策矩阵

| 发生点 | 权威状态 | Credits | 是否 fallback/retry | 用户状态 |
|---|---|---|---|---|
| preflight 无 Tool/Plan/健康 Provider | 无 Provider task | 不预留 | 不提交；可提供 degraded 方案 | `ready/capability_blocked` |
| Credits 不足 | 无 Provider task | 预留失败 | 不提交 | `ready/credits_required` |
| Provider 明确拒绝且确认未受理 | task `rejected` | 安全释放 | 等价 route 可自动；非等价需确认 | `running` 或 `ready/degraded_confirmation` |
| 提交超时、是否受理未知 | `acceptance_unknown/reconciling` | 保持 `provider_inflight` | 禁止第二次提交 | `running/provider_reconciling` |
| Provider 已完成、下载失败 | task 保留 succeeded receipt | 进入/保持 provider debt | 只重试下载 | `running/asset_recovery` |
| 文件已下载、Artifact commit 失败 | media task/result 保留 | 不释放已发生债务 | 只重试 snapshot/commit | `running/artifact_recovery` |
| Artifact 结构失败 | Unit failed，候选保留 | 已发生费用正常结算 | 依据失败类型重做最小 Unit | `failed` 或可修订 |
| QA 未达标 | Unit/Artifact candidate 不批准 | 已发生费用正常结算 | 按 policy 局部重做，需受预算限制 | `waiting_approval/quality_review` |
| worker 崩溃 | 读 task/unit/checkpoint | 不改变账本 | lease 到期后恢复 | 保持原状态 |
| 提交前取消 | Unit cancelled | 释放 `reserved` | 不提交 | `cancelled` |
| Provider accepted 后取消 | task 继续 reconciliation | 不释放 provider debt | 不再启动下游，回收结果 | `cancelled/provider_settlement_pending` |

## 6. 图片/海报实施流水线

### 6.1 合同

`CreativeBrief` 至少包含：

- 用途、渠道、受众、比例、尺寸；
- 核心主题和 CTA；
- exact copy；
- 商品、人物、Logo、品牌色和字体；
- exact asset 与 creative reference 分类；
- 允许重绘范围；
- 禁止项和内容安全；
- 候选数、交付格式和透明背景；
- 质量/时效/费用档位。

### 6.2 阶段

1. `brief_compile`：从自然语言和附件生成结构化 brief。
2. `reference_inventory`：保存 hash、用途、授权和冻结规则。
3. `composition_plan`：线框、主体区域、safe area、文字层和导出尺寸。
4. `composition_review`：付费生成前确认高风险 exact-copy/商品约束。
5. `prompt_compile`：按 Provider 编译 prompt/negative constraints。
6. `candidate_generate`：每个候选独立 Unit/Media task。
7. `candidate_qa`：可解码、尺寸、比例、主体、伪影、安全。
8. `deterministic_compose`：Logo、商品冻结层、exact copy、裁切和色彩。
9. `final_qa`：OCR、文案、safe area、品牌、导出和视觉评分。
10. `selection`：保存 SelectionReceipt，不默认把第一张当最终图。
11. `output_review`：预览、批准或按 layer/candidate 局部修改。
12. `archive`：PNG/JPEG/WebP、source manifest、评价和成本。

### 6.3 局部修改

- 改文案：只重跑 deterministic composition 和 final QA；
- 改 Logo/商品位置：只重跑 layout/composition；
- 换背景风格：重跑相关 candidate，不动 exact assets；
- 主体错误：重跑失败 candidate；
- 比例变化：若安全裁切可满足，只重排；否则新增候选。

### 6.4 无生成能力

仍保存：

- brief；
- composition wireframe；
- exact copy；
- reference pack；
- prompt pack；
- deterministic brand layers。

Request 映射为 `ready/capability_blocked`，不得产生“最终图片”Artifact。

## 7. 视频实施流水线

### 7.1 合同

`VideoBrief` 至少包含：

- 渠道、比例、总时长、语言、受众；
- 故事、开头 hook、CTA；
- 商品/人物/场景一致性；
- 参考图、首尾帧、多模态参考；
- 镜头数、单镜头时长和运动；
- 字幕、旁白、音乐、音效；
- 是否允许生成真人/肖像；
- 交付分辨率、编码和文件上限。

### 7.2 阶段

1. `brief_compile`
2. `script`
3. `storyboard`
4. `shot_spec_compile`
5. `storyboard_review`：所有付费视频提交前批准
6. `keyframe_pack`
7. `shot_submit`：每个 shot 一个 Unit 和 Media task
8. `shot_reconcile`
9. `shot_qa`：解码、时长、坏帧、黑帧、主体/商品一致性、运动、安全
10. `shot_retry`：只对失败 shot
11. `edit_compose`：顺序、裁切、转场
12. `caption_voice_music`：确定性字幕和独立音频阶段
13. `package_qa`：MP4、编码、响度、字幕、封面、CTA
14. `output_review`
15. `archive`

### 7.3 计费和恢复

- 高成本视频默认每镜头一个候选；
- 未达到门槛或用户明确要求时才局部重做；
- 每个 shot 有独立 Credits reservation；
- Provider accepted 后任何网络错误不得提交第二次；
- 已成功 shot 的 dependency hash 未变时永久复用；
- 合成失败只重跑合成；
- 字幕错误只重跑字幕层；
- 取消新任务不能释放已产生 Provider 债务的 reservation。

### 7.4 无生成能力

仍交付：

- script；
- storyboard；
- shot list；
- keyframe specification；
- subtitle/voiceover script；
- asset list；
- edit decision list。

不得生成空壳 MP4 或把 storyboard 称为最终视频。

## 8. PPT 实施流水线

### 8.1 先解决当前合同漂移

当前 `render_html_to_pptx()` 实际默认 `render_mode="visual"`，即每页截图，高保真但不可直接编辑（`backend/app/services/document_conversion/pptx_renderer.py:26-37,493-540`）；editable 路径会把浏览器布局映射成 shapes/text/images，但复杂 CSS 存在差异（同文件 `401-491,542-580`）。

实施要求：

- 不修改全局 Tool 默认值，避免破坏现有普通 Tool 调用；
- `builtin.presentation.v2` 必须显式传 `render_mode`；
- 在 brief 中保存 `editability_contract`：
  - `editable`：结构、图表和文字可编辑优先；
  - `visual_fidelity`：允许整页栅格化，必须明确提示；
  - `hybrid`：文字/数据/图表可编辑，复杂装饰视觉可栅格化；
- 默认产品合同采用 `editable`；只有用户选择高保真视觉且确认部分元素可能栅格化时，才使用 `hybrid` 或 `visual_fidelity`；
- Artifact evaluation 明确记录每页 editable/rasterized 比例。

### 8.2 结构化 schema

新增：

- `PresentationBrief`
- `SourceInventory`
- `DeckOutline`
- `SlideSpec`
- `ThemeSpec`
- `LayoutSpec`
- `CitationRef`
- `PresentationEvaluation`

每个 `SlideSpec` 至少包含：

- `slide_id`, `purpose`, `headline`
- `slide_type`
- supporting points
- data/chart/table spec
- source refs
- visual intent
- speaker notes
- editability requirement
- layout/template id

### 8.3 阶段

1. `source_inventory`：解析附件，记录 hash、来源和不可确认事实。
2. `brief_compile`
3. `outline`
4. `outline_review`
5. `slide_spec`
6. `theme_compile`
7. `asset_plan`：只为装饰或场景视觉生成图片。
8. `slide_render`：每页独立 Unit。
9. `deck_assemble`
10. `pptx_render`：显式 render mode。
11. `pdf_render`
12. `structural_qa`
13. `semantic_qa`
14. `visual_qa`
15. `pptx_pdf_parity`
16. `output_review`
17. `archive`

### 8.4 必须检查

- PPTX/PDF 均可打开；
- 页数、标题、语言和顺序；
- 文本 overflow、元素越界、遮挡、空白页；
- 最小字号、对齐、留白、对比度；
- 字体可用和替换；
- 图片分辨率和版权/来源；
- 数据、事实、引文和图表来源；
- PPTX/PDF 视觉一致；
- editable/rasterized 合同；
- 逐页信息密度和重复版式；
- Artifact hash、size、snapshot。

当前 fallback renderer 在内容超出页面时可能直接停止后续 flow element（`backend/app/services/document_conversion/pptx_renderer.py:551-580`），因此 overflow 必须在渲染前后作为显式失败，不能只依赖生成文件存在。

### 8.5 按页修订

- revision 指令解析为受影响 slide ids；
- 用户明确限定了页面、元素、文件或字段时，该范围是硬边界。QA 如果发现范围外问题，
  本次修订必须保留失败回执并请求用户授权扩大范围；不得为了通过质量门禁擅自修改范围外
  的文案、字号、布局、素材或事实；
- 重新计算 dependency hash；
- 只 supersede 受影响页及 deck assembly/PPTX/PDF；
- 来源、主题未变时复用其他页；
- 每页 Artifact/preview 和整 deck Artifact 都有 revision lineage；
- final approve 前重新跑整 deck parity 和引用完整性。

### 8.6 没有图片生成

使用 shapes、typography、charts、tables、icons 和已授权 workspace assets 继续完成专业 PPT。没有合适图片不属于失败；破图占位、虚构事实和未声明栅格化才是失败。

## 9. Agent、Skill、Tool 和员工使用方式

### 9.1 谁能提出任务

所有对目标 Agent 有访问权、且 tenant/Plan 允许的用户都可以提出任务。普通用户不选择 Provider、模型或 Tool。

### 9.2 谁能执行

执行必须依次通过：

`tenant → Agent active/access → Skill resolution → Tool visible/granted → entitlement/tier → provider capability/health → Credits → autonomy/approval → durable state/idempotency`

任何失败返回结构化原因和恢复动作，不能通过换 Skill、换 Agent 或直接调用 Provider 绕过。

### 9.3 本期 Agent 策略

- 先让现有合适 Agent 通过 task-scoped expert 执行；
- 不因新增 Tool 立即创建三个持久员工；
- 能力达到 `business_flow_proven` 后，才允许 AgentTemplate 对客户承诺；
- 只有需要长期品牌记忆、定时内容生产、渠道身份或持续 KPI 时，才增加“视觉设计/视频制作/PPT 顾问”等持久 Agent 员工；
- AgentTemplate 的 `default_skills` 和 `default_tools` 分开最小授权；
- Provider/Tool/Skill 大版本变化后重新认证。

## 10. 前端产品流程

### 10.1 统一入口

保留聊天为主入口：

```text
用户说目标或点“制作”
  → 系统识别图片/视频/PPT
  → 打开对应 brief
  → 只询问缺失且会改变合同的信息
  → 展示交付物、预计费用、审批点和可用性
  → 保存 brief
  → 用户发送确认消息启动
  → 阶段时间线
  → 审批/局部修改
  → Workspace 归档
```

不在普通 UI 展示 Skill、Tool、Provider、模型或凭据。

### 10.2 兼容演进

1. 旧 PPT 按钮和 v1 drawer 保留。
2. 新 catalog/workbench 只对 allowlist 显示。
3. 图片/视频 quick buttons 增加“快速生成”说明，但行为不变。
4. v2 达到等价后，再把正式“图片/视频/PPT”入口合并为一个“制作”入口。
5. 旧入口至少保留一个完整观察期。

### 10.3 UI 必须展示的事实

- 当前状态和 stage；
- available/degraded/unavailable；
- 预计 Credits 范围和是否需要重新确认；
- 已批准 brief；
- 中间成果；
- Provider 提交未知时的 reconciling，而不是“失败，请重试”；
- 每个候选/镜头/页面的质量状态；
- 可局部修改范围；
- Artifact 类型、revision、可编辑性和批准状态；
- 用户可执行的下一步。

### 10.4 前端状态来源

- 后端是唯一事实；
- React Query cache 只做显示；
- pending/inflight 乐观状态必须可由刷新恢复；
- 轮询条件扩展为 active execution，而不是只看 request `running`；
- WebSocket 事件用于加速，列表/详情 API 用于对账；
- 不依赖浏览器内存保存 approval、cost、provider receipt 或 Artifact。

## 11. Feature flags、灰度和回滚

建议配置：

- `DELIVERABLE_V2_ENABLED=false`
- `DELIVERABLE_V2_TENANT_IDS=""`
- `DELIVERABLE_V2_AGENT_IDS=""`
- `DELIVERABLE_PRESENTATION_V2_ENABLED=false`
- `DELIVERABLE_IMAGE_V2_ENABLED=false`
- `DELIVERABLE_VIDEO_V2_ENABLED=false`
- `DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED=false`
- `DELIVERABLE_CREATIVE_QUALITY_GATE_TENANT_IDS=""`
- `DELIVERABLE_CREATIVE_QUALITY_GATE_AGENT_IDS=""`
- `MEDIA_PROVIDER_VOLCENGINE_ENABLED=false`
- `MEDIA_PROVIDER_VOLCENGINE_SHADOW_ENABLED=false`

优先级：

`现有活动 execution 的持久 contract > tenant/Agent allowlist > modality flag > global flag`

回滚规则：

- 关闭 flag 只阻止新 execution，不中断已被 Provider 接受的任务；
- 进行中的 execution 按已快照 workflow/provider/pricing 继续或安全 reconciling；
- Provider 紧急禁用后不接新任务，但 reconciler 仍轮询已受理任务；
- v2 UI 隐藏后，旧 PPT 和 quick media 路径仍可用；
- 不通过数据库 downgrade 回滚；
- 不删除新表或已生成 Artifact；
- 回滚后仍能通过详情 API查看/下载历史 v2 Artifact。

## 12. 可观测性、审计和隐私

统一 correlation：

- `request_id`
- `execution_id`
- `unit_id`
- `agent_run_id`
- `tool_execution_id`
- `media_task_id`
- `provider_task_id`
- `reservation_id`
- `artifact_revision_id`

指标：

- preflight availability/reason；
- brief→launch 转化；
- 各 stage 时长/失败/重试；
- Provider accepted/unknown/failed；
- reconciling 数量和年龄；
- Credits reserved/settlement_ready/finalized/released；
- 首轮可用率；
- 每个可用 Artifact 成本；
- 人工修改次数；
- 局部重做率；
- Artifact QA 失败；
- blocked 恢复成功率；
- 重复 Provider submit 和重复扣费。

告警：

- `acceptance_unknown` 超过阈值；
- `settlement_ready` 长时间未 finalize；
- Provider succeeded 但 Artifact 未 commit；
- active execution 长时间无进展；
- output review Artifact hash 变化；
- tenant scope mismatch；
- worker/reconciler backlog；
- Provider quota/套餐临界。

日志不得包含：

- API Key、Authorization header；
- 原始客户文件内容；
- 未脱敏 prompt/人物信息；
- 可长期访问的下载 URL；
- 完整 Provider response 中的敏感字段。

### 12.1 容量、租约和背压

- manifest 为页数、候选数、镜头数、时长、分辨率和单文件大小设硬上限；
- tenant、Provider、modality 分别设置并发和速率，不接受前端覆盖；
- Unit、Media task、reconciliation worker 使用租约和稳定领取顺序，lease 到期才允许其他 worker 接管；
- Provider 已接受的任务即使新任务 flag 已关闭，也必须保留 reconciliation 容量；
- backlog 超阈值时 preflight 返回 degraded/预计等待，不得继续无限接单；
- Provider 轮询使用退避和 jitter，不把短暂限流标为最终失败；
- large video/PPT assembly 设独立 worker pool，避免占满普通聊天和轻量 Tool worker；
- 容量压测至少覆盖同 tenant 并发、跨 tenant 公平性、Provider 限流和 worker 重启。

### 12.2 数据保留、来源和合规

- Provider 临时 URL 不是系统事实；结果必须下载到受控私有存储并生成 immutable snapshot；
- approved final、source manifest、approval 和费用审计按 tenant 正式保留策略保存；
- 未选候选、失败镜头和中间 render 使用单独、可配置的短期保留期，过期删除不得影响已批准 Artifact；
- A/B fixture 必须已授权并匿名化，访问限于评测成员，不直接复制生产客户目录；
- reference pack 保存来源、授权范围和 hash；真人/肖像、商标、版权素材缺少授权时必须 blocked；
- Provider 数据留存、训练使用、区域和内容标识不满足客户合同的 route 不得被选择；
- 删除 tenant/用户数据时，Execution、Media task、Artifact 和 benchmark 副本必须进入同一可审计删除流程。

## 13. 测试和验收矩阵

### 13.1 Schema/unit

- manifest/spec unknown field fail-closed；
- contract fingerprint 稳定；
- route/profile/pricing version；
- dependency hash；
- status projection；
- revision impact graph；
- Artifact validators；
- quality evaluator；
- Provider error mapping。

### 13.2 权限和安全

- cross-tenant request/execution/unit/artifact 404；
- 非 creator 不能编辑、批准或下载；
- Agent access revoked；
- direct/group/OpenClaw 边界；
- Tool disabled/assignment override；
- Plan denied/tier drift；
- 路径穿越、外部 URL、超大文件、损坏文件；
- approval replay/version conflict；
- client idempotency payload drift。

### 13.3 Credits/Provider

- reserve 失败时零 Provider submit；
- 明确 rejected 可安全 release/fallback；
- acceptance_unknown 不重复 submit；
- settlement_ready 不释放；
- finalize exactly once；
- refund exactly once；
- URL 过期重取；
- Provider succeeded、下载失败、Artifact commit 失败的恢复；
- cancellation 与已发生债务。

### 13.4 Runtime/recovery

- WebSocket 断线自动发送一次；
- 同一 `work_request_id` 重放；
- direct lane 竞争；
- worker 在 submit 前/后、download 前/后、Artifact commit 前/后崩溃；
- Runtime checkpoint 重放不降级批准请求；
- approval resume 幂等；
- final revision 新 Run；
- active v2 execution 在 flags 关闭后仍恢复。

### 13.5 Artifact

- immutable snapshot hash；
- workspace path scope；
- revision number 并发；
- parent lineage；
- candidate→approved/rejected/superseded；
- PPTX/PDF/PNG/MP4 结构；
- 页面/镜头/候选与整包 Artifact 对账；
- 下载 inline/attachment 和 MIME。

### 13.6 浏览器业务流

至少验证：

1. 普通聊天不受影响；
2. 普通文件/图片/视频上传不受影响；
3. 现有 quick image/video 点击不自动发送；
4. v1 PPT 仍可创建、发送、运行、刷新恢复、审批和下载；
5. v2 brief 创建与 unavailable 保存；
6. outline/storyboard approval；
7. refresh/reconnect/reopen；
8. 局部修改；
9. Credits 不足；
10. Tool 禁用；
11. Provider unknown/recovery；
12. final approve 与 immutable download；
13. v2 flag 关闭后的旧路径；
14. group/OpenClaw 不出现不受支持入口。

## 14. 质量基准和进入默认路由门槛

### 14.0 开放场景评测合同

这里的 Benchmark 不是固定用户只能做某一种图片、视频或 PPT，也不是为几条 prompt 做定向优化。
评测由四层组成：

1. **历史回归锚点**：少量固定题仅验证已知缺陷是否复发，不参与“全面能力”结论；
2. **动态开放场景**：按 modality、行业、目标、渠道、受众、语言、输入素材、约束、画幅和风格做
   均衡组合；每轮更换 seed，不能把模板或某个商品固化为能力边界；
3. **真实需求滚动样本**：只使用已授权、匿名化的客户 brief，保留原始需求分布和长尾，不把客户数据
   复制到公共 fixture；
4. **隔离留出集**：开发者和 prompt 优化执行者在结果冻结前不能读取题目或 Provider 映射；公开清单只
   保存数量和 SHA-256 commitment。

同一轮 Provider 比较时，必须固定的是用户合同、输入素材 hash、候选/重试预算和评分表；不得固定创意
表达、画面模板、分镜模板或视觉风格。结果必须按场景分桶报告分布、失败率和置信区间，不能只报单一均分，
也不能用手工挑选的最佳候选替代首轮可用率。

provider-free 本地底座：

- `backend/app/services/creative_evaluation.py`：动态场景、覆盖统计、留出 commitment、盲评和统一评分；
- `backend/app/services/creative_sample_ingestion.py`：授权导出输入、HMAC 假名化、敏感信息清理和
  强制人工审核；
- `backend/app/services/creative_artifact_evaluation.py`：图片、视频、PPT 的文件结构和交付合同观察；
- `backend/app/services/creative_blind_review.py`：候选复制、公开清单去标识、评审封存和事后解盲；
- `backend/scripts/generate_creative_evaluation_suite.py`：生成公开 manifest 和独立
  `restricted-holdout.json`，不调用真实 Provider；
- `backend/scripts/anonymize_creative_brief_export.py`：从 JSONL stdin 接收授权导出，避免生产原文落盘；
- `backend/scripts/inspect_creative_artifacts.py`：不调用 Provider 的本地 Artifact 结构检查；
- `backend/scripts/prepare_creative_blind_review.py`：生成公开 review package 和私有 attribution key；
- `backend/scripts/score_creative_blind_review.py`：先写 provider-free sealed score，再按私钥解盲；
- `backend/scripts/audit_creative_benchmark_run.py`：从整轮层面核对 modality 覆盖、公开 Artifact hash、
  provider 去标识、三人模板和正式 panel 结果；provisional 单人结果永远不能形成商用通过；
- `score_creative_blind_review_panel.py` 的正式输出绑定 batch spec、public package 和候选 Artifact
  SHA-256；同时绑定实际 `panel-submissions.json` SHA-256、评审人 receipt 列表和该场景必需的感知
  证据种类。审计器从原始 panel 重新计算评分，拒绝跨批次复制、产物替换、panel 替换或手工篡改
  `commercially_usable` 的旧评分；
- `backend/tests/test_creative_evaluation.py`：验证 seed 可复现、seed 轮换、覆盖、留出隔离、Provider
  去标识、缺失证据不乐观通过和硬门禁 fail-closed。

`prepare_creative_blind_review.py` 只保证公开 JSON 和文件名不含 provider/model/原路径。它不会篡改
候选二进制中的 AIGC metadata、画面水印、PPT 文本/页脚或音频内容，因此公开包必须声明
`masking_scope=manifest_and_filename_only` 和 `embedded_identity_review_required=true`。若样本存在
可见来源标识，正式评审必须使用无标识原始导出重新生成，不能通过模糊、裁切水印或后期修图伪造盲测。

正式评审不得继续复用单份 `BlindCandidateReviewSubmission` 作为放行依据。当前本地 shadow 已增加：

- `creative_review_panel.py`：至少 3 名独立评审、候选全集覆盖、唯一 reviewer receipt、分歧
  fail-closed、Artifact hash 绑定、modality-specific 感知证据，以及明确禁用水印 OCR 命中时
  强制硬门禁失败；
- `collect_creative_ocr_evidence.py`：图片 OCR 和视频逐帧 OCR 私有 receipt；缺少目标语言包时为
  `partial`，不会把“未识别到”写成“无水印”；当前本地已补 `chi_sim`，并增加全图/四角增强、
  稀疏文本识别和 exact/possible 两级禁用词检查；
- `prepare_creative_review_panel.py`：为每种 modality 生成至少 3 份相互隔离、权限为 `0600` 的
  provider-free 空白评审模板；
- `record_creative_human_evidence.py`：把 visual/audio/AV-sync/document 结论绑定到候选 Artifact
  hash；人物同步对白视频可强制要求 `human_av_sync`；
- `assemble_creative_review_panel.py`：拒绝 placeholder receipt、空评分和不足 3 名真实评审的提交，
  只在每名评审完成候选全集后封存 panel；
- `score_creative_blind_review_panel.py`：先封存 panel 结果，再可选解盲 Provider；只有结构、感知、
  独立评审和商业评分全部完成才能输出正式商用候选。

当前三类历史包已各生成 3 份模板，共 9 份；这只证明评审输入与隔离流程已准备好，不表示 9 份真实判断
已经存在。

2026-07-31 使用整轮审计器复核 `blind-review-2026-07-27`：

- 图片 3/3、视频 2/2、PPT 4/4 个公开 Artifact 文件的 SHA-256 与 package receipt 一致；
- 三类各有 3 份 reviewer 模板，完成数均为 0，正式 panel result 均不存在；
- 旧单人/自动评分均被标记为 provisional，不参与 `commercial_ready`；
- 整轮状态为 `awaiting_human_review`，不是 `commercial_ready`，也不是包损坏。

本轮又补齐正式封存的防篡改边界：评审根 receipt、候选判断 receipt 和人工证据 receipt 必须按同一
评审人绑定，人工证据来源只能是独立评审或 Astra 受管身份评审；相同人工证据 receipt 不能跨候选或
跨评审复用。明确包含 `lip sync`、`口型同步` 或同步对白的质量合同会自动要求
`human_av_sync`。`commercial_ready` 在该审计器中只证明所评 Artifact 的质量结论，不证明成本、耗时
或默认路由资格；后者仍需单独的执行/Credits receipt 和路由门槛审计。

本地审批 shadow 已增加 `deliverable_quality_gate.py`，并接入 Artifact approval/read model：

- receipt 必须绑定本次整组 Artifact 的 `artifact_key -> SHA-256`，任一文件替换或 digest 篡改都
  fail closed；
- 只有完整的至少 3 人 blind panel 能签发 `passed`；自动 OCR/结构证据只能签发明确
  `blocked`，不能签发商用通过；
- `blocked`、`incomplete`、无效或 hash 不一致的已附 receipt 始终阻断批准；
- 没有 receipt 时由 `DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED` 加 tenant/Agent 双 allowlist
  共同控制是否强制等待正式评审。开关打开但 allowlist 为空时仍不影响任何客户；配置在本地和三个
  部署 compose 合同中默认 `false + empty`，所以历史客户流程保持兼容；
- API 以 additive `approval_readiness` 暴露状态，前端在阻断时禁用“批准交付”，但保留“退回重做”；
- `build_deliverable_quality_receipt.py` 可把 exact prohibited OCR finding 转成私有、hash-bound
  blocked receipt；possible match 仍只进入人审。

这仍不是生产发布：正式开启 flag 前还必须完成真实独立评审、视频听音/口型条件、评审身份与审批审计、
生产隔离存储/访问控制、receipt 受管写入 API、内部 allowlist 和回滚演练。

本地 holdout 文件权限不是生产隔离。正式评测必须把 holdout 和盲评私钥放在优化执行者无读权限的独立
存储/服务中，评审提交锁定后才能解盲。

### 14.1 图片

- 每个评测周期至少 36 个已授权匿名化真实 brief，并加入同量级动态开放场景；
- `MM-current / MM-optimized / Volc-optimized` 同题；
- 每轮比较固定候选预算、合同参数和 reference hash，不固定用户创意模式；
- 至少 3 人盲评；
- 评分：需求、构图、主体/品牌、文字、伪影、安全、首轮可用、成本。

进入默认 Pro/Ultra 候选条件：

- 相对 `MM-optimized` 首轮可用率提升至少 20 个百分点，或盲评均分提升至少 0.7/5；
- 严重商品/Logo/人物错误率不高于 5%；
- 单位可用图成本在预算；
- 限流、耗尽和失败恢复通过。

### 14.2 视频

- 每个评测周期至少 18 个已授权匿名化真实 brief，并加入同量级动态开放场景；
- 覆盖 T2V、I2V、多参考、商品广告、编辑/延长；
- 实际时长误差不超过 0.2 秒；
- 无连续黑帧超过 200ms；
- Chrome/Safari/ffprobe 可解码；
- 主体/商品、运动、镜头、伪影、故事、品牌、成本盲评；
- 局部重做不重复成功镜头费用。

### 14.3 PPT

- 每个评测周期至少 10 个已授权匿名化真实 brief，并加入中文/英文、8–15 页动态开放场景；
- PPTX/PDF 打开、页数、标题、语言和明确要求 100%；
- 无 overflow、越界、严重遮挡、空白页和缺失字体；
- 重要事实和数字可追溯；
- 至少 80% 页面无需人工结构性排版修复；
- 按页修改不全量重跑；
- editability contract 与实际一致。

### 14.4 全局硬门禁

- cross-tenant 泄漏：0；
- 重复 Provider 提交：0；
- 重复扣费：0；
- 占位/损坏文件被标为 final：0；
- 已批准 Artifact 被 Runtime 重放降级：0；
- 未确认的实质降级自动执行：0。

## 15. 实施阶段、文件范围和停门

### P0 — 合同冻结与现状回归

产出：

- 三类真实匿名化 fixture；
- 动态开放场景 public manifest、独立 holdout commitment 和盲评去标识合同；
- 当前 UI/API/Runtime/Tool/Credits/Artifact 回归测试；
- 当前生产样本的质量分类；
- Provider 调用前的授权清单。

候选范围：

- `backend/tests/fixtures/creative_deliverables/`
- `backend/tests/test_deliverable_*`
- `frontend/src/utils/deliverables.test.ts`
- 浏览器 E2E。

仓库内 provider-free 冻结门禁：

- `cd backend && .venv/bin/python scripts/validate_creative_v1_contracts.py`
- `cd backend && .venv/bin/python scripts/generate_creative_evaluation_suite.py --seed <cycle-seed> --count 24`
- `cd backend && <authorized-export-command> | .venv/bin/python scripts/anonymize_creative_brief_export.py --output <private-pending-review.json> --source-ref <receipt>`
- `cd backend && .venv/bin/python scripts/inspect_creative_artifacts.py --modality <image|video|presentation> ...`
- `cd backend && .venv/bin/python scripts/prepare_creative_blind_review.py --batch-spec <private-spec.json> --output-dir <private-cycle-dir>`
- `cd backend && .venv/bin/python scripts/score_creative_blind_review.py --batch-spec <private-spec.json> --review-package <public/review-package.json> --review-submissions <sealed-submissions.json> --output-dir <results-dir> --private-key <private/review-key.json>`
- 该命令保护 v1 workflow、普通聊天/WebSocket、quick media、brand-safe media、typed Tool receipt 和 Artifact 合同，不调用真实 Provider。
- 动态评测生成命令只生成 provider-neutral 场景和 holdout commitment，也不调用真实 Provider；
  `restricted-holdout.json` 必须按评测环境的独立权限保存，不能提交到仓库或交给优化执行者。
- Agent 模板如果承诺创意能力，还必须通过 `scripts/validate_agent_capabilities.py`；当前仅允许复用已存在的 quick media/brand-safe 合同。

停门：无代表性样本或 v1 回归未锁定，不做 Provider 切换。

### P1 — 数据和编排基础，全部 flags 关闭

实现：

- Execution/Unit 模型和 expand migration；
- v2 schemas/API read model；
- status projection；
- correlation/metrics；
- media task 和 Artifact nullable linkage；
- v1 双写 shadow；
- provider-neutral interfaces，但不接真实火山。

候选范围：

- `backend/app/models/deliverable.py`
- `backend/app/models/media_generation.py`
- `backend/app/schemas/deliverable.py`
- `backend/app/api/deliverables.py`
- `backend/app/services/deliverable_*`
- 新 Alembic migration。

停门：flags 关闭时 v1 的既有 API 字段、状态语义和浏览器行为兼容；新增字段只能是向后兼容的 additive response。

### P2 — PPT v2 shadow 和内部 allowlist

先做 PPT，因为不依赖新付费媒体 Provider：

- SourceInventory/DeckOutline/SlideSpec；
- outline approval/resume；
- theme/layout；
- explicit render mode；
- structural/semantic/visual/parity QA；
- per-slide revision。

2026-07-28 已完成第一段 provider-free 基础：`adaptive-v1` 页级视觉计划、按页数/档位计算的独立素材
预算、版式多样性、单素材复用上限、可编辑信息设计配额，以及 HTML 与 `slide_spec` 的逐页素材对账。
该段保持旧 `slide_spec` 兼容，尚未完成 outline approval、按页 revision、真实开放场景批次或生产灰度。

停门：10 个基准、v1 并行保留、内部浏览器业务流和 Artifact 通过。

### P3 — MiniMax 图片优化 shadow

- CreativeBrief/ReferencePack/PromptCompiler；
- candidate units；
- deterministic composition；
- OCR/主体/品牌/安全 QA；
- SelectionReceipt；
- 不切默认路由。

停门：得到 `MM-optimized - MM-current` 的真实增量。

### P4 — 火山账号/API 资格与 Adapter 验证（本地 Provider 行为已完成，订单信息仍待控制台）

先只读/低成本验证：

- 套餐 SKU、API endpoint、模型白名单；
- Seedream/Seedance 的实际可调用 model id；
- AFP、并发、速率、耗尽和错误；
- 商用、数据保留、内容标识；
- 服务器网络；
- async task/download/cancel；
- error mapping。

任何凭据写入、真实付费批量 A/B 和生产配置变更都需单独确认。

当前本地结果：

- Plan Key 鉴权、文字、Seedream 5.0 Lite、Seed TTS 2.0 已验证；
- 所有当前官方视频路线均在 Provider 接受前拒绝，行为与 Small 套餐一致；
- 本地配置已从误填 Large 收敛为 `small + text/image/audio`；
- 订单级 SKU、有效期、AFP 余量仍需要控制台登录态或火山 AK/SK 管理 API；
- 官方 Seedream/Seedance Skill 已完成 Astra 受管适配并分配给 Douyin Operations Manager。
- Seedance 1.5 Pro 已完成本地协议级兼容接入；当前 Key 仍是行为级 Small，故不能把
  `tool_ready` 写成 `provider_verified`。换成控制台确认的 Medium-or-up Key 后，先做一条
  4 秒 480p T2V 和一条首帧 I2V，再进入正式浏览器成功流与豆包同题 Benchmark。
- 真实 Agent Skill 验证已完成：Seedream 5.0 Lite 生成 9:16 真人广告首帧；Seedance 在 Small
  权益下执行一次后稳定返回 unavailable，没有创建 Provider task 或扣 Credits。媒体附件短路径
  规范化和“本地校验失败也禁止自动重试”的执行边界已补齐。

停门：订单级信息、真实 API receipt、错误语义、Credits 和恢复证据齐全。

### P5 — 图片 A/B、allowlist 和 canary

- 三组同题盲评；
- 火山只作为候选 route；
- tenant/Agent allowlist；
- 保留 MiniMax；
- 观察成功率、成本、人工修改和 recovery。

停门：达到第 14 节门槛并通过产品/财务/安全确认。

### P6 — 视频 storyboard、逐镜头执行和 A/B

- 先完成 provider-independent storyboard/shot pipeline；
- 当 fallback 为 MiniMax 且目标不是 16:9 时，必须先生成并验证同画幅首帧再走 I2V；
- Provider 返回后必须用文件实测画幅、时长、音轨和水印；请求 metadata 不能代替交付验收；
- 当前已加入 `media_video_requires_first_frame_for_aspect_ratio` 付费前停门，直到自动首帧组合链
  完成前，不允许非 16:9 MiniMax T2V 被标记为成功；
- MiniMax optimized shadow；
- 火山 video adapter shadow；
- shot-level Credits/recovery；
- edit/caption/audio/package；
- 内部 allowlist。

停门：18 个基准、零重复提交/扣费、真实浏览器局部重做通过。

### P7 — 统一制作入口和旧路径观察

- 合并为“制作”入口；
- quick generation 明确保留；
- 新旧路径并行一个完整观察期；
- 仅在业务流和指标等价后考虑逐步收口。

停门：旧路径使用量、失败率、客户反馈和回滚演练已评审。

### P8 — Agent 员工认证

- 评估是否需要视觉设计、视频制作、PPT 顾问持久角色；
- 先定义 KPI、渠道、触发器、记忆、autonomy 和升级；
- 最小 Skill/Tool grant；
- capability recertification。

停门：依赖能力未达到 `business_flow_proven`，不得对客户承诺。

### P9 — 受管质量评审和 Artifact 放行

本阶段把离线 panel 文件提升为产品内的受管评审对象，但不改变现有 Deliverable 创建、产物下载、
退回重做和审批状态机：

- 评审批次、评审分配、自动证据和人工判断必须持久化；
- 每个批次绑定完整 Artifact 清单、版本和 SHA-256，文件变化后旧批次自动失效；
- 同一租户内至少 3 名非交付创建者参与，按底层 `Identity` 去重，账号别名不能重复计数；
- 每位评审只能封存一次完整判断；相同 payload 可幂等重放，不同 payload 不可覆盖；
- 图片、视频和 PPT 使用服务端固定的 modality 合同、硬门禁、评分维度和感知证据要求；
- 自动证据只能阻断；没有发现问题不能代替三人盲审签发 `passed`；
- 管理员证据写入必须记录提交人、私有 evidence ref、Artifact hash、findings 和 audit log；
- `blocked`、评审分歧、缺失字段、证据不完整和 hash 变化都 fail closed；
- rollout 仍使用全局开关加 tenant/Agent 双 allowlist，默认关闭且空 allowlist。

2026-07-27 本地实现和验证状态：

- Alembic head 已扩展为 `add_deliverable_quality_reviews`，fresh、历史升级和 downgrade/upgrade
  PostgreSQL smoke 均通过；
- 受管 reviewers/create/latest/get/submit/evidence/read-only artifact download API 已接入；
- 前端交付卡片可配置评审人、显示服务端状态并进入独立评审工作台；
- 隔离临时数据库中的真实浏览器流程完成一条 PPT `0/3 open -> 3/3 passed`，三名不同
  Identity 逐一登录、完整填写并封存；同库另一条视频通过精确
  `prohibited_term_detected=...` 自动证据变为 `0/3 blocked`；
- 应用随后切回原开发数据库，原库评审记录仍为 0；临时数据库和转储已删除；
- 原开发数据只有两名合格非创建者，页面正确显示 `创建评审 (2)` 和 `批准交付` disabled，
  没有为了演示修改真实账号或伪造第三名评审；
- 本阶段没有修改生产配置、没有调用付费图片/视频 Provider，也没有产生模型 Credits。

2026-07-31 主开发租户的后续 QA 状态机验证：

- 新增三名来源明确标记为 `local_quality_gate_qa` 的本地 QA Identity；它们只能证明身份去重、一次性
  封存、receipt、创建者批准和 Artifact 状态转换，不能被称为三名真实独立质量评审人；
- 创建者在真实聊天交付卡片中创建三人批次，三个 QA Identity 分别提交并在备注中声明 QA 性质，
  状态为 `0/3 open -> 3/3 passed`；
- 创建者在真实浏览器中看到“3/3 位评审人已完成”，点击“确认交付”后页面显示“交付已确认”；
- 数据库最终为 request `succeeded / delivered`、review `passed`、3 份 sealed submission、
  两份 Artifact `approved`；
- 该轮没有新 Provider 请求、Credits 或生产变更。

当前停门：

- 代码、迁移、API 和本地真实评审状态转换已验证；
- 主开发租户的 QA 身份状态机和创建者最终批准 clickthrough 已验证，但 QA 身份不能代替三名真实
  独立人员的质量结论；
- 管理员 evidence ref 当前是受审计的内部 attestation，不是独立 evaluator 的签名回执。进入生产前
  必须接入隔离执行身份、私有对象存储、不可变签名/摘要和保留策略；
- 生产 allowlist、生产迁移、生产真实评审人、告警和回滚演练均未授权，状态仍不是
  `production_verified`。

## 16. 每个阶段的发布检查

发布前：

- migration expand 可前后兼容；
- flags 默认关闭；
- v1 回归；
- cross-tenant/security；
- Credits/recovery；
- worker 版本兼容；
- dashboard/alert；
- rollback runbook；
- 生产凭据和套餐变更另行批准。

发布中：

- 先 backend schema/read；
- 再 worker/reconciler；
- 再 frontend hidden；
- 再 internal tenant；
- 再小比例 allowlist；
- 不在同一发布中同时切 UI、Provider 和价格。

发布后：

- 核对 release SHA、migration head、worker image；
- 跑普通聊天、v1 PPT、quick media；
- 跑一条低成本 v2 canary；
- 核对 request→execution→run/task→reservation→artifact 全链路；
- 核对监控和告警；
- 仅证据充分时报告 `production_verified`。

## 17. 开始写代码前必须确认的剩余外部事实

这些问题不能只从仓库推断，进入 P4 时必须用已购买火山账号实测。当前进展：

1. Agent Plan 的订单级 SKU 和有效期：`pending_console_or_GetPersonalPlan`；行为级判定为 Small；
2. API Key 是否允许 Astra 服务器调用：`verified_local`；
3. Seedream/Seedance 的真实 model ids：Seedream `verified`；Seedance 当前账号 `entitlement_denied`；
4. endpoint、区域和网络：`verified_local`；
5. AFP 抵扣、超额和耗尽语义；
6. RPM、并发、排队和最大任务；
7. 图片/视频输入、输出、时长、分辨率和参考数量；
8. task id、轮询、取消、下载 URL 过期；
9. 明确 rejection 与 acceptance unknown 的错误码；
10. 商用、肖像、客户数据留存和内容标识。

上述事实只影响 Provider Adapter/Profile 和路由，不改变本文的产品流程、权限、Credits、Durable Workflow 和 Artifact 合同。

## 18. 最终实施原则

1. 先冻结现有行为，再扩展。
2. 先建立持久执行和恢复，再开图片/视频 launch。
3. 先优化 MiniMax，后比较火山，避免把编排问题误判为模型问题。
4. PPT 独立建设，不绑定媒体 Provider。
5. 用户购买交付结果，不操作 Skill/Tool/Provider。
6. 快速生成和正式交付长期可以并存。
7. Provider receipt、Credits 和 Artifact 都必须 exactly-once。
8. 修改只重做最小失败单元。
9. 能力不足时保存中间成果并可恢复，不伪造完成。
10. 每一阶段都有停门、灰度和回滚，不做一次性大切换。
