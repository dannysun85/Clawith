# 产品能力、Skill、Tool 与 Agent 员工治理规则

## 适用范围

本规则适用于所有新增或升级的产品能力，包括但不限于图片、视频、PPT、文档、数据分析、发布、通信、外部系统操作，以及后续增加的 Agent 员工。

任何实现、评审或上线工作都必须同时遵守本规则和 `.agents/workflows/add-product-capability.md`。图片、视频、PPT 的当前事实与专项质量合同见 `.agents/reference/creative-deliverables-capability.md`。

## 一、用户只购买结果，不操作内部零件

1. 普通用户入口必须以任务和交付物为中心，不得要求用户先理解或选择 Skill、Tool、Provider、模型、API Key 或内部 Agent 拓扑。
2. 标准产品流程必须是：

   `提出任务 → 补齐关键约束 → 确认工作说明/预计费用 → 执行 → 验收/修改 → 归档`

3. 平台必须先识别任务结果、风险和交付合同，再选择 Expert/Agent、Skill、Tool 和 Provider。
4. Provider 与模型选择属于平台路由策略，只能在 SaaS Admin、审计或故障诊断界面暴露。
5. 直接工具入口可以作为管理员、测试或“快速生成”能力保留，但不得替代可恢复、可验收的正式交付流程。

## 二、五类对象必须分离

### Capability

Capability 描述产品承诺的结果，例如“生成品牌安全海报”“生成可编辑 PPT”“制作 15 秒竖屏短视频”。它必须定义输入、输出、质量、成本、风险和降级合同。

### Skill

Skill 是 Agent 的方法、判断规则和 SOP。Skill 可以决定何时询问、如何拆解、如何检查和如何恢复，但不得包含 Provider 密钥，也不得绕过 Tool、套餐、Credits、租户或审批检查。

### Tool

Tool 是可执行动作及其 schema。Tool 必须负责参数校验、租户边界、幂等、执行凭据、结构化回执和安全失败；Tool 不得因为某个 Agent 拥有同名 Skill 就自动授权。

### Provider

Provider 是可替换执行后端。Provider Adapter 必须隐藏供应商差异，并声明真实 `CapabilityProfile`、健康状态、价格/额度、异步状态、结果有效期和错误语义。

### Agent employee

Agent 员工是持久责任主体，包含身份、职责、记忆、触发器、渠道、Skill、Tool grant、自主级别和验收指标。不能把“有一个新工具”直接等同于“需要招聘一个新员工”。

## 三、调用授权必须通过完整门禁链

所有用户可以提出任务；只有通过以下门禁的 Agent 才能真正执行 Tool：

1. 请求、Agent 和所有输入属于同一有效 tenant。
2. Agent 处于可运行状态，并被当前用户/团队允许参与任务。
3. 所需 Skill 能通过 global/tenant scope、显式选择或 AgentTemplate 正确解析。
4. Tool 全局启用，且对该 tenant 与 Agent 可见。
5. Tool 已通过产品默认策略或显式 `AgentTool`/AgentTemplate grant 授权；显式禁用必须优先。
6. 租户 Plan entitlement 允许对应 modality、tier 和用量。
7. 路由所需 Provider capability、平台凭据池和网络健康状态可用。
8. Credits 预检/预留成功，预计费用和实际结算遵循 exactly-once 语义。
9. Agent autonomy policy 允许该动作；高风险、外发、发布或超阈值付费动作取得所需批准。
10. Durable workflow 状态允许执行，且请求带稳定 idempotency key。

任何一项失败都必须返回结构化原因和下一步，不得通过换 Agent、换 Skill、复制 Tool 或直接调用 Provider 绕过。

**Skill 存在不代表 Tool 有权限；Tool 有权限也不代表本次动作已获批准。**

## 四、默认授权与最小权限

1. 高频、低风险、产品级能力可以设计为 product-wide requestable capability，但实际执行仍必须经过上述门禁。
2. 高成本、外部发布、安装扩展、代码执行、导入 MCP、发送消息或修改外部数据的 Tool 必须按角色/模板显式 grant。
3. AgentTemplate 只能授予该角色完成职责所需的最小 Skill 与 Tool 集。
4. 移除模板授权时，只能撤销模板来源的 grant，不能覆盖用户的显式选择。
5. tenant 自定义 Skill/Tool 只能在本 tenant 使用；Agent-owned Tool 必须精确分配。
6. 普通员工不得继承“所有未来 Tool”。每个新增 Tool 都要重新完成授权设计和审查。
7. 所有 folder-based AgentTemplate 必须通过 `AgentTemplateManifest` 严格校验；未知字段、缺失 Skill、未注册 Tool、没有 Durable Runtime typed adapter 的 Tool 都必须 fail closed，不得跳过后继续启动。
8. 每个 explicit-grant Tool 必须被至少一个 AgentTemplate 引用，或在 `EXPLICIT_TOOL_ASSIGNMENT_EXEMPTIONS` 中记录手动/系统专用原因；新增 Tool 导致审计失败时，必须先完成分配决策，禁止通过删除测试绕过。
9. AgentTemplate 自动同步只拥有 `AgentTool.source=template`；用户显式启用/禁用必须保留，模板移除的授权必须自动撤销并输出结构化同步报告。

