# 已知问题、实施范围与回归基线

## 0. 文档状态

- 日期：2026-08-03
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
| PL-010 | P1 | Experience 保存 tenant-scoped Task/Delivery 来源；Work 可跳转“沉淀为团队经验”；发现中心复用员工市场 | 权限负向、草稿发布、来源回跳浏览器流 | `worktree_implemented` |
| PL-011 | P0 | `promote_m3_text_primary` 已把 Lite/Pro/Ultra 的 MiniMax-M3 提升为文字 Primary，Agent Plan 保留安全 fallback；迁移 smoke 和 SaaS 路由浏览器证据已通过 | 真实 Provider route snapshot 需要费用授权 | `local_browser_verified` |
| PL-012 | P0 | MiniMax-only 图片/视频已分类为非等价 `degraded`；正式 Deliverable 默认不提交付费任务，用户显式接受后才允许应急线路；Runtime 有同一门禁和 reason code | 真实主线路故障/恢复与付费对账需授权验证 | `local_browser_verified` |
| PL-013 | P0 | 凭据写入、能力池和 Runtime 均校验 Agent Plan `plan_tier`；Small/Medium 不贡献或选择视频能力，Large/Max 路由标准 Seedance 2.0；当前本地 Large 已取得图片/视频/语音真实 Artifact | 生产凭证、生产真实调用和三人独立质量评审 | `local_provider_verified + production_unverified` |
| PL-014 | P1 | SaaS media routes 已统一显示目标顺序、当前 Provider、主线路、正式/降级/不可用状态、建议动作与成本 | 最后一次真实 Provider 验证时间和 receipt 仍未持久展示 | `local_browser_verified_with_evidence_gap` |
| PL-015 | P1 | 导航已分为工作、协作角色、组织；`/enterprise` 和 `/invitations` 统一受公司管理员守卫；普通成员、`agent_admin`、公司管理员矩阵已实跑；平台管理员在全局控制台可通过“进入公司工作区”选择有效公司成员身份，切换后真实进入租户 `/work`；公司管理员正向入口和 release identity 已复验 | 新候选上普通成员/`agent_admin` 再次登录；全新租户首次切换 | `local_browser_verified_with_role_gap` |
| PL-016 | P0 | 当前 dirty worktree 后端全量 `4465 passed`；能力合同、创作合同、前端 Node `118 passed`、Vitest `207 passed`（38 files）、生产构建、Ruff、compileall、`git diff --check` 和 PostgreSQL fresh/historical/downgrade/re-upgrade smoke 均通过；Alembic 单一 head 为 `onboarding_product_settings`。IAM-01–16 双 Tenant、五身份、desktop/390px 浏览器矩阵、QA 清理与独立 code-reviewer/architect 终审均通过 | immutable candidate SHA、发布/生产证据仍需分别完成 | `local_browser_verified` |
| PL-017 | P0 | 工作台已实现 preflight → confirmation fingerprint → 持久 Task；Group 等待参与者终态并聚合结果 | stale confirmation、重复提交、Group 部分失败与刷新恢复的完整门禁 | `targeted_tests_pass` |
| PL-018 | P1 | OKR 可引用完成 Task 或带批准 Artifact 的成功 Deliverable，并保存不可变 evidence snapshot；本地 UI 已完成真实证据关联和来源回跳 | Artifact 替换、权限负向浏览器流 | `local_browser_verified` |
| PL-019 | P0 | Agent 对象级 `manage` 已统一控制配置、审批查看/处理和 OpenClaw API Key；企业审批队列复用同一可管理对象查询，私人助手保持 owner-only；候选完整测试和定向 API 测试通过 | 新候选上 `agent_admin` 浏览器再次登录 | `candidate_tests_pass + prior_worktree_browser_verified` |
| PL-020 | P1 | 修复浏览器恢复/`localhost` 与 `127.0.0.1` 主机切换后 HttpOnly 媒体会话未续期的问题：App 启动时即使用户已在内存中，也会重新建立同源浏览器会话；图片、音频、视频、PDF 的原生预览不再因 bearer API 正常但媒体 URL 401 而失效 | 生产域名、反向代理和跨子域 Cookie 策略仍需在 E4 生产验收中验证 | `local_browser_verified` |
| PL-021 | P0 | Agent 列表与详情 API 均以 viewer-specific onboarding 关系和内置模板身份下发 `product_role`；当前 companion、历史助理、长期员工分别展示；Private Assistant 模板设为 `not_recruitable`，阻止未来从员工市场重复创建。本地真实浏览器已验证当前助理详情为“我的助理”、员工详情为“Agent 员工”、18 名员工统计/选择器和不含 Private Assistant 的招聘市场 | 本地没有历史助理 fixture，仍需补历史分组的视觉验收；后续实现显式归档/转员工流程 | `local_browser_verified_normal_state + production_problem_verified` |

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
- 历史助理保留原 Agent ID、会话、Workspace、权限和深链；只改变产品分组与员工计数，不自动删除、合并或转员工。
- 临时专家是 Task/Run 级执行策略，不出现在数字员工花名册，不继承长期记忆、Trigger 或 Channel。
- Provider 路由、fallback、Credits 和 Artifact 必须 exactly-once；`acceptance_unknown` 禁止重复提交。
- 普通用户不看到或选择 Provider/model/API Key；平台管理员可以看到真实诊断事实。

## 5. 分阶段实施顺序

1. **已完成实现批次**：文字 Primary、私人助手边界、工作台与对象链、Group 关联执行、Experience 来源、导航职责和 OKR 证据已进入本地工作树。
2. **已完成验证批次**：完整后端/前端门禁、PostgreSQL 迁移 smoke、非付费工作树浏览器矩阵、旧深链与角色负向验证。
3. **已完成修复批次**：PL-012 的媒体降级语义和付费前重新确认已落到 preflight、Runtime 与 SaaS UI；PL-014 已统一当前 readiness 解释，但“最后真实验证 receipt”仍作为显式证据缺口保留。
4. **上一候选收口批次**：反冗余清理、代码审查、架构审查、完整门禁、实现提交和 SHA 绑定管理员浏览器复验对应上一轮 `cc6affe7`；当前工作树有后续未提交修复，不能沿用旧 SHA 作为发布候选。
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
