# Agent 能力治理整体审计与修复报告

日期：2026-07-21
审计范围：AgentTemplate、Skill、Tool、AgentTool、凭据、套餐、审批、Runtime 工具工作集，以及与上述能力有关的前端入口
代码基线：本地 `main` 的 `5d802fb2`，修复分支 `codex/agent-capability-governance`

## 1. 结论

过去若干次改动的核心问题，不是单一页面或单一模型，而是把不同概念混在了一起：

- 把 Skill 当成可执行能力；
- 把 Tool 的全局默认值当成所有 Agent 的角色授权；
- 把“按钮存在”当成“后端工作流已实现”；
- 把“配置存在”当成“当前租户、当前 Agent、当前套餐真的可用”；
- 把“模型可以自动选择 Tool”误解为“模型一定会正确调用”；
- 把平台统一出资的 MiniMax 凭据重新塞进 Tool 或 Agent 对象；
- 把生产高风险写操作只写进 Prompt，而没有持久化审批门禁。

本轮修复后的正确关系是：

> Skill 只提供工作方法；Tool 才提供可执行动作；AgentTemplate 声明经过评审的角色能力包；AgentTool 记录某个 Agent 的精确授权；凭据、套餐、Provider readiness 和审批策略继续逐层收窄；最终只有通过全部检查的 Tool Schema 才会交给模型。

## 2. 八层能力模型

| 层 | 真实职责 | 不能代替什么 |
|---|---|---|
| 1. Skill | 可复用 SOP、方法、约束、示例和辅助文件 | 不授予 Tool、API key、套餐或外部权限 |
| 2. Tool catalog | 可执行函数名称、参数 Schema、handler 与 readiness 类型 | 不代表每个 Agent 都有权使用 |
| 3. AgentTemplate | 经过代码评审的角色包：`default_skills`、`default_tools`、默认 autonomy | 不是最终运行授权，也不能跳过租户边界 |
| 4. AgentTool | 某个 Agent 对某个 Tool 的精确启用状态、配置和授权来源 | 不能改变 Tool 的租户所有权 |
| 5. Credential ownership | 平台凭据池、tenant config、Agent config 按真实所有者保存 | 加密不能替代所有权判断 |
| 6. Entitlement/readiness | 套餐、档位、Provider 本地配置、渠道和代码沙箱是否满足 | 不产生权限，只能继续收窄已授权能力 |
| 7. Autonomy/approval | 对外发送、安装、发布、部署、自动化、代码执行等动作的 L0-L3 门禁 | Prompt 中一句“先询问”不能代替持久化审批 |
| 8. Runtime workset | 合并以上事实，生成当前模型步骤的最终 Tool Schema | 数据库里存在 Tool 不等于当前模型能看到 |

这八层是串联关系，不是任选其一：

```text
角色/用户选择
  -> Skill 方法
  -> Tool 授权
  -> 租户所有权
  -> 凭据/渠道/沙箱 readiness
  -> 套餐与 Credits
  -> autonomy/审批
  -> 当前模型步骤的 Tool Schema
  -> 模型 AUTO 选择
  -> handler 再次校验并执行
```

## 3. 直接回答：Skill 是通用还是特殊？只要有 Tool 会自动调用吗？

### 3.1 Skill 分三类

1. 全局通用 Skill

   当前只保留 `complex-task-executor`。它提供复杂任务拆解和验证方法，适用于所有 Agent，不声称拥有任何专业 Tool。

2. 角色专用 Skill

   由内置 `AgentTemplate.default_skills` 分配。例如：

   - 内容/媒体角色：`content-writing`、`brand-safe-media`；
   - 开发/部署角色：`vercel-full-stack-deploy`、`mcp-installer`；
   - 研究角色：`web-research`、`market-data`、`financial-calendar`；
   - 私人助理：`meeting-notes`。

   角色 Skill 不是全局必装。过去把 `mcp-installer`、`brand-safe-media`、`vercel-full-stack-deploy` 和 `skill-creator` 推给所有 Agent 是错误的。

3. 用户主动选择或外部导入的 Skill

   只属于目标 Agent。导入不会授予 Tool，也不会覆盖已存在、被用户修改的 Skill 目录。

