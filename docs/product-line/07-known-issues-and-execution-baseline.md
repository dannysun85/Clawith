# 已知问题、实施范围与回归基线

## 0. 文档状态

- 日期：2026-08-02
- 基线版本：Astra `v1.11.14`；当前工作树 HEAD `1a81291a`
- 上游基线：Clawith `v1.11.3`
- 状态：`local_business_flow_verified_with_benchmark_gate`
- 事实来源：`docs/product-line/01-06`、`DESIGN.md`、当前模型/API/前端路由和测试
- 非完成声明：旧候选 `1276da37` 已因角色入口和迁移兼容问题失效；`cc6affe7aa1ad35f5bc1e4be0ab4a7247067b248` 是上一轮已验证候选，不代表当前工作树。当前工作树包含未提交实现和媒体凭证路由防错修复，因此尚未形成新的 immutable candidate SHA。当前本地自动化回归、真实浏览器产物展示和无新增付费调用的 Benchmark 审计已有证据；真实 Provider 新生成、三人独立盲评、发布和生产验证仍未执行。

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
| PL-013 | P0 | 凭据写入、能力池和 Runtime 均校验 Agent Plan `plan_tier`；Small 不贡献/选择视频能力 | 当前账号真实资格仍未获授权复验；Provider submit=0 的浏览器/集成证据待补 | `code_gate_exists + external_account` |
| PL-014 | P1 | SaaS media routes 已统一显示目标顺序、当前 Provider、主线路、正式/降级/不可用状态、建议动作与成本 | 最后一次真实 Provider 验证时间和 receipt 仍未持久展示 | `local_browser_verified_with_evidence_gap` |
| PL-015 | P1 | 导航已分为工作、协作角色、组织；`/enterprise` 和 `/invitations` 统一受公司管理员守卫；普通成员、`agent_admin`、公司管理员矩阵已实跑；平台管理员在全局控制台可通过“进入公司工作区”选择有效公司成员身份，切换后真实进入租户 `/work`；公司管理员正向入口和 release identity 已复验 | 新候选上普通成员/`agent_admin` 再次登录；全新租户首次切换 | `local_browser_verified_with_role_gap` |
| PL-016 | P0 | 当前工作树后端最新全量 `4192 passed`；能力合同、creative v1 `94 passed`、前端 Node `107 passed`、Vitest `158 passed`、生产构建、Ruff、`git diff --check` 和 Alembic 单一 head（`backfill_private_assistant_tpl`）均通过；上一轮 `cc6affe7` 候选的提交后复验仍可追溯，本轮新增凭证路由防错、机器 JSON 输出、平台租户切换、文字主/备 Provider 路由和产品上市 PPT 视觉意图识别回归尚未固化为新的 immutable SHA | 新候选的完整迁移/浏览器收口和精确 SHA 绑定 | `local_business_flow_verified_with_evidence_gap` |
| PL-017 | P0 | 工作台已实现 preflight → confirmation fingerprint → 持久 Task；Group 等待参与者终态并聚合结果 | stale confirmation、重复提交、Group 部分失败与刷新恢复的完整门禁 | `targeted_tests_pass` |
| PL-018 | P1 | OKR 可引用完成 Task 或带批准 Artifact 的成功 Deliverable，并保存不可变 evidence snapshot；本地 UI 已完成真实证据关联和来源回跳 | Artifact 替换、权限负向浏览器流 | `local_browser_verified` |
| PL-019 | P0 | Agent 对象级 `manage` 已统一控制配置、审批查看/处理和 OpenClaw API Key；企业审批队列复用同一可管理对象查询，私人助手保持 owner-only；候选完整测试和定向 API 测试通过 | 新候选上 `agent_admin` 浏览器再次登录 | `candidate_tests_pass + prior_worktree_browser_verified` |
| PL-020 | P1 | 修复浏览器恢复/`localhost` 与 `127.0.0.1` 主机切换后 HttpOnly 媒体会话未续期的问题：App 启动时即使用户已在内存中，也会重新建立同源浏览器会话；图片、音频、视频、PDF 的原生预览不再因 bearer API 正常但媒体 URL 401 而失效 | 生产域名、反向代理和跨子域 Cookie 策略仍需在 E4 生产验收中验证 | `local_browser_verified` |

## 3. 不属于“靠代码直接修好”的外部门禁

以下项目必须保留为显式门禁，不得为了让登记表变绿而伪造事实：

1. 私人助手已使用独立 companion slot，不占长期员工 `max_agents`；是否免费、各 Plan 包含数量和超限价格仍需要产品/财务批准，不能只凭技术豁免对外承诺。
2. 当前 Agent Plan Key 的视频行为是 Small。Medium+ 购买、套餐生效和 Seedance 真实调用需要账号/成本授权。
3. 图片、视频、PPT 达到商用需要真实独立评审、感知证据、滚动客户样本和质量阈值；QA 身份和自动检查不能替代真人结论。
4. Provider 批量 Benchmark 会产生付费调用，本轮未授权新增消耗。
5. 推送、发布、迁移生产数据、修改生产 Provider 配置、灰度和生产验证需要单独确认。

## 4. 不可破坏合同

- 保留现有 Agent、Group、session、Run、Task、Deliverable、Artifact、Workspace 文件和 URL/ID。
- 旧 `/dashboard`、`/plaza`、`/agents/:id/*`、`/groups/*`、`/quality-reviews/:id`、`/okr`、
  `/enterprise`、`/account/subscription` 深链继续工作。
- 工作台只聚合和深链，不成为第二套 Runtime、Credits、Approval 或 Artifact 状态源。
- 私人助手仍是 `Agent` 执行主体，但通过 onboarding 关系识别；不按名称或自由文本角色猜测。
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
