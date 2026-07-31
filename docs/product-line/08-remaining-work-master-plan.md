# 产品线剩余工作总计划

## 0. 计划基线

- 日期：2026-08-01
- 本地基线：`35693e9321b3660eb6aedc84df320552d1a5d946`
- 分支：`main`（仅本地，未推送、未发布）
- 事实来源：`docs/product-line/01-07`、当前代码、测试、迁移与既有本地浏览器证据
- 总目标：把“私人助手是默认任务入口、Agent 是长期执行员工、Deliverable 是正式产物、Workspace 是工作现场”落实为可恢复、可审计、可验收的完整产品链路。

本计划只把有新证据的事项标记完成。`code_exists`、`tests_pass`、`local_browser_verified`、
`provider_verified`、`commercially_usable_proven` 和 `production_verified` 保持严格分离。

## 1. 成功标准

本地候选完成必须同时满足：

1. 注册公司、加入公司、私人助手创建/跳过/失败恢复均幂等，跨租户隔离成立；
2. 普通成员、`agent_admin`、`org_admin`、平台管理员的服务端权限和界面入口一致；
3. `Intent → Task → Run → Artifact → Review → Approval → Delivery → Experience` 可刷新恢复，旧证据不会错误指向新产物；
4. Agent、临时专家和 Group 三类执行路径都保留真实责任主体、对象 ID 和失败恢复信息；
5. 图片、视频和 PPT 的工作流支持局部修订与正式交付，但没有真实 Provider/真人盲评证据时不宣称商用；
6. Provider、模型、套餐、降级和故障切换由平台治理，普通用户只选择业务能力；
7. 后端、前端、迁移、合同校验、非付费多角色浏览器矩阵和独立评审通过；
8. 固化新的 immutable candidate SHA，且所有证据绑定该 SHA。

## 2. 本地实施目标

| 目标 | 优先级 | 覆盖场景 | 实施范围 | 完成证据 |
|---|---:|---|---|---|
| R1 身份、Onboarding 与角色合同 | P0 | REG-01/02、AST-01/02/03、AGT-02、ENT-01 | 私人助手幂等、失败恢复、注册路径保持、tenant isolation、四类角色正负权限 | 定向测试、PostgreSQL 并发/约束证据、浏览器身份矩阵 |
| R2 任务恢复与 Group 协作 | P0 | WORK-01/02/03、GRP-01/02、DEL-02 | stale confirmation、重复提交、刷新/断网恢复、Group 参与者终态/部分失败/交接/审批、对象 ID 对照 | exactly-once 断言、读模型一致性、真实本地 Group 流 |
| R3 产物、审批、OKR 与 Experience 生命周期 | P0 | REV-01/02、DEL-01、EXP-01、OKR evidence | Artifact 替换 supersede 旧 review/approval、OKR 证据失效、Experience draft/publish/source-back 权限 | 状态机测试、权限负向、浏览器来源回跳 |
| R4 Provider readiness 治理 | P0 | SUB-01/02、ENT-02、VID-01、IMG-02 | 最后一次真实验证 receipt、能力恢复条件、路由/套餐/降级可解释、secret redaction、本地 Group planning/compact readiness | API/UI 合同、无付费 submit 证据、SaaS 管理页面 |
| R5 创意交付闭环 | P1 | IMG-01/02、VID-01/02、PPT-01/02 | 图片多候选与选择；视频受管首帧、镜头单元与局部重做；PPT 大纲确认、逐页布局与按页修订；保留 v1 兼容 | provider-free 合同测试、Artifact lineage、渲染/结构检查 |
| R6 非付费浏览器与候选冻结 | P0 | 全矩阵可在无付费 Provider 下执行的部分 | desktop/窄视口、双租户、多角色、旧深链、console/network、release identity | 验收记录、截图/对象 ID、完整门禁、独立代码/架构审查、新 SHA |

## 3. 执行顺序与停止条件

### Batch 1 — 身份与权限

先完成 R1。私人助手重复创建、跨租户读取或服务端权限绕过任一存在时，不进入后续候选冻结。

### Batch 2 — 任务与协作

完成 R2。重复提交不得产生第二次 Provider 外发、Credits 预留或 Run；Group 必须等待真实参与者终态，部分失败不得伪装成功。

### Batch 3 — 证据生命周期

完成 R3。Artifact 内容或 hash 变化后，旧 review、approval、delivery/OKR 证据必须失效或明确 superseded。

### Batch 4 — 平台治理与创意修订

先完成 R4 的免费验证合同，再推进 R5。Small 账号的视频能力继续 fail-closed；代码存在不能替代账号资格和真实 Provider 证据。

### Batch 5 — 全量本地验收