### 3.2 Tool 的自动调用边界

当前 OpenAI-compatible 和 Responses adapter 在有 Tool 时使用 `tool_choice="auto"`；原生 Gemini 使用 `functionCallingConfig.mode="AUTO"`。这表示模型**可以**从当前 Schema 中选择 Tool，不表示一定调用，也不表示一定选对。

Tool 要进入当前模型步骤，至少必须同时满足：

- Tool 全局启用且属于当前 Agent 可见范围；
- 属于 36 个真正的核心默认 Tool，或存在精确 `AgentTool.enabled=true` 授权；
- 当前 Runtime 已有 typed adapter；
- 本地凭据、渠道、代码执行策略等 readiness 通过；
- 当前 Agent 的套餐/档位允许；
- 调用时通过 autonomy/审批和 handler 二次校验。

### 3.3 Skill 不会像函数一样“自动执行”

当前实现没有独立的确定性 Skill router，也没有 deferred Tool Search。只有当最终工作集中存在 `read_file` 时，Runtime 才把 Agent 工作区的 Skill Catalog 告诉模型，并要求“请求明确匹配时，先读取准确的 `skills/<name>/SKILL.md`”。

因此：

- Skill 是模型遵循的说明包，不是函数；
- 模型可能没有判断出匹配关系，所以不能把“安装了 Skill”当成确定性执行保证；
- Skill 引用的 Tool 如果不在最终工作集中，必须停止并说明缺少能力，不能编造执行结果；
- 需要更高确定性时，后续应增加可观测的 Skill 命中/加载指标，再评估是否建设确定性 router，而不是继续堆 Prompt。

## 4. 当前能力清单事实

本轮以源码 catalog 直接统计：

- 内置 Skill：13 个；全局默认 Skill：1 个；
- 内置 Tool：139 个；核心默认 Tool：36 个；
- 需要精确角色/用户授权的 Tool：18 个；
- Durable Runtime 已有 typed application Tool：112 个；
- 未 typed 的 catalog 名称：27 个，其中包含 Runtime 控制 Tool `finish`、21 个 AgentBay 写/动作 Tool，以及仍被发布门禁隔离的旧 Plaza/Feishu 动作。

18 个精确授权 Tool 是：

```text
agentbay_code_edit_file
agentbay_code_execute
agentbay_code_read_file
agentbay_code_write_file
agentbay_command_exec
check_video_minimax
execute_code
execute_code_e2b
generate_image_minimax
generate_music_minimax
generate_speech_minimax
generate_video_minimax
import_mcp_server
install_skill
publish_page
update_kr_content
update_kr_progress
upload_image
```

## 5. 角色能力矩阵

以下为当前内置角色的额外能力；所有角色仍会继承唯一的全局通用 Skill 和真正的核心默认 Tool。

| 角色组 | 角色 Skill | 额外 Tool 授权 |
|---|---|---|
| Backend Architect / Frontend Developer | `vercel-full-stack-deploy` | `execute_code`、`publish_page` |
| Code Reviewer | 无额外 Skill | `execute_code` |
| DevOps Automator / Rapid Prototyper | `mcp-installer`、`vercel-full-stack-deploy` | `execute_code`、`import_mcp_server`、`publish_page` |
| Content Creator / Douyin / TikTok | `web-research`、`content-writing`、`brand-safe-media` | MiniMax 图片、语音、音乐、视频、视频查询 |
| LinkedIn Content Creator | `web-research`、`content-writing`、`brand-safe-media` | MiniMax 图片、视频、视频查询 |
| Growth Hacker | `web-research`、`data-analysis`、`brand-safe-media` | MiniMax 图片 |
| Chief of Staff | `web-research`、`meeting-notes` | 无额外 Tool |
| Private Assistant | `meeting-notes` | 无额外 Tool |
| 市场/财务研究角色 | `web-research`、`market-data`、`financial-calendar` 的角色组合 | 无额外 Tool；使用已配置的核心搜索/数据能力 |
| SEO Specialist | `web-research`、`competitive-analysis` | 无额外 Tool |

MiniMax 媒体 Tool 的“角色授权”只代表允许请求该能力。真实可用还要求平台 `LLMCredential` 池健康、对应 modality 未阻断、套餐/档位允许、媒体路由启用且 Credits 可预留。

