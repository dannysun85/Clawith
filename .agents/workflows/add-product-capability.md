# 新增产品能力、Skill、Tool、Provider 与 Agent 员工工作流

## 目标

把每个新功能从“一个模型/接口能调用”推进到“客户可理解、可授权、可计费、可恢复、可验收的产品能力”。本流程同样适用于新增图片/视频/PPT 能力和后续 Agent 员工。

## 0. 先分类，不先写代码

明确本次变化属于哪些对象：

- 新的客户结果：Capability；
- 新的方法/SOP：Skill；
- 新的执行动作：Tool；
- 新的执行后端：Provider Adapter；
- 新的长期责任角色：Agent employee；
- 仅升级质量/成本/可靠性：现有 Capability 的新 revision。

一个需求可以涉及多类对象，但必须分别列出，不得用一个 Skill 包办全部职责。

## 1. 建立产品合同

写清：

- 用户任务与目标受众；
- 必填/可推断/仅缺失时询问的输入；
- 输出格式、可编辑性、质量和时效；
- 精确保真要求与允许变化范围；
- 费用/额度与必须批准的节点；
- 不负责事项；
- `available/degraded/unavailable` 行为；
- 成功、部分成功和失败的判定。

停门：没有可测试的输出合同，不进入 Tool 或 Provider 实现。

## 2. 用真实样本建立基线

1. 从已授权且完成匿名化的真实客户任务建立代表性 fixture。
2. 同题保存当前系统、候选实现和人工/竞品参考输出。
3. 为正确性、可用性、视觉质量、一致性、时间、成本和失败恢复定义评分。
4. 区分编排问题、Provider 能力上限、输入质量问题和产品合同问题。

停门：没有代表样本和评分表，不得得出“应该换模型/Provider”的结论。

## 3. 定义 Provider-independent schema

至少定义：

- `CapabilityManifest`：输入、输出、风险、成本与依赖；
- 领域 brief/schema；
- `CapabilityProfile`：Provider 真实支持的特性；
- durable job 状态和 provider receipt；
- Artifact manifest；
- revision/retry contract。

普通用户请求不得包含 provider/model 字段。

## 4. 编写或升级 Skill

Skill 必须包含：

- 何时触发和何时不触发；
- 如何从自然语言/附件推断 brief；
- 哪些缺失信息必须询问；
- 如何选择交付合同；
- 质量检查与验收；
- 降级、blocked、恢复和人工升级；
- Tool 调用前置条件；
- 禁止行为。

Skill 不得包含密钥、暗示越权、硬编码 tenant，或声称自己拥有 Tool。

## 5. 实现 Tool 与 Provider Adapter

Tool：

- 使用稳定、严格的输入 schema；
- 校验 tenant、Agent、路径、文件和参数；
- 使用幂等键；
- 返回结构化 receipt，不依赖解析自然语言成功消息；
- 支持 durable execution、取消/轮询/恢复（适用时）；
- 只从受控凭据服务获取 secret；
- 保留隐私安全审计。

Provider Adapter：

- 声明 capability、限制、价格、健康和错误语义；
- 把供应商状态映射到统一状态；
- 处理任务 ID、下载 URL 过期和异步恢复；
- 明确区分 rejected、accepted、succeeded、failed、`acceptance_unknown`。

停门：本地 mock/单元测试通过不等于 `provider_verified`。

## 6. 设计授权、套餐和自主级别

逐项决定：

- product-wide default 还是显式 Agent grant；
- builtin/admin/tenant/agent ownership；
- 哪些 AgentTemplate 可以获得；
- Plan modality/tier/额度；
- Credits 估算、预留、结算和退款；
- autonomy action type；
- 何时需要人工批准；
- 管理员如何禁用、撤销和审计。

停门：没有 grant/revoke 路径、计费安全或租户边界，不得上线。

### 6.1 Tool 注册到 Agent 的仓库落点

新增 builtin Tool 时必须一次完成以下闭环，不能只把 Tool 写进目录：

1. 在 `backend/app/services/builtin_tool_definitions.py` 注册唯一 Tool schema、`effect`、`readiness`、超时、重试和敏感字段合同。
2. 在 `backend/app/services/agent_tools.py` 增加真实 typed execution adapter，并进入 `RUNTIME_TYPED_APPLICATION_TOOL_NAMES`；只有目录定义、没有执行适配器的 Tool 不能授予 Agent。
3. 选择分配模式：
   - 高频低风险产品底座：canonical `is_default=true`，对全部合格 Agent 生效；
   - 角色能力：加入 `EXPLICIT_GRANT_TOOL_NAMES`，并写入相关 `backend/agent_templates/<role>/meta.yaml` 的 `default_tools`；
   - 租户/Agent 自定义能力：通过 Agent 管理界面建立精确 `AgentTool` assignment；
   - 手动或系统专用：加入 `EXPLICIT_TOOL_ASSIGNMENT_EXEMPTIONS` 并写明原因。
