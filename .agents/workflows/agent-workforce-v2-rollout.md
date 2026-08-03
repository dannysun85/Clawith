# Agent Workforce v2 全量落地计划

## 目标

在单一分支 `codex/agent-workforce-v2-rollout` 中，把固定版本
`jnMetaCode/agency-agents-zh@e7c3050dd94212832158e478f0f0af17409070f5`
的 268 个角色完整纳入 Astra 治理：19 个升级现有员工、92 个新增候选、
142 个条件行业包、15 个合并或拒绝项。任何上游 Prompt 都不能直接获得
本地执行权限。

完成结果必须同时具备：版本化角色合同、真实 Skill/Tool/MCP 映射、最小权限、
readiness、迁移回滚、产品入口、效果评测和自动化验证。未取得真实 Provider、
浏览器业务流或生产证据时，必须保留对应未验证状态。

## 固定范围

- 机器清单：`backend/app/data/agent_workforce_catalog.v1.json`。
- 上游来源：268 个唯一角色文件、19 个部门、MIT。
- 当前 Astra 源码基线：33 个内置模板；30 个目录模板、4 个 legacy 模板，
  `Project Manager` 被目录模板覆盖一次。
- 禁止付费 Provider 调用、生产配置修改、推送、部署和外部发布。
- 保留已有 Agent ID、tenant、记忆、关系、触发器、任务记录和用户显式授权。

## 生命周期

| 决策 | 初始生命周期 | 用户可招聘 | 转换条件 |
|---|---|---:|---|
| `upgrade_existing` | `enabled_existing` | 是 | v2 合同和能力再认证 |
| `add_candidate` | `candidate_disabled` | 否 | v2 合同、eval、依赖 readiness |
| `conditional_pack` | `conditional_disabled` | 否 | 行业合同、数据、Tool/Provider、人工审查 |
| `merge_or_reject` | `not_recruitable` | 否 | 只能按目录 resolution 处理 |

状态必须 fail closed。角色存在于目录不等于可招聘，Skill 存在不等于 Tool 授权，
Tool 授权不等于本次动作已审批。

## 角色运行合同

所有启用或重大升级角色使用 `schema_version: 2`，至少声明：

- 稳定 `role_key`、递增 `role_revision` 和 `source_provenance`；
- 5–8 项职责和明确非职责；
- 40–80 行精简 `soul.md`，不常驻复制上游长 Prompt；
- 最小 `default_skills`、`default_tools`、`default_mcp_servers`；
- `default_autonomy_policy`、审批、失败、人工升级和 eval；
- 可对客户承诺的能力仅限至少达到 `business_flow_proven` 的部分。

长工作流、模板、清单和领域知识进入按需 Skill。外部发布、支付、代码执行、
数据修改、消息发送和进攻性安全不得仅由角色文字触发。

## G001：冻结来源与验收基线

交付：

1. 固定上游提交、许可证和实际文件清单。
2. 为 268 个角色保存唯一 ID、中文名、部门、摘要、来源路径和本地决策。
3. 校验 19/92/142/15 总数、不重复、不遗漏和本地 33 模板基线。
4. 保存可重复生成脚本；上游 SHA 不匹配时立即失败。

验证：目录合同测试、生成器 Ruff、`git diff --check`。

## G002：员工目录与生命周期合同

交付：

1. 后端只读目录服务和按 decision/pack/readiness 查询。
2. 模板增加招聘可见性、激活状态、限制和来源读模型；数据库迁移必须兼容旧行。
3. API 默认只返回可招聘模板；管理员可查看 disabled/conditional/rejected 及原因。
4. 条件角色和拒绝角色不能通过直接 API ID 绕过。

停门：旧 Agent 创建流程和 tenant 过滤回归通过，才进入角色批量启用。

## G003：升级 19 个现有角色

交付：

1. 12 个旧目录角色迁移到 v2：Backend、Frontend、Code Reviewer、DevOps、
   Rapid Prototyper、Content、Growth、SEO、LinkedIn、TikTok、Douyin、Chief of Staff。
2. 7 个现有 v2 角色只做来源更新、方法补强和再认证，不覆盖本地安全边界。
3. Project Manager legacy 继续由目录版本覆盖；不新建重复模板。
4. 模板 revision 更新不得重置实例 memory、关系、触发器或用户授权。

验收：每个角色具备职责、非职责、交付物、失败边界和来源；19 个目标键均唯一。

## G004：落地 92 个新增岗位

交付：

1. 按工程、设计、营销、销售、财务、HR、产品、项目、测试、支持和经营专项生成
   92 个独立 v2 目录模板。
2. 研究、策划、草稿、审查可使用已就绪低风险能力；未注册的发布/外发动作写入
   非职责和 blocked 行为。
3. 同义岗位不重复创建；目录中 `target_role_key` 必须与文件夹一致。
4. 初始保持 `candidate_disabled`，通过对应 eval 后按批次启用。

批次：

- A：工程质量、设计、产品、项目、测试；
- B：销售、经营、客服、财务分析；
- C：平台营销岗位的 research/draft 合同；发布能力单独门禁。

## G005：142 个条件包与 15 个拒绝合并项

交付：

1. 142 个角色按 vertical engineering、channel operations、paid media、finance、
   legal、supply chain、spatial、game、academic、GIS、security 等 pack 管理。