## 6. 本轮确认的历史错误与修复

| 历史错误 | 影响 | 本轮处理 |
|---|---|---|
| 多个专业 Skill 被标成全局默认 | 所有 Agent 获得无关甚至误导性指令 | 只保留 `complex-task-executor` 全局默认，专业 Skill 进入角色模板 |
| 修改 registry 后旧 Agent 工作区仍保留历史 Skill | 新规则无法真正撤销旧错误 | 增加 managed manifest、内容 hash 和自动/用户选择 provenance；只删除字节完全匹配的旧系统副本 |
| Seeder 直接覆盖 Skill 文件 | 用户优化的 Skill 会被启动过程覆盖 | 仅在旧 managed 副本未被修改时升级；用户修改、外部导入和来源不明目录 fail closed |
| Skill 导入只检查简单路径并可覆盖目录 | 外部包可污染或替换现有工作方法 | 统一目录/文件路径验证、500 KiB 上限、根 `SKILL.md`、ClawHub moderation、禁止覆盖 |
| Skill 冲突状态在聚合层使用了错误的单数 key | 检测到用户修改时本应返回 409，实际可能抛 `KeyError` 变成 500 | 统一映射到 `conflicts` 统计并增加服务层与 API 回归测试，用户内容继续原样保留 |
| `.astra-managed.json` / `.astra-import.json` 暴露在文件 UI | 用户可破坏 provenance，使同步永久冲突 | 从文件列表、读取、预览、下载、写入、上传、删除和恢复入口隔离 |
| Tool `is_default` 被当成角色权限 | 代码、媒体、安装、发布等能力向所有 Agent 漂移 | 建立 18 个 explicit-grant Tool；缺少 `AgentTool` 时绝不回退到默认 |
| Runtime 仍直接读取数据库 `is_default` | Seeder 虽会修正显式授权 Tool，但 Seeder 执行前或旧数据漂移时仍存在误放行窗口 | 所有 Runtime、审批重检、媒体、交付物和管理 API 共用 `tool_enabled_for_agent()`；核心默认 Tool 保留，显式授权及 Agent-owned Tool 对陈旧默认值 fail closed |
| Tool 面板为所有 Tool 自动回填 AgentTool | 打开设置页本身会改变授权状态 | 删除读取时写数据库的 backfill；读取接口保持无副作用 |
| 历史 `AgentTool.source=system` 同时表示系统与用户选择 | 无法安全判断哪些授权可撤销 | 新增 `template`、`user_selected`、`legacy_ambiguous` provenance，并按保守规则迁移 |
| 角色切换/模板更新后旧 Tool grant 残留 | Agent 获得超出当前角色的执行能力 | `source=template` 由模板同步器独占管理，角色移除的 grant 会撤销，用户选择不被覆盖 |
| 自定义模板可声明默认 Tool/autonomy | 未经评审模板可批量制造执行权限 | 自定义模板只允许 Skill；`default_tools` 和默认 autonomy 仅限内置评审模板 |
| Runtime 和管理 API 使用不同 Tool 可见性 | 跨租户旧 assignment 可能绕过 UI 或 Runtime | 两端共享同一 tenant ownership predicate；assignment 不再改变 Tool 所有者 |
| MiniMax 凭据可写入 Tool/Agent 配置 | 重新引入对象级授权和路由分叉 | MiniMax 媒体只使用平台 `LLMCredential` 池；API 拒绝对象级密钥，启动时清理旧旁路 |
| 历史全局 Tool config 中混入公司凭据 | 多租户环境可能把密钥归错公司 | 单租户时迁入加密 tenant config；所有者不明时先加密隔离再从 Runtime 清除，不做猜测性归属 |
| 通用 `system_settings` GET 只要求登录且返回 SMTP/Jina 密钥 | 普通用户可读取平台设置，浏览器会收到明文密钥 | GET 改为管理员；敏感项仅平台管理员；API 只返回已配置占位符；存储加密；Runtime 内存解密 |
| 租户 Jina 配置被前端写入全局 system setting | 一个公司的配置影响所有公司 | 移除 Jina 全局前端旁路，回到现有 tenant Tool config；全局值只保留平台兼容 fallback |
| 服务器代码 Tool 只看“存在”或静态环境开关 | UI 可启用但生产 Runtime 无精确 Agent/租户/网络策略授权 | 代码 Tool 必须有 AgentTool、租户授权、生产沙箱与 egress 策略；AgentBay 写动作继续 fail closed |
| 渠道只检查“有一行配置” | 多条配置、缺关键字段或不耐久 Provider 被误判可用 | 使用精确 provider 配置和 durable provider 集合；无真实渠道时 UI/Runtime 都隐藏发送能力 |
| 外部发送、安装、部署、发布仅靠 Prompt 提醒 | 模型可绕过提醒直接产生副作用 | 对能力安装、外部部署、内容发布、外发消息、自动化和代码执行增加持久化 autonomy/审批门禁 |
| 审批后执行又回到字符串成功/失败 | timeout/partial success 被误判并可能重复副作用 | 审批 worker 保留 typed `succeeded/failed/unknown`、artifact/evidence receipt |
| Tool catalog 有重复/漂移来源 | Seeder、UI 和模型 Schema 可能互相矛盾 | Seeder 固定从 canonical `BUILTIN_TOOL_SEEDS` 同步，旧 builtin 漂移项禁用隔离 |
| 媒体按钮按静态列表展示 | 用户看到按钮不代表 Agent、套餐或平台池可用 | 前端只展示后端计算为 `available=true` 的媒体能力 |
| “交付物”抽屉展示 PPT/海报/视频，但后两项只保存表单 | 把半成品伪装成已实现功能，增加无意义填写 | 当前只显示真实可启动且可生成文件的 PPT；海报/视频工作流未完成前不展示 |
| PPT 抽屉要求用户先填写大量结构化字段 | 把 Agent 本应推断的内容转嫁给客户 | 只要求一句目标；页数、受众、风格收进可选设置；后端仍做能力预检 |
| `brand-safe-media` 只给提示词建议 | 参考产品、一致文字和成片验证没有执行合同 | 明确 frozen source hash、参考图、文字 overlay、字体、视频 decode、artifact receipt 和一次生成边界 |

