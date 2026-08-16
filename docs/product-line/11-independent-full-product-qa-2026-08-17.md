# 2026-08-17 独立全产品 QA 报告

- 执行角色：独立测试工程师
- 执行范围：当前 `/Users/sun/Documents/PythonProject/Clawith` dirty worktree
- 本地基线：`main...origin/main [ahead 164]`，初始 HEAD `737141127511c916e599eecbb18a4de16859b771`
- Verdict：`PASS_WITH_EXTERNAL_GATES`
- P0/P1：未发现未解释的本地产品阻断缺陷

## 1. 结论

当前工作树通过本地自动化、迁移、能力合同、本地 HTTP/SMTP/OIDC smoke、一次性六角色三租户 fixture 抽样和 390px 页面检查。可以进入后续本地候选冻结与终审，但不能被描述为 immutable candidate、已部署、生产验证、真实外部邮箱到达、真实企业 IdP 验证、付费 Provider 验证或商用质量通过。

本轮 QA 发现并纠正 4 个测试器自身问题，均未判为产品失败：

- `mktemp` 预创建 state 文件导致 fixture seed 拒绝；改用未存在路径，并删除误建的精确 `/tmp/clawith-g8-qa-fixture-XXXXXX.json` 占位文件。
- cleanup 后再调用 fixture `summary` 会因 state 文件已删除而失败；改用 run_tag 直接查库验证残留。
- 历史助理生命周期接口误用 `PATCH`；按前端合同改为 `POST` 后通过。
- Work preflight 初始 payload 使用了非法 priority `normal`；按 schema 改为 `medium` 后通过。

## 2. 命令证据

| 类别 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `backend/.venv/bin/python -m pytest -q` | `4507 passed, 13 warnings in 64.59s` |
| 前端 Node/Vitest | `npm test` | Node `133 passed`；Vitest `38 files / 208 passed` |
| 前端生产构建 | `npm run build` | `6459 modules transformed`，build 成功 |
| MFA 本地 HTTP/PostgreSQL | `backend/scripts/smoke_identity_mfa.py --base-url http://127.0.0.1:3008` | `assertions=35 audit_rows=19 qa_cleanup=passed` |
| 系统邮件 loopback | `backend/scripts/smoke_outbound_email_delivery.py` | `outbound_email_postgres_smtp_smoke=passed` |
| tenant purge | `scripts/tenant-purge-postgres-smoke.sh` | `assertions=32`，head `legacy_assistant_lifecycle` |
| 企业 OIDC 本地模拟 | `ALLOW_LOCAL_OIDC_EMULATOR=true ... smoke_google_workspace_oidc.py` | `assertions=46`，state/code replay、wrong browser、wrong tenant 均拒绝，cleanup passed |
| Agent 能力合同 | `scripts/validate_agent_capabilities.py` | `templates=30 skills=17 tools=141 runtime_typed=114` |
| 六模态矩阵 | `scripts/validate_multimodal_capability_matrix.py --json` | `status=ready`，`provider_health_verified=false` |
| 创意 v1 合同 | `scripts/validate_creative_v1_contracts.py` | `115 passed` |

## 3. 一次性 fixture 与浏览器/API 证据

fixture：`run_tag=284f580805f5`，6 个角色 `owner/admin/member/agent_manager/second_owner/platform`，3 个临时 tenant，3 个 Agent。未输出测试密码或 MFA secret。

