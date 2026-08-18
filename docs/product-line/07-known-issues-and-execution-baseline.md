# 已知问题、实施范围与回归基线

## 0. 文档状态

- 日期：2026-08-17
- 已部署基线：Astra `v1.11.17`，commit `1286865f08a9b09ab4f3bccfd2875f08fd990b15`
- 上游基线：Clawith `v1.11.3`
- 状态：`next_slice_worktree_implemented`
- 事实来源：`docs/product-line/01-06`、`DESIGN.md`、当前模型/API/前端路由和测试
- 非完成声明：`v1.11.17`/`1286865f` 是当前生产基线；本节新增的历史助理分类仍是未提交本地工作树，不是 immutable candidate，也未发布。本地已取得 hash 绑定的火山图片、标准 Seedance 2.0 视频和语音样本，但三人独立盲评、商用质量结论与生产多模态验证仍未完成。

## 1. 本轮目标

在不替换现有 Agent Runtime、Group、Deliverable、Credits、Approval、Workspace 和深链的前提下，
把产品从“页面和 Agent 优先”收敛为“任务和可追溯结果优先”：

`Intent → Task → Run → Artifact → Review → Approval → Delivery → Experience`

普通成员从工作台或自己的私人助手提出结果目标；平台选择长期 Agent、临时专家或 Group；正式产物继续
由 Deliverable/Artifact 管理，Workspace 继续保存工作现场。用户不选择 Skill、Tool、Provider 或模型。

## 2. 已知问题登记表