## 7. 凭据所有权规则

| 凭据类型 | 唯一权威位置 | 说明 |
|---|---|---|
| MiniMax 文本/图片/语音/音乐/视频 | 平台 `LLMCredential` 池，`tenant_id IS NULL` | SaaS 统一出资和路由，不允许 Tool/Agent object key |
| 普通 builtin Tool 的公司配置 | `TenantSetting(tool_config:<tool_name>)` | 按 tenant 加密保存 |
| Agent 的个性化 Tool 配置 | `AgentTool.config` | 只能覆盖当前 Agent，不能改变 Tool 所有权 |
| MCP/第三方集成 | tenant/Agent 配置或明确 BYOK | 需要单独授权和 readiness |
| 平台 SMTP、遗留全局 Jina fallback | 加密 `SystemSetting`，仅平台管理员 | 浏览器只能看到配置占位符 |
| 所有者不明的旧全局 Tool 密钥 | `legacy_tool_config_quarantine:*` | 加密、`runtime_enabled=false`，等待管理员人工归属或废弃 |

## 8. 有意保留的限制，不应再误报为完成

1. Skill 路由仍是模型判断，不是确定性 router。当前只能证明 Catalog 有条件注入和路径可读，不能保证每个匹配请求都一定加载 Skill。
2. 21 个 AgentBay 写/动作 Tool 尚未完成 typed provider receipt，继续从 Durable Runtime fail closed。不能因为数据库里有名称就对外宣称可用。
3. `feishu_approval_create`、旧 Plaza 写入和旧 `send_feishu_message` 仍受发布门禁隔离。
4. 海报和短视频“交付物工作流”尚未实现。媒体 Tool 可按 Agent 单独调用，不等于已经有对标豆包/千问的一键成品工作流。
5. 本地 fake Provider、单元测试或构建通过，不等于 MiniMax、Vercel、邮件、MCP 等真实 Provider 已在线验证。
6. 本地迁移代码存在，不等于生产历史数据已经迁移；生产发布前仍须 PostgreSQL migration smoke、备份、灰度和运行后审计。
7. 被隔离的旧凭据不会自动猜测租户。管理员必须依据真实所有者重新配置或废弃。
8. 最终本地启动实测中，Skill Seeder 安全删除了 5 个字节完全匹配的旧副本，同时保留了 73 个来源不明或疑似被用户修改的历史 Skill 目录。它们不会被自动覆盖或删除，但仍需管理员按 Agent 逐项确认是保留、迁移还是废弃。