| ID | 覆盖对象 | 断言 | 状态 |
|---|---|---|---|
| QA-AUTH-01 | owner/admin/member/agent_manager/second_owner/platform | 密码登录必须进入 MFA challenge；TOTP 后签发 token | Pass |
| QA-ROLE-01 | owner/member | owner `/api/agents/` 看到 3 个 fixture agents；member 看到 0 个 | Pass |
| QA-IDOR-01 | member | member 访问 secondary tenant agent list 返回 `403` | Pass |
| QA-AGENT-01 | agent_manager | 受托者访问 retained assistant 返回 `403`，访问 managed employee 返回 `200` | Pass |
| QA-LEGACY-01 | owner | retained assistant stale expected state 返回 `409 legacy_assistant_transition_invalid` | Pass |
| QA-LEGACY-02 | owner | `archive -> restore_history -> convert_to_employee -> restore_history` 均返回 `200` | Pass |
| QA-WORK-01 | owner | Work preflight 不含 provider/model/skill/tool 字段；返回 `200`、`unavailable`、confirmation fingerprint | Pass |
| QA-WORK-02 | owner | 使用旧 fingerprint 改 title 提交返回 `409 work_capability_changed` | Pass |
| QA-EMAIL-01 | platform | `/admin/platform/system-email` 在 390px 显示系统邮件，测试按钮禁用，`overflow=0`，console error/warning `0` | Pass |
| QA-EMAIL-02 | member | member 调系统邮件配置 API 返回 `403` | Pass |
| QA-ROUTE-01 | admin/member/manager/second_owner/platform | 角色页面路由无横向溢出，未出现 5xx；错误路径 `/platform/system-email` 确认为测试器路径错误，实际导航为 `/admin/platform/system-email` | Pass |

清理：fixture cleanup 删除 `agents=3 identities=6 tenants=3`。随后按 run_tag 直查数据库：`{'identities': 0, 'tenants': 0, 'agents': 0}`。

## 4. 主线覆盖判定

| 主线 | 本地 QA 结论 | 备注 |
|---|---|---|
| 注册、邮箱验证、公司创建/加入、初始化 | Pass by automated tests + local smoke | 真实外部收件箱未验证 |
| MFA | Pass | 本轮覆盖登录 challenge、TOTP、恢复/审计 smoke 与账户 UI 合同；未重新绑定真实手机验证器 |
| 公司 membership 与角色面 | Pass | 六角色 fixture API/页面抽样通过 |
| 权限与跨租户 IDOR | Pass | API 负向覆盖 member、agent_manager、cross-tenant |
| 私人助理、历史助理、数字员工 | Pass | 历史助理状态机和授权边界通过 |
| Work -> Task preflight | Pass with provider-free boundary | Provider 不可用时不创建 Task、不扣 Credits |
| Task -> Run -> Artifact -> Review -> Approval -> Delivery | Pass by automated contract; external/provider execution gated | 本轮未调用真实 Provider，未生成新真实 Artifact |
| Group | Pass by automated contract; browser dynamic multi-agent execution gated | 未调用真实模型执行多人动态终态 |
| Experience/OKR/知识 | Pass by automated contract | 本轮未新增人工浏览器抽样 |
| 套餐/Credits/Provider readiness | Pass for local governance | 六模态 ready 仅证明注册/授权，不证明 provider health |
| 平台运营与系统邮件 | Pass with external inbox gate | `smtp_accepted` 不等于真实收件箱到达 |

## 5. 外部门禁

以下均未执行，必须保持 blocked/unverified：

- 真实外部 SMTP 收件箱到达与已读验证。
- 真实企业 IdP 往返。
- 付费文字/图片/视频/语音/音乐 Provider 调用、真实 Artifact、Credits 结算。
- 图片/视频/PPT 三人独立盲评与商用质量验收。
- 推送、部署、生产迁移、生产 release identity、生产浏览器业务流。

## 6. 剩余风险

- 当前工作树仍 dirty，不能签为 immutable candidate。
- 本轮浏览器是高风险抽样，不替代后续候选 SHA 绑定后的完整录屏矩阵。
- 系统邮件和 Provider 的本地 `smtp_accepted`、`provider_health_verified=false` 边界必须在产品汇报中继续分开陈述。
- Group/正式交付的真实执行终态仍受“无付费 Provider/无真实模型调用”边界限制。

## 7. 最终候选复验（2026-08-17）

复验背景：G8 全量验收之后，最终候选仅新增 `frontend/src/pages/Employees.tsx` 中历史助理 seat-limit 升级 URL 的类型安全提取，以及对应契约测试。复验只更新本报告；未修改产品代码。

### 7.1 差异核对