| ID | 优先级 | 本轮实现事实 | 仍需验收/修复 | 当前状态 |
| --- | --- | --- | --- | --- |
| PL-001 | P0 | 已新增 `/work`，租户根路径默认进入工作台；`/dashboard` 保留 | 无租户和全新用户首次任务浏览器流 | `local_browser_verified` |
| PL-002 | P0 | Onboarding 已使用“私人协调者”，创建或恢复后进入 `/work` | 全新公司、创建失败、幂等恢复浏览器流 | `worktree_implemented` |
| PL-003 | P0 | `Layout.tsx` 按 onboarding 关系拆分“我的助理”和长期 `Agent 员工`；`private_assistant_access` 精确修复旧 company-wide 助手权限 | 全新 tenant 与普通成员再次登录 | `candidate_admin_browser_verified + tests_pass` |
| PL-004 | P0 | Onboarding companion 不执行员工配额检查；员工 `max_agents` 统计排除全部 onboarding assistant ID | 订阅生命周期与账单展示；免费/数量/超限价格待产品财务批准 | `code_gate_exists + external_policy` |
| PL-005 | P0 | 临时专家使用 `executor_kind=temporary_expert` 与不可变角色快照，不创建花名册员工 | 权限负向、执行失败恢复 | `local_browser_verified` |
| PL-006 | P0 | Task additive 增加 tenant、intent、origin、executor、Group、work statement、confirmation 与幂等字段；PostgreSQL upgrade/downgrade/upgrade smoke 已通过 | IDOR 与旧 API 全量回归随最终完整门禁收口 | `migration_smoke_pass` |
| PL-007 | P0 | 服务端 `/api/work` 聚合 Task/Run/Deliverable/Artifact；工作台和 Dashboard 共用 | 大数据分页、并发刷新与浏览器一致性 | `worktree_implemented` |
| PL-008 | P0 | Work projection 分离 execution、artifact、review、approval、delivery 与 `user_stage` | blocked/failed/retry/approval 浏览器恢复 | `targeted_tests_pass` |
| PL-009 | P1 | Work item 深链真实 Agent、Group session 和 Deliverable；Group Run 使用 `work-task:{task_id}` correlation | 跨入口对象 ID 对照和 Group 交接浏览器流 | `targeted_tests_pass` |
| PL-010 | P1 | Experience 保存 tenant-scoped Task/Delivery 来源；只接受同租户、归属一致且已完成的 Task 或已正式交付对象。发布后公司内可读，编辑生成独立 revision draft，发布修订再原子替换来源，撤回/草稿保持非公开；Work 可跳转“沉淀为团队经验”，发现中心复用员工市场。本轮 Experience/引用定向测试通过 | 仍需在最终角色浏览器矩阵验证草稿、发布、来源回跳、撤回和他人未发布内容 403 | `targeted_tests_pass + browser_pending` |
| PL-011 | P0 | `promote_m3_text_primary` 已把 Lite/Pro/Ultra 的 MiniMax-M3 提升为文字 Primary，Agent Plan 保留安全 fallback；迁移 smoke 和 SaaS 路由浏览器证据已通过 | 真实 Provider route snapshot 需要费用授权 | `local_browser_verified` |
| PL-012 | P0 | MiniMax-only 图片/视频已分类为非等价 `degraded`；正式 Deliverable 默认不提交付费任务，用户显式接受后才允许应急线路；Runtime 有同一门禁和 reason code | 真实主线路故障/恢复与付费对账需授权验证 | `local_browser_verified` |
| PL-013 | P0 | 凭据写入、能力池和 Runtime 均校验 Agent Plan `plan_tier`；Small/Medium 不贡献或选择视频能力，Large/Max 路由标准 Seedance 2.0；当前本地 Large 已取得图片/视频/语音真实 Artifact | 生产凭证、生产真实调用和三人独立质量评审 | `local_provider_verified + production_unverified` |
| PL-014 | P1 | SaaS media routes 已统一显示目标顺序、当前 Provider、主线路、正式/降级/不可用状态、建议动作与成本 | 最后一次真实 Provider 验证时间和 receipt 仍未持久展示 | `local_browser_verified_with_evidence_gap` |
| PL-015 | P1 | 导航已分为工作、协作角色、组织；`/enterprise` 和 `/invitations` 统一受公司管理员守卫；普通成员、`agent_admin`、公司管理员矩阵已实跑；平台管理员在全局控制台可通过“进入公司工作区”选择有效公司成员身份，切换后真实进入租户 `/work`；公司管理员正向入口和 release identity 已复验 | 新候选上普通成员/`agent_admin` 再次登录；全新租户首次切换 | `local_browser_verified_with_role_gap` |
| PL-016 | P0 | 2026-08-17 最终工作树后端全量 `4507 passed`；能力合同（30 templates、17 skills、141 tools、114 runtime-typed）、六模态矩阵、创作合同 `115 passed`、前端 Node `134 passed`、Vitest `208 passed`（38 files）、生产构建（6459 modules）、Ruff、compileall、`git diff --check` 均通过。PostgreSQL fresh/historical/downgrade/re-upgrade 与 tenant purge smoke 到唯一 `legacy_assistant_lifecycle (head)`；六身份、多 Tenant、desktop/390px 浏览器正负矩阵及 QA 数据清理完成；独立测试工程师为 `PASS_WITH_EXTERNAL_GATES`，code reviewer 为 `APPROVE`，architect 为 `CLEAR` | 本地 candidate SHA 只能在提交后绑定；发布/生产证据仍需分别完成 | `local_business_flow_proven + independent_qa_passed` |
| PL-017 | P0 | 工作台已实现 preflight → confirmation fingerprint → 持久 Task，并以同一 `client_request_id` 安全重试；Runtime、Artifact、Review、Approval 与 Delivery 保持独立权威状态。前端已按结构化 `error.code` 处理确认过期和能力变化。2026-08-17 浏览器在不指定 Provider/model/Skill/Tool 的情况下提交业务意图，能力不可用时返回 `unavailable` 并明确不创建 Task、不扣 Credits；刷新保留草稿但要求重新 preflight。重复提交、过期确认、失败/取消恢复由后端/前端生命周期自动化覆盖 | 本轮未获授权调用真实 Provider；完整执行终态与网络中断浏览器恢复仍由独立 QA 复核，Group 部分失败归入 PL-024 | `local_browser_verified_without_provider_execution` |
| PL-018 | P1 | OKR 可引用完成 Task 或带批准 Artifact 的成功 Deliverable，并保存不可变 evidence snapshot；实时有效性会把已替换 Artifact 标为失效，但不篡改历史快照；Task 与 Deliverable 必须属于同一 Work 链。本轮 OKR 证据和日报路由定向测试通过 | Artifact 替换、权限负向与来源回跳仍需最终浏览器复验 | `targeted_tests_pass + prior_browser_verified` |
| PL-019 | P0 | Agent 对象级 `manage` 已统一控制配置、审批查看/处理和 OpenClaw API Key；企业审批队列复用同一可管理对象查询，私人助手保持 owner-only；候选完整测试和定向 API 测试通过 | 新候选上 `agent_admin` 浏览器再次登录 | `candidate_tests_pass + prior_worktree_browser_verified` |
| PL-020 | P1 | 修复浏览器恢复/`localhost` 与 `127.0.0.1` 主机切换后 HttpOnly 媒体会话未续期的问题：App 启动时即使用户已在内存中，也会重新建立同源浏览器会话；图片、音频、视频、PDF 的原生预览不再因 bearer API 正常但媒体 URL 401 而失效 | 生产域名、反向代理和跨子域 Cookie 策略仍需在 E4 生产验收中验证 | `local_browser_verified` |
| PL-021 | P0 | Agent 列表与详情 API 已能识别当前 companion、历史助理和长期员工；Private Assistant 模板不可从员工市场重复招聘。creator-only 归档/恢复/转员工/撤回状态机具备乐观并发、审计、席位门禁、运行约束和独立整理区，原 Agent ID、会话、Workspace、深链与默认私有范围不变。2026-08-17 PostgreSQL migration smoke 通过；owner 在真实浏览器完成 `archive → restore → convert_to_employee → return_to_history` 往返，其他身份只看到其被授权的员工，390px 员工页无横向溢出 | 席位不足、并发 expected-state 冲突、他人/跨租户负向由自动化覆盖；独立 QA 仍需复测 | `local_browser_verified + migration_smoke_pass` |
| PL-022 | P0 | Identity MFA、challenge、恢复码、角色强制和会话失效已存在；登录和账户安全页使用同一内存 QR 组件，明确说明角色策略触发的首次设置，手工密钥折叠显示，setup 输入限制为 6 位数字。2026-08-17 HTTP/PostgreSQL smoke 再次通过 `35` 项断言和 `19` 条审计；owner 浏览器登录成功，账户安全页在 desktop/390px 正确显示 MFA 已启用且无横向溢出 | 首次扫码/恢复码浏览器流沿用 IAM-19 证据；本轮未重新绑定真实验证器，独立 QA 仍需复核组件和敏感信息不落 storage | `local_http_postgres_verified + current_security_page_browser_verified` |
| PL-023 | P1 | 系统邮件已进入独立平台导航；SMTP secret 加密落库且只回占位符，测试收件地址由后端校验。测试按钮只依据上一次已保存的完整配置启用，接口与 UI 统一返回 `smtp_accepted` 证据，明确不代表收件箱到达或已读。2026-08-17 platform operator 在 desktop/390px 实跑该页面，无已保存完整配置时按钮保持禁用；公司身份不能进入平台页；loopback SMTP + PostgreSQL 再次通过 | 真实外部收件箱未验证且本轮未发送，不能由本地 `smtp_accepted` 代替 | `local_browser_verified + local_smtp_verified + external_inbox_unverified` |
| PL-024 | P0 | Group Task 固化有序参与者快照，第一个 Agent 是唯一第一责任人，其余为协作者；所有参与者终态后才聚合，部分失败保持 Task `pending` 并逐人说明结果。`work-task:{task_id}` 贯穿 Run，工作台同时保留 Group 现场深链和第一责任 Agent 的正式交付入口；Workspace 使用版本/CAS，Deliverable 修订保留旧执行、Artifact 与评审谱系，Artifact hash 变化会使旧审批 fail-closed。Group/交付定向 205 项及 7 项高风险场景、前端 Group/交付 Node 32 项通过 | 2026-08-17 未为浏览器复测调用真实模型；真实多人执行、部分失败后的重试、修改请求与批准的动态浏览器链仍由独立 QA 在无外部费用边界内复核 | `targeted_tests_pass + prior_browser_verified` |

