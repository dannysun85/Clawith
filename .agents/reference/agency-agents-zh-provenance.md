# `agency-agents-zh` 角色来源、筛选与适配事实

## 固定来源

- 上游仓库：`https://github.com/jnMetaCode/agency-agents-zh`
- 本次审查提交：`e7c3050dd94212832158e478f0f0af17409070f5`
- 固定日期：2026-07-31
- 许可证：MIT
- 原英文版权：Copyright 2025 Michael Sitarzewski
- 中文翻译与本地化版权：Copyright 2026 jnMetaCode

仓库内新增角色是面向 Astra 对象模型、权限、就绪检查、执行回执和质量门禁的重写，不是把上游 Markdown 原样当作可执行权限。每个 `schema_version: 2` 模板在 `source_provenance` 中记录实际参考路径。

## 完整清单与决策基线

- 上游当前实际角色文件：268 个，19 个部门。
- 合并升级现有角色：19 个。
- 新增适配候选：92 个，默认生命周期为 `candidate_disabled`。
- 条件行业包：142 个，默认生命周期为 `conditional_disabled`。
- 合并、Skill 化、Task-scoped 或拒绝默认启用：15 个，均不可直接招聘。
- 机器可校验事实源：`backend/app/data/agent_workforce_catalog.v1.json`。
- 目录加载与严格校验：`backend/app/services/agent_workforce_catalog.py`。
- 可重复生成脚本：`backend/scripts/generate_agent_workforce_catalog.py`；脚本要求上游 checkout 精确匹配固定提交，否则失败。

上游 `package.json` 在该提交仍声明 267 个角色，而实际目录、`AGENT-LIST.md` 与本地逐文件核对均为 268 个；其 `scripts/check-counts.mjs` 因此失败。上游 `scripts/lint-agents.sh` 只扫描 253 个文件。Astra 不能把上游自报数量或 lint 通过当作本地上线证据。

## 首批映射

| Astra role_key | 上游参考 | 选择原因 | 关键改写 |
|---|---|---|---|
| `product-manager` | `product/product-manager.md` | 补强问题定义、PRD、证据与产品取舍 | 增加证据状态、验收门禁、Tool 就绪与外发边界 |
| `project-manager` | `project-management/project-manager-senior.md` | 替换旧的通用 Project Manager 角色合同 | 去除 Laravel/Livewire 等硬编码，改为里程碑、依赖、证据和恢复合同 |
| `multi-agent-systems-architect` | `engineering/engineering-multi-agent-systems-architect.md` | 直接支撑 Astra 多 Agent 与能力治理 | 映射到 AgentTemplate/Skill/Tool/Provider/readiness/approval/eval |
| `customer-success-manager` | `specialized/customer-success-manager.md` | 覆盖长期客户结果与风险责任 | 禁止虚构 CRM、情绪、续约概率和外发动作 |
| `support-analytics-reporter` | `support/support-analytics-reporter.md` | 补齐可复现客服数据分析 | 加入数据质量、口径、隐私和因果边界 |
| `xiaohongshu-operator` | `marketing/marketing-xiaohongshu-operator.md` | 增加中文市场内容运营角色 | 禁止 Cookie/非官方发布，接入 `brand-safe-media` 与审批后发布 |
| `security-engineer` | `engineering/engineering-security-engineer.md` | 补齐授权范围内的防御性安全角色 | 增加授权停门、证据级别、密钥保护和安全复测 |

## 明确未直接引入

- 上游中假定存在但 Astra 未实现或未就绪的 Tool、子 Agent、CRM、浏览器 Cookie、发布渠道和 Provider 不会写入角色承诺。
- “角色描述提到某动作”不会产生执行权限；权限只来自 canonical Tool policy、`AgentTemplate.default_tools`、精确 `AgentTool` assignment 和运行时门禁。
- 创意角色目前只复用已经存在且通过合同测试的 quick media 与 `brand-safe-media`。在 DeliverableExecution v2 达到 `business_flow_proven` 前，不新增承诺长视频或高质量 PPT 全流程的员工模板。