4. 为外部依赖实现 readiness；未配置时 Tool 必须从模型 workset 隐藏并返回可操作的 `next_action`，不能等 Agent 调用后才发现。
5. 运行 `cd backend && .venv/bin/python scripts/validate_agent_capabilities.py`。该命令同时检查 Skill、Tool 目录、runtime adapter、模板引用和 explicit Tool 分配决策。
6. 启动同步时，`reconcile_template_tool_grants()` 会把模板授权幂等应用到已存在员工，撤销模板删除的授权，并保留用户显式选择；全局禁用只影响运行时可见性，不删除角色授权。必须检查 `TemplateToolReconcileReport`。
7. 增加或更新角色 eval，至少覆盖：Tool 可用、Tool 未配置、用户显式禁用、模板移除授权、审批拒绝和执行失败回执。

新增 Agent 员工时，使用 `schema_version: 2`；`role_key` 必须与目录名相同，并声明职责、非职责、最小 Skill/Tool、自主策略和来源。模板加载不再容忍未知字段或缺失能力引用。

## 7. 接入 Durable Workflow

持久化：

- 用户 brief 与附件引用；
- preflight 结果和预计 Credits；
- 批准记录；
- stage/run 状态；
- provider receipt；
- 每个中间/最终 Artifact；
- quality/selection receipt；
- revision 和局部 retry 关系。

刷新、断线、worker 重启和重复点击后必须从后端事实恢复，不能依赖前端乐观状态。

## 8. 完成降级与故障恢复

对每个依赖逐项演练：

- Tool 被禁用；
- Plan 不允许；
- 凭据池不可用；
- Provider 提交前不健康；
- 提交后超时/结果未知；
- 额度不足；
- 下载过期；
- Artifact 校验失败；
- 单页/单镜头/单候选失败。

验证不会重复提交、重复扣费、丢 brief、跨 tenant、伪造成功或强制全量重做。

## 9. 分层验证

按顺序取得证据：

1. schema/unit tests；
2. tenant/grant/autonomy/Credits/security tests；
3. Provider sandbox/真实账号验证；
4. durable integration/recovery tests；
5. 真实浏览器端到端任务；
6. Artifact 结构与人工质量验收；
7. allowlist/canary；
8. production release/runtime/monitoring 验证。

每次报告必须使用 `.agents/rules/capability-and-agent-governance.md` 的分级状态，禁止把较低门槛包装成 `production_verified`。

## 10. 决定是否增加 Agent 员工

仅在能力本身已经证明后判断：

- 一次性或偶发任务：由 task-scoped expert 执行；
- 需要长期记忆、周期触发、事件监听、渠道身份、跨任务积累或命名责任：建立/更新 AgentTemplate；
- 只是新增 Tool：更新合适角色的最小 grant，不自动新建员工。

建立员工时补齐 role/soul、非职责、Skill、Tool、autonomy、trigger、channel、relationship、KPI、eval、失败升级和 recertification。

## 11. 灰度、回滚与维护

1. 先内部 tenant/allowlist，记录成功率、首轮可用率、平均耗时、单位可用产物成本、人工修改次数和恢复成功率。
2. 保留旧路径直到新路径完成业务流等价和观察期。
3. 默认路由、套餐、价格或 Provider 切换必须可回滚。
4. Provider/模型/Tool/Skill 大版本变化触发重新评测和员工能力再认证。
5. 文档同步：
   - 产品/UI 决策更新 `DESIGN.md`；
   - 强制边界更新 `.agents/rules/`；
   - 步骤更新 `.agents/workflows/`；
   - 实现事实更新 `.agents/reference/`；
   - 新专题必须登记到 `SKILL.md`。

## 交付检查表

- [ ] 产品合同和非目标明确
- [ ] 真实匿名化样本与基线存在
- [ ] Provider-independent schema 存在
- [ ] Skill 与 Tool 权限分离
- [ ] tenant/grant/autonomy/entitlement/Credits 门禁完整
- [ ] available/degraded/unavailable 行为完整
- [ ] `acceptance_unknown` 不会触发重复提交
- [ ] Durable Run/Artifact/revision 可恢复
- [ ] 真实 Provider 和浏览器业务流分别验证
- [ ] Artifact 通过结构与质量验收
- [ ] Agent employee 是否必要已有明确结论
- [ ] 灰度、监控、回滚和再认证方案存在
- [ ] `DESIGN.md`、专题文件和 `SKILL.md` 索引已同步