## 3. 不属于“靠代码直接修好”的外部门禁

以下项目必须保留为显式门禁，不得为了让登记表变绿而伪造事实：

1. 私人助手已使用独立 companion slot，不占长期员工 `max_agents`；是否免费、各 Plan 包含数量和超限价格仍需要产品/财务批准，不能只凭技术豁免对外承诺。
2. 当前 Agent Plan 控制台已确认升级为 Large，并已完成一次受控标准 Seedance 2.0 本地真实调用；追加或批量付费调用仍需要费用范围授权，已有样本仍需要独立质量验收。
3. 图片、视频、PPT 达到商用需要真实独立评审、感知证据、滚动客户样本和质量阈值；QA 身份和自动检查不能替代真人结论。
4. Provider 批量 Benchmark 会产生付费调用；已完成的单样本验证不授权自动重跑、扩样或质量重试。
5. 推送、发布、迁移生产数据、修改生产 Provider 配置、灰度和生产验证需要单独确认。

## 4. 不可破坏合同

- 保留现有 Agent、Group、session、Run、Task、Deliverable、Artifact、Workspace 文件和 URL/ID。
- 旧 `/dashboard`、`/plaza`、`/agents/:id/*`、`/groups/*`、`/quality-reviews/:id`、`/okr`、
  `/enterprise`、`/account/subscription` 深链继续工作。