## 五、能力不可用时不得伪造成功

每个能力必须实现三态预检：

- `available`：能够按已确认合同执行。
- `degraded`：仅能提供不同质量、成本、格式、时长或等待时间的方案。必须说明差异并在差异实质性时重新确认。
- `unavailable`：不能完成请求的最终生成。

`unavailable` 时必须：

1. 保存原始请求、工作说明、输入引用和已完成阶段。
2. 生成仍然有用且不依赖缺失能力的中间产物，例如 brief、脚本、分镜、版式、提示词包、素材清单或可编辑占位稿。
3. 将请求标记为可恢复的 blocked 状态，显示安全、可操作的原因和恢复条件。
4. 能力恢复后从最小未完成阶段继续，不重复已成功或已计费的阶段。
5. 明确区分“已保存/已准备/待能力恢复”和“已生成最终产物”。

禁止：

- 用文本说明、占位图、损坏文件或未经验证的路径冒充最终产物；
- 在用户不知情时降低质量、改变比例、缩短时长、栅格化可编辑内容或换成创意重绘；
- Provider 已接受但结果未知时向另一 Provider 重复提交；
- 因能力不可用而丢失客户工作说明或要求从头填写。

## 六、Provider fallback 与计费安全

1. 只有以下情况可以自动选择等价健康 Provider：
   - 提交前发现原 Provider 不健康或能力不匹配；
   - Provider 明确拒绝且确认未受理、未计费；
   - 路由合同预先声明 Provider 可替换，且输出、质量、合规和预计费用不发生实质变化。
2. `acceptance_unknown`、超时但可能已受理、回执丢失等状态必须进入 `reconciling`，禁止立即 fallback。
3. 非等价 fallback 必须作为 `degraded` 方案向用户说明并重新确认。
4. Credits reservation、provider receipt、下载与 artifact commit 必须具备 exactly-once/幂等语义。

## 七、质量不是单次模型调用

任何面向客户的生成能力都必须至少包含：

1. 结构化 brief；
2. 输入与参考素材清单及 hash；
3. Provider-independent 中间 schema；
4. Provider-specific 编译/适配；
5. 候选或重试策略；
6. 机器可执行的结构、格式与安全检查；
7. 与业务目标相关的质量评估；
8. 选择/修改回执；
9. 可审计的最终 Artifact；
10. 局部重做与版本记录。

不能用“模型返回了一个 URL/文件”作为质量完成证据。

## 八、新 Agent 员工的成立条件

1. 先定义业务责任、输入来源、交付物、服务对象、触发方式、边界和 KPI，再决定是否需要持久 Agent。
2. 一次性任务优先使用 task-scoped expert；需要长期记忆、定时/事件触发、渠道身份或持续问责时才建立 Agent 员工。
3. AgentTemplate 必须声明：
   - role/soul 与不负责事项；
   - `default_skills`；
   - 最小 `default_tools`；
   - `default_autonomy_policy`；
   - 输入渠道、触发器和协作关系；
   - 质量 eval、失败处理和人工升级路径。
   新建或重大升级模板使用 `schema_version: 2`，并补齐稳定 `role_key`、`role_revision`、职责、非职责和可追溯来源。
4. 只有其依赖能力至少达到 `business_flow_proven`，才能把该能力写入员工对客户的承诺。
5. 能力不可用时，员工可以继续分析和准备中间产物，但必须明确 blocked，不得假装完成执行。
6. Provider、Tool 或 Skill 发生重大变更后，相关员工必须重新进行 capability recertification。

## 九、完成状态必须分级陈述

以下状态不得混称“完成”：

- `spec_defined`：产品合同与边界已定义。
- `skill_ready`：SOP/判断/恢复规则已就绪。
- `tool_ready`：执行 Tool 与本地测试就绪。
- `provider_verified`：真实 Provider/API 已验证。
- `business_flow_proven`：真实浏览器业务流和有效 Artifact 已证明。
- `production_verified`：目标生产 release、运行时、计费、监控和真实流程已证明。

代码存在、测试通过、部署成功和客户可用分别报告，不得互相替代。

## 十、禁止的捷径

- 不得因竞品展示效果更好就直接替换 Provider，必须用真实客户样本做同题 A/B。
- 不得把 Provider SDK schema 暴露成产品工作说明。
- 不得把密钥放进 Skill、Agent workspace、Tool config 或日志。
- 不得创建一份只写提示词、不写权限、计费、恢复和验收的“能力方案”。
- 不得先批量创建 Agent 员工，再补其能力和验收合同。
- 不得在未经产品、财务、安全或生产授权的情况下购买套餐、写入凭据、切换默认路由或进行生产灰度。