执行 R6，修复所有相关回归，运行完整测试、构建、迁移 smoke、合同验证、反冗余清理和两路独立评审，再固化候选 SHA。

## 4. 外部门禁

以下工作不属于本地代码自动授权范围：

| 门禁 | 所需授权/前置 | 通过标准 |
|---|---|---|
| E1 真实 Provider 验证 | 明确费用上限、目标账号与测试模型 | route snapshot、Provider receipt、Artifact、Credits 结算一致 |
| E2 豆包动态 Benchmark | 明确样本与费用授权 | 相同开放输入和交付合同、盲评、失败分类、可复现优化结论 |
| E3 套餐与商业政策 | 产品/财务确认私人助手数量、价格、超限和 Medium+ 权益 | UI、Entitlement、账单和对外说明一致 |
| E4 发布与生产验收 | 推送/发布/生产配置/迁移授权 | release identity 一致、监控健康、生产浏览器业务流通过 |

没有 E1/E2 时可以完成 provider-free 流程和质量门禁，但不得写成“图片、视频、PPT 已达商用”；
没有 E4 时只能标记本地候选，不得写成已发布或生产验证。

## 5. 当前实施状态（2026-08-01 本地证据）

| 目标 | 当前状态 | 已取得证据 | 尚未包含 |
|---|---|---|---|
| R1 身份、Onboarding 与角色合同 | `local_verified` | 私人助手幂等创建/重用、注册路径保持、管理员保护与服务端负向权限合同已进入全量测试；平台管理员登录后可切换租户，导航将“我的助理”和“Agent 员工”分区展示 | 生产身份提供方、生产多租户浏览器验收 |
| R2 任务恢复与 Group 协作 | `local_verified` | Work 草稿跨页面恢复且清理成功；Group 页面保留成员/Agent、会话与 `@` 唤醒入口；Group handoff/planning/task completion 合同进入全量测试 | Docker 不可用，本机未新起真实 Agent 容器执行一轮 Group 任务 |
| R3 产物、审批、OKR 与 Experience | `local_verified` | Work、Agent 对话、交付抽屉、OKR、团队经验库页面边界成立；旧产物 review/approval/evidence supersede 合同与来源回跳进入全量测试 | 生产通知、真实人工评审队列 |
| R4 Provider readiness 治理 | `code_and_local_ui_verified` | SaaS 页面严格区分已配置、账号验证、生成验证、人工质量；文本路由显示 MiniMax-M3 优先，图片/视频/语音显示火山 Agent Plan 主线路，音乐仅 MiniMax；普通 Agent 页面不暴露 Key/Provider | 当前账号 receipt 仍未建立；未做任何付费生成或外部连接验证 |
| R5 创意交付闭环 | `local_artifact_verified` | PPT/图片/视频在 Agent 消息中展示，详情在右侧抽屉；历史执行影子按已验证 Artifact 投影；PPT 可按页、视频可按镜头创建新修订且不覆盖旧版；真实存量文件 hash/size 与数据库一致 | 真实 Provider 新生成、豆包盲评、商用质量结论 |
| R6 非付费浏览器与候选冻结 | `local_verified` | 管理员登录、租户切换、Work、私人助理、Agent 员工、Group、Dashboard、OKR、发现中心、企业设置、订阅、文本/媒体路由、Skill/Tool、PPT/图片/视频交付均已走查；本地 API 代理已固定 IPv4；反冗余检查完成，独立代码与架构审查均为 APPROVE | 普通成员与 `agent_admin` 的完整浏览器负向矩阵；本文件所在 Git commit 作为 immutable candidate SHA |

本地文件证据：

- 图片：PNG，`4096×2304`；
- 视频：H.264 + AAC，`1364×768`，24 fps，`5.875s`；
- PPT：PPTX 8 页、8 个媒体文件；对应 PDF 8 页；
- 四个存量产物均重新从私有不可变快照读取，实际 `sha256` 和字节数与数据库记录一致。

当前完整门禁：后端 `4074 passed`；前端 Node 合同 `96 passed`、Vitest `142 passed`；
creative v1 合同 `87 passed`；Ruff、TypeScript/Vite build、`git diff --check` 和 Alembic 单一 head 均通过。
PostgreSQL fresh upgrade 与 downgrade/upgrade smoke 已通过，升级前本地数据库有效备份已校验。
历史交付 lazy adoption 的真实浏览器验证还确认：生成 Execution/Unit 投影时保留原请求时间，
且本轮验收曾触发的 4 条本地时间变化已按备份中的精确微秒值恢复。

## 6. 最终门禁命令

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

数据库结构变化还要执行 PostgreSQL fresh upgrade 与 downgrade/upgrade smoke。浏览器证据必须记录候选 SHA、
身份/tenant、Task/Run/Deliverable/Artifact ID、结果和仍未验证项。