- 工作台只聚合和深链，不成为第二套 Runtime、Credits、Approval 或 Artifact 状态源。
- 私人助手仍是 `Agent` 执行主体，但通过 onboarding 关系识别；不按名称或自由文本角色猜测。
- 历史助理保留原 Agent ID、会话、Workspace 和深链。归档只停止执行并隐藏于员工花名册；转员工必须由原创建者显式确认、通过名额检查并留下审计，且不得自动扩大原有访问范围。
- 临时专家是 Task/Run 级执行策略，不出现在数字员工花名册，不继承长期记忆、Trigger 或 Channel。
- Provider 路由、fallback、Credits 和 Artifact 必须 exactly-once；`acceptance_unknown` 禁止重复提交。
- 普通用户不看到或选择 Provider/model/API Key；平台管理员可以看到真实诊断事实。

## 5. 分阶段实施顺序

1. **已完成实现批次**：文字 Primary、私人助手边界、工作台与对象链、Group 关联执行、Experience 来源、导航职责和 OKR 证据已进入本地工作树。
2. **已完成验证批次**：完整后端/前端门禁、PostgreSQL 迁移 smoke、非付费工作树浏览器矩阵、旧深链与角色负向验证。
3. **已完成修复批次**：PL-012 的媒体降级语义和付费前重新确认已落到 preflight、Runtime 与 SaaS UI；PL-014 已统一当前 readiness 解释，但“最后真实验证 receipt”仍作为显式证据缺口保留。
4. **候选收口批次**：上一轮 `cc6affe7` 只保留为历史证据；本轮已重新执行反冗余清理、代码审查、架构审查、完整门禁和独立 QA，新的 candidate identity 只能使用本轮提交后的真实 SHA，不能沿用旧 SHA。
5. **授权后批次**：真实 Provider 与豆包 Benchmark、发布、生产迁移和生产业务流均需单独费用/生产授权。

## 6. 回归基线

每个阶段至少运行与变更直接相关的测试；最终合并候选必须运行：

```bash
cd backend
.venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_agent_capabilities.py
.venv/bin/python scripts/validate_creative_v1_contracts.py
.venv/bin/python -m ruff check app tests
.venv/bin/alembic heads

cd ../frontend
npm test
npm run build
```

数据库结构变化还必须完成 fresh upgrade、当前开发库兼容升级和 downgrade/upgrade smoke；真实浏览器至少覆盖
注册公司、私人助手、首次任务、招聘 Agent、临时专家、Group、图片/视频/PPT、审批、订阅和企业配置。

## 7. 完成状态口径

- `scope_frozen`：问题、范围、非目标和门禁已冻结。
- `worktree_implemented`：实现只存在于当前未固化工作树，尚无 immutable candidate SHA。
- `targeted_tests_pass`：与该改动直接相关的定向测试通过，仍不代表整库门禁通过。
- `code_exists`：实现已提交到本地候选，但不表示测试通过。
- `tests_pass`：自动化门禁通过，但不表示浏览器或真实 Provider 通过。
- `local_business_flow_proven`：本地真实浏览器与对象事实已核验。
- `provider_verified`：目标账号和模型真实调用已核验。
- `commercially_usable_proven`：真实独立质量评审和运营指标达标。
- `production_verified`：目标 release、配置、迁移、监控和生产业务流均核验。

任何较低层级不得被包装成较高层级。

## 8. 2026-08-15 身份与权限重构新增 P0