- `git status --short --untracked-files=all` 确认当前工作树仍为 dirty，报告继续保持“不能签 immutable candidate”的边界。
- `git diff -- frontend/src/pages/Employees.tsx frontend/tests/legacyAssistantLifecycleContract.test.mjs frontend/tests/productLineNavigationContract.test.mjs frontend/src/utils/productRoles.test.ts backend/tests/test_legacy_assistant_lifecycle.py backend/tests/test_agent_plan_limits.py` 核对：
  - `Employees.tsx` 的 `upgradeUrlFromError(error: unknown)` 只接受 `ApiError`，读取 `error.details.upgrade_url`、`error.detail.upgrade_url`、嵌套 `details.upgrade_url`，并对 `402` fallback 到 `/account/subscription`。
  - `legacyAssistantLifecycleContract.test.mjs` 覆盖 typed API envelope 与升级按钮保留。
  - 后端历史助理 quota 测试继续断言 `402/max_agents` 不改变历史助理状态；套餐限额测试继续断言未转换私人助理模板不计入员工 seats。

### 7.2 自动化重跑

| 类别 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `backend/.venv/bin/python -m pytest -q` | `4507 passed, 13 warnings in 64.12s` |
| 前端全量 | `npm test` | Node `134 passed`；Vitest `38 files / 208 passed` |
| 前端生产构建 | `npm run build` | `6459 modules transformed`，build 成功 |
| 历史助理/套餐限额后端直相关 | `backend/.venv/bin/python -m pytest -q backend/tests/test_legacy_assistant_lifecycle.py backend/tests/test_agent_plan_limits.py` | `16 passed in 2.36s` |
| 历史助理/导航/角色前端直相关 | `node --test tests/legacyAssistantLifecycleContract.test.mjs tests/productLineNavigationContract.test.mjs && npx vitest run src/utils/productRoles.test.ts` | Node `17 passed`；Vitest `1 file / 5 passed` |

### 7.3 seat-limit 浏览器/API 复验

fixture：`run_tag=cab41b49899e`，复用 `backend/scripts/browser_acceptance_fixture.py` 创建 6 角色、3 tenant、3 Agent。未输出测试密码或 MFA secret。

设置：仅将 fixture primary tenant 的 `default_max_agents` 改为 `1`，不创建额外 Plan/Subscription。该 tenant 已有 1 个 managed employee，因此 retained assistant 执行 `convert_to_employee` 应触发 seat-limit。

| 场景 | 证据 | 状态 |
|---|---|---|
| 真实 MFA 登录 | owner 密码登录返回 MFA challenge；TOTP 后 `/auth/mfa/challenge/verify` 签发 token；`/auth/me` tenant 匹配 fixture primary tenant | Pass |
| API seat-limit | `POST /api/agents/{retained_assistant}/legacy-assistant-disposition` with `convert_to_employee` 返回 `402`，`quota_type=max_agents`，`upgrade_url=/account/subscription` | Pass |
| API 状态保护 | seat-limit 失败后 `GET /api/agents/{retained_assistant}` 的 `legacy_assistant_disposition` 仍为 `active` | Pass |
| 浏览器用户流 | Playwright 进入 `http://127.0.0.1:3008/employees`，点击 `历史助理整理 -> Browser Previous Assistant -> 转为员工 -> 确认占用名额并转换` | Pass |
| 浏览器升级动作 | 页面出现 `role=alert` 的套餐上限错误；显示 `查看套餐` 按钮；点击后 URL 为 `http://127.0.0.1:3008/account/subscription` | Pass |
| 浏览器 API 观测 | 浏览器过程中捕获该 disposition 请求返回 `402`，响应体包含 `max_agents` 与 `/account/subscription` | Pass |
| 浏览器后状态保护 | 浏览器失败后再次查 API，retained assistant 仍为 `active` | Pass |
| fixture 清理 | cleanup 删除 `agents=3 identities=6 tenants=3`；随后按 run_tag 查库 `identity_count=0 tenant_count=0 named_fixture_agent_count=0` | Pass |

### 7.4 最终 verdict

`PASS_WITH_EXTERNAL_GATES` 仍成立。

外部门禁不变：本轮没有发送真实外部邮件、没有验证真实收件箱到达、没有调用真实企业 IdP、没有调用付费 Provider、没有生成真实付费 Artifact、没有部署/推送/生产迁移。当前候选只能表述为本地 dirty worktree 的最终复验通过，不能表述为 immutable、deployed、production verified 或 commercial quality approved。