## 9. 后续功能设计必须遵守的产品规则

1. 先定义用户结果，再决定是 Skill、Tool 还是完整 Workflow；不能先画按钮。
2. Skill 负责“怎样做”，Tool 负责“执行一个动作”，Workflow 负责“多步、可恢复、有产物和费用结算的业务流程”。
3. 新角色能力必须同时声明：角色 Skill、必要 Tool、凭据所有者、套餐、readiness、审批级别、typed receipt 和 UI 可用条件。
4. 前端入口只能依据后端 capability API 展示，不能硬编码“所有人都有”。
5. 任何供应商调用必须区分：请求未发出、明确失败、成功、部分成功和响应不明；响应不明时不能自动重试副作用。
6. 测试报告必须分别写明：`code exists`、`tests pass`、`local fixed`、`business_flow_proven`、`provider_verified`、`production_verified`。

## 10. 本轮封板门禁

已完成的本地门禁：

- changed Python Ruff 通过；`backend/app/main.py` 按仓库既有规则忽略历史 `E402`；
- Agent capability、Skill sync、tenant isolation、凭据、审批、媒体与交付物定向回归通过；
- backend 全量 pytest：`3600 passed`，14 条 warning，无失败；
- frontend：67 个 Node test 与 124 个 Vitest test 通过；Vite production build 通过；
- Alembic 只有 `agent_template_default_tools (head)`；
- PostgreSQL fresh upgrade、downgrade、re-upgrade 与 seeder smoke 通过；
- `git diff --check` 通过；
- 本地 source stack 启动成功，frontend `:3008`、backend `:8008` 均 ready；
- 最终候选代码重新执行 migration、data migration 和 Seeder 后启动成功；`/api/health` 与 `/login` 均返回 `200`，Alembic 仍只有 `agent_template_default_tools (head)`；
- 浏览器使用平台管理员登录后切换到真实测试租户，成功打开私人助理；
- 浏览器点击“新建会话”后，后端 `POST /api/agents/{agent_id}/sessions` 返回 `201`，页面进入带 `session_id` 的新会话，不再出现 `A tenant is required for chat sessions`；
- Skills 页展示“方法而非权限”的边界说明；Tools 页展示“模型可 AUTO 选择但不保证调用”的边界说明；
- 对话框只展示真实可启动的 PPT 交付入口；PPT 工作台只要求一句目标，其他字段折叠为可选；未完成的海报/视频工作流不展示；
- PPT 预检最终返回 `200`、请求创建返回 `201`；待发送卡片可移除，移除时只清理由工作台自动生成且仍未被用户修改的启动文本，不会误删用户自行编辑的内容；
- 已测试的关键业务接口没有新增 4xx/5xx 或前端运行时错误，但本地 backend 曾出现一次约 140–218 秒的瞬时事件循环/数据库连接停顿；PostgreSQL 同期未记录服务端故障，连接池随后恢复正常。当前证据不足以把它归因于本轮代码，亦不能据此宣称本地运行稳定性已完全闭环，发布前应继续纳入运行时监控和压力复测；
- 独立代码/安全 reviewer 复核为 `PASS`，独立架构/产品 reviewer 复核为 `CLEAR`。首轮发现的 Skill 冲突 500 和陈旧 `is_default` 显式授权回退均已修复并经过二次复核，无剩余阻断项。

仓库全量 Ruff 仍有 101 条本轮之前已存在的历史问题；它们不在本轮改动文件中，不能伪装成已清零，也不影响上述 changed-file gate。

在真实 Provider 和生产环境没有执行前，最终结论只能是“本地代码与本地业务流验收通过”，不能写成“Provider 已验证”或“生产已闭环”。