| ID | 优先级 | 原始问题 | 当前本地实现状态（2026-08-15） |
|---|---:|---|---|
| IAM-P0-01 | P0 | `ROLE_HIERARCHY` 把 member、agent_admin、org_admin、platform_admin 排成单线 | 已改为 membership/global/object/surface 四层计算；后端全量与五身份浏览器正负矩阵通过 |
| IAM-P0-02 | P0 | 自助创建允许已有成员新建 Tenant 并成为 `org_admin` | 已使用账户级 `company.create`，创建者原子成为唯一 `org_owner`；Alpha/Beta 两公司与幂等/并发负向通过 |
| IAM-P0-03 | P0 | 公司没有活跃管理员时首位持码加入者自动成为 `org_admin` | first-joiner escalation 已移除；邀请角色由服务端凭证固定 |
| IAM-P0-04 | P0 | 没有 owner，任意本公司 `org_admin` 可以永久删除 Tenant | owner 模型、确认式转移、30 天可恢复删除和受控到期 purge 已实现；真实删除只在一次性本地数据库夹具验证，生产清理仍需授权 |
| IAM-P0-05 | P0 | 平台权分散在 User.role、Identity flag 和配置邮箱 | 全局 platform operator 与公司 membership 已分开，独立产品外壳与依赖已落地 |
| IAM-P0-06 | P0 | 注册码与公司邀请码共用 InvitationCode，缺少 email/role/expiry/status | `RegistrationGrant`、`OrganizationInvitation`、`OrganizationJoinLink` 已分表/分状态机 |
| IAM-P0-07 | P0 | `/me` 主要下发角色，前端多处重复判断 | `/me` 已下发 `effective_capabilities/available_surfaces`，新产品面按能力守卫 |
| IAM-P0-08 | P0 | agent_admin 同时像成员角色又依赖 Agent manage | `AgentPermission use/manage` 为对象权威；兼容值无公司治理加权；admin → 受托者 → admin 委派/撤销与旧页面 fail-closed 已实跑 |
| IAM-P0-09 | P0 | 成员退出/管理员停用只有确认框，没有责任清单，可能留下孤儿 Agent 或泄露 private 工作信息 | 两类服务端 preflight、立即停用/恢复、责任阻断、Agent/私人助手处置、跨公司 fallback 和 private 脱敏均已实跑 |

2026-08-16 G12 已完成 IAM-01 至 IAM-24 的本地收口：G8 为 `local_smtp_verified`，G9 为
`local_http_postgres_verified`，G10 为 `local_oidc_emulated`，G11 为 `isolated_postgres_purge_verified`；G12 的
desktop/390px 五身份双公司矩阵、全量自动化、完整迁移、QA 清理和两路独立终审均通过。Web 注册使用
`/auth/register/init`，旧 `/auth/register` 仅保留等价委托、弃用与 sunset 响应头。

独立架构终审曾发现并阻断 `auth.py` 中 tenant-switch redirect 的未闭合 f-string；修复后当前树重新取得后端
`4496 passed`、前端 Node `125 passed`、Vitest `207 passed`、生产 build、认证/权限专项 `100 passed`、OIDC
`46` 项、MFA `35` 项、purge `32` 项和 PostgreSQL migration 全链通过，architect 由 `BLOCK` 转为 `CLEAR`；
code reviewer 为 `APPROVE`。标准 pytest 入口固定为仓库的 `backend/.venv/bin/python -m pytest`，不以未加载项目
插件的系统 Python 结果替代仓库门禁。

本基线随本地 immutable candidate commit 固化；准确 SHA 在 commit 创建后通过 Git、`/api/version` 和页面 footer
绑定，不回写到 commit 自身。真实外部 SMTP、真实企业 IdP、生产 purge、推送、部署和生产业务流仍未执行，因此候选
不得称为已部署、生产已修复、`provider_verified` 或 `production_verified`。

## 9. 2026-08-18 四类新增 P0

2026-08-17 的独立 QA 是历史合同证据，不覆盖下列新定义的角色数据和 Workbench 行动合同。完整产品合同与
验收编号见 `12-four-p0-product-closure-plan-2026-08-18.md`。

| ID | 优先级 | 新发现 | 目标状态 | 当前状态 |
|---|---:|---|---|---|
| PL-025 | P0 | 普通 member 可读取 tenant 级套餐、Credits、流水、订单与账单主体 | membership-scoped billing.view/manage；member-safe 个人投影；admin/owner 分层 | `scope_frozen + implementation_pending` |
| PL-026 | P0 | OKR/日报/复盘缺对象级 viewer policy，REST/Agent Tool 不一致，普通 member 可触发管理型 outreach | company/本人/Agent object grant 投影；管理动作在敏感读取与后台任务前 fail closed | `scope_frozen + implementation_pending` |
| PL-027 | P0 | Dashboard token aggregate 与 topology node 字段向普通 member 暴露公司资源 | `company.analytics.view`；member topology 资源字段为 `null`；删除前端聚合 fallback | `scope_frozen + implementation_pending` |
| PL-028 | P0 | Work 没有统一产品详情和权威 action inbox，首屏仍要求用户选择 executor | deterministic auto proposal；additive detail/inbox；保留单一 Runtime 与旧 getTask/manual 行为 | `scope_frozen + implementation_pending` |

完成 PL-025–028 必须同时通过 `BILL-*`、`OKR-*`、`OVR-*`、`WORK-*`、`CROSS-*` 新鲜证据。
本节建立问题和验收范围，不代表实现、测试、本地浏览器或生产已经完成。