2. 每个 pack 声明启用所需数据、许可证、Tool、Provider、审批和人工资质。
3. 15 个角色按 `merge`、`skill_only`、`runtime_capability`、
   `task_scoped_only`、`reject_default` 处理。
4. 自动发布、报告外发、付款和渗透测试均不可成为默认招聘能力。

## G006：Skill、Tool、MCP 与 readiness 闭环

每项执行能力必须完成：

1. 在 `builtin_tool_definitions.py` 注册唯一 schema、effect、readiness、超时、重试和
   敏感字段。
2. 在 `agent_tools.py` 提供 typed adapter 并进入 Runtime typed allowlist。
3. 角色能力写入对应模板 `default_tools`；高风险动作使用 explicit grant。
4. Skill 只提供 SOP，不授予 Tool；MCP 只在服务配置和 tenant 授权有效时可见。
5. readiness 不满足时从模型 workset 隐藏，并返回结构化 `next_action`。
6. 覆盖可用、未配置、显式禁用、审批拒绝、模板撤权和执行失败回执测试。

不因 92 个岗位名称存在就创建虚假 Tool。没有真实动作的岗位允许保持
analysis/draft-only。

## G007：迁移、同步、回滚和产品入口

交付：

1. 模板 revision 与实例采用渐进同步；模板只拥有 `AgentTool.source=template`。
2. 撤销模板授权时保留用户显式选择；升级失败时旧 revision 继续可用。
3. Talent Market 和创建入口展示职责、功能、限制、readiness、来源和生命周期。
4. 普通用户只看到可招聘岗位；管理员可查看候选、条件包和拒绝原因。
5. 每一批通过持久化 lifecycle 和评测 allowlist 控制；回退停止新招聘且不改写已有实例。

已落地接口与状态：

- `POST /agents/template-capabilities/reconcile`：管理员执行本地 Tool、已导入 MCP、
  托管 Skill 和 revision 的无损同步；不导入外部 MCP。
- `GET /agents/{agent_id}/capability-readiness`：返回实际运行 workset 的阻塞项和
  `next_action`，不包含密钥。
- Agent 实例记录 `template_revision_applied`、`template_sync_status`、
  `template_sync_details`、`template_synced_at`。缺依赖保持 `pending`，托管 Skill
  与用户修改冲突时保持 `conflict`。
- 人才市场卡片和聘用确认页展示能力合同、revision、交付物、限制和固定来源；
  `candidate_disabled` 不会由列表或直接 template ID 绕过。

## G008：效果评测与启用门禁

使用不含客户数据的合成匿名任务建立固定 fixture，至少覆盖工程、内容、协调、
营销和销售；后续真实匿名样本必须另经数据审查。新旧版本同题比较：

- 首次有效输出时间；
- 无意义澄清次数和总对话轮数；
- Tool 调用次数、成功率和错误能力声明；
- 人工修改次数、任务完成率、Token 和总耗时；
- 权限、审批、tenant、失败恢复和交付物合同遵守率。

没有 A/B 证据时保持 candidate；新版本没有达到门槛时不得扩大启用范围。

已落地门禁：

- 固定 fixture：`backend/app/data/agent_role_evaluation_fixtures.v1.json`，使用 10 个
  合成匿名任务覆盖 engineering、content、coordination、marketing、sales；禁止写入
  客户数据或把 fixture 当成生产验证。
- `POST /agents/templates/{template_id}/evaluations` 只接收外部已测量结果并持久化，
  不自动调用 Provider；记录模板 revision、fixture、基线、新候选、安全与能力结论。
- `GET /agents/templates/{template_id}/evaluations` 提供仅平台管理员可见的历史证据、
  启用和回滚回执；写操作同样要求平台管理员身份。
- 指标合同固定为任务完成率、首次有效输出、澄清轮次、Tool 成功率、人工修改比例、
  总耗时和 Token。安全、能力或关键指标退化时 `gate_status=failed`。
- `POST /agents/templates/enable-batch` 最多启用 10 个候选且采用原子 fail-closed；
  revision 对应的最新评测未通过、能力合同未就绪或已回滚时整个批次不变更。
- `POST /agents/templates/{template_id}/rollback` 只停止新招聘，不删除或改写已存在
  Agent，并写入审计记录。

## G009：验证、清理和独立评审

顺序执行：

1. 角色目录、模板合同、能力治理和迁移单元测试；
2. tenant、grant、autonomy、readiness 和回滚集成测试；
3. 前端类型检查、测试和 production build；
4. 非付费真实浏览器招聘、限制展示和禁用绕过测试；
5. changed-files `ai-slop-cleaner`，然后重新验证；
6. 独立 `code-reviewer` 和 `architect` 评审；
7. 形成不可变本地候选 SHA，分别报告 `tests_pass`、`business_flow_proven`、
   `provider_verified` 和 `production_verified`。

## 回滚

- 分支可整体丢弃，不影响 `main`；禁止 force push 和生产操作。
- 目录生命周期可将新模板全部恢复为 disabled，不删除已有 Agent。
- 模板 revision 回滚只恢复模板来源授权，不覆盖用户显式 Tool 选择。
- 数据迁移必须支持 downgrade；发生不确定授权或来源漂移时 fail closed。
