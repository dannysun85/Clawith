# 产品线已知问题实施与本地验收记录

> 历史快照：本文保留 2026-07-31 候选冻结前的证据，不再代表当前工作树。当前计划、修复状态和候选门禁以 `docs/product-line/06-browser-business-acceptance-matrix.md`、`07-known-issues-and-execution-baseline.md` 与 `08-remaining-work-master-plan.md` 为准。

## 0. 结论与证据边界

- 日期：2026-07-31（2026-08-01 候选冻结前复核）
- 本地分支：`main`
- Git 起点基线：`dd0b49ad`；本文件随后续 candidate commit 一起冻结，精确 immutable SHA 以冻结后的 `git rev-parse HEAD` 和 SHA-bound 验收记录为准
- Alembic head：`backfill_private_assistant`
- 候选冻结前最高证据层级：`tests_pass`；下述浏览器记录来自冻结前探索，不得代替 candidate SHA 上的新鲜验收
- 未达到：完整 `local_business_flow_proven`、`provider_verified`、`commercially_usable_proven`、`deployed`、`production_verified`

本轮已完成所有可以在不调用付费 Provider、不修改外部账号、不发布生产的前提下安全完成的代码修复。
涉及账号套餐资格、真实 Provider 调用、三人独立质量评审、豆包盲评和生产发布的项目仍是外部门禁，不能用本地代码或历史产物冒充完成。

## 1. 实施结果

### 1.1 能力治理与 Provider 路由

- 新增迁移 `promote_m3_text_primary`：Lite/Pro/Ultra 的文字主路由统一为 MiniMax-M3，火山 Agent Plan 保留为兼容 fallback。
- 迁移保存精确旧状态，处理循环依赖，并支持 downgrade 恢复；部署门禁和迁移图契约同步更新。
- 图片/视频继续遵守“火山优先、MiniMax 非等价降级”的既有正式交付门禁；普通用户不选择 Provider、模型、Skill 或 Tool。
- 当前账号真实视频资格和 Seedance 行为没有重新付费调用；因此只确认代码路由与能力契约，不声明目标 Provider 已验证。

### 1.2 私人助手与 Agent 员工边界

- 私人助手不再占用普通 Agent 员工数量；订阅生命周期和配额统计只排除 onboarding 明确关联的助手，避免按名称误判。
- 导航把 `我的助理` 与 `Agent 员工` 分区；旧 Agent ID 和聊天深链保持不变。
- Onboarding 文案从“第一位员工”改为私人协调者，完成或恢复后进入 `/work`。
- 新增 `backfill_private_assistant` 迁移：只在同 tenant、同创建人且唯一匹配内置 `Private Assistant` 模板时收养旧助手；歧义数据 fail-closed；downgrade 可恢复。
- 浏览器发现并修复一个真实回归：Native Agent 的正常 `idle` 状态此前会被工作台和重试流程拒绝。现在 `running/idle` 均可执行，删除中、已删除、过期等状态仍拒绝。

### 1.3 任务工作台与对象链

- 新增 `/work`，并把租户根入口改为工作台；旧 `/dashboard` 和其他深链继续可用。
- `Task` additive 扩展 tenant、Intent、origin、执行者类型/快照、Group、客户端幂等键和指纹；`DeliverableRequest` 增加 `task_id` 来源。
- 新增服务端 `/api/work` 读模型，聚合 Task、Run、Deliverable、Artifact、Review、Approval 和 Delivery 状态，不复制第二套 Runtime 状态源。
- 支持“我的助理协调 / 指定 Agent 员工 / 临时专家”三类业务选择；临时专家是 Task 级不可变角色快照，不进入员工花名册。
- 工作列表限制为最近 20 条，避免历史交付过多挤压任务输入区。
- 修复幂等重放窗口：按 `task_id` 精确投影旧任务，不再依赖最近 100 条列表。
- 临时专家的角色、仅本任务范围和不继承长期记忆的合同已进入 Runtime goal 和审计 payload，不再只是 UI metadata。
- 工作台图片/视频/PPT 快捷入口明确改为 `task_only` brief；只有建立 `DeliverableRequest` 的项目才标记 `formal_deliverable`，避免普通 Task 虚假承诺正式产物。

### 1.4 发现、经验与导航职责

- `Experience` 增加 `source_task_id` 和 `source_deliverable_request_id`，API 只接受当前用户可访问且 tenant 一致的来源。
- 已交付工作可带来源深链进入经验草稿；编辑/发布过程保留来源。
- `/plaza` 明确拆成 `经验库` 和 `员工市场`，员工市场复用现有 Talent Market 招聘流程。
- 一级导航收敛为：工作台、公司概览、OKR、发现中心、协作群组；Workspace 仍是工作现场，Deliverable/Artifact 仍是正式产物。

## 2. PL-001 至 PL-016 状态

| ID | 代码状态 | 自动化 | 本地浏览器 | 仍需外部证据 |
| --- | --- | --- | --- | --- |
| PL-001 | 已实现 `/work` 默认入口 | 通过 | 工作台和旧 Dashboard 均可进入 | 无 |
| PL-002 | Onboarding 角色/去向已修复 | 通过 | 既有账号恢复路径通过 | 全新公司完整注册流 |
| PL-003 | 助理与员工分组已修复 | 通过 | 旧私人助手已迁移并独立显示 | 窄视口专项 |
| PL-004 | companion slot 与普通员工配额已解耦 | 通过 | 现有账号显示正确 | 免费/定价策略批准 |
| PL-005 | 临时专家 Task 快照与 Runtime 角色合同已实现 | 通过 | 表单选择、校验和按钮可用 | 真实 Provider 执行 |
| PL-006 | Task/Deliverable 来源链已扩展 | 通过 | 工作索引可读取 | 生产数据迁移 |
| PL-007 | 服务端 work index 已实现 | 通过 | 最近工作可见 | 生产规模性能数据 |
| PL-008 | 用户态状态投影已实现 | 通过 | 已正式交付/等待批准/需要处理等分开显示 | 断网恢复实跑 |
| PL-009 | 原 Agent/Group/Artifact 深链保留 | 通过 | 主要入口可进入 | 全链路新任务对象 ID 追踪 |
| PL-010 | Experience 来源和双分区已实现 | 通过 | 经验库、员工市场、人才弹窗通过 | 新建并发布真实经验 |
| PL-011 | MiniMax-M3 文字 Primary 已实现 | 通过 | 管理/普通入口无模型选择暴露 | 真实 Runtime route receipt |
| PL-012 | 正式媒体非等价降级门禁保留 | 通过 | 历史产物可访问 | 故障注入与真实扣费证明 |
| PL-013 | 套餐能力 fail-closed 门禁保留 | 通过 | 未触发 Provider submit | 当前/升级账号真实资格验证 |
| PL-014 | 平台治理与普通用户选择边界保留 | 通过 | 普通工作台只选业务执行者 | 平台管理员路由变更实操 |
| PL-015 | 导航和页面职责已收敛 | 通过 | 所有一级入口可进入 | 普通成员/Agent 管理员角色矩阵 |
| PL-016 | 自动化回归完成，浏览器核心路径完成 | 通过 | 见第 4 节 | 全新租户、负向权限、付费流和生产流 |

## 3. 自动化验证

| 门禁 | 结果 |
| --- | --- |
| 后端全量测试 | `4002 passed, 13 warnings` |
| 后端 Ruff | `All checks passed!`（`backend/app`、`backend/tests` 和本轮新迁移） |
| Agent 能力契约 | `templates=30`、`skills=17`、`tools=140`，校验通过 |
| 创意交付契约 | `82 passed` |
| PostgreSQL 迁移 smoke | fresh upgrade、分段 downgrade/upgrade、并发/幂等/安全 smoke 全部通过 |
| Alembic head | `backfill_private_assistant (head)` |
| 前端 Node 合同测试 | `95 passed` |
| 前端 Vitest | `29 files / 139 tests passed` |
| 前端生产构建 | `7039 modules transformed`，构建成功 |
| Git whitespace | `git diff --check` 通过 |

说明：运行时仍输出第三方依赖 deprecation warnings；本地 WeasyPrint 缺少系统库的提示也仍存在。它不影响上述测试结果，
但会影响依赖 WeasyPrint 的真实 PDF 生成路径，部署前必须补齐运行镜像依赖或使用已验证的替代渲染路径。

## 4. 候选冻结前的浏览器探索（`admin@reeftotem.ai`）

> 证据边界：本节是候选冻结前完成的探索记录，仅证明当时本地运行态。它没有绑定后续 immutable candidate SHA，因此不可作为发布验收证据。冻结后必须在精确 SHA 上重跑非付费矩阵。

本轮只做无付费、非破坏性操作：登录、切换已有公司、浏览、填写但不提交任务、打开已有 Artifact。没有招聘新员工、
没有创建付费媒体任务、没有修改订阅/Provider/企业配置。

| 场景 | 结果 | 证据/说明 |
| --- | --- | --- |
| 工作台 | 通过 | 默认 `/work`；业务快捷入口、三类执行者和最近 20 条工作可见 |
| 私人助手 | 通过 | `私人助理` 单列在“我的助理”，不再出现在员工列表；聊天和 Workspace 入口可用 |
| 临时专家 | 表单级通过 | 角色填写后“开始执行”可用；为避免模型费用未点击提交 |
| 公司概览 | 通过 | 19 名数字员工、任务/Token/活动投影正常加载 |
| OKR | 通过 | 页面、空态、新建目标入口正常 |
| 发现中心 | 通过 | 经验库 → 员工市场 → 人才市场弹窗完整打开 |
| 协作群组 | 入口通过 | `/groups` 恢复到既有 Group/session；未新建或发送消息 |
| 企业配置 | 入口通过 | 公司信息、订阅等设置页可进入；未做写操作 |
| 订阅 | 入口通过 | 套餐详情、Credits、坐席用量可见；未购买/变更 |
| 图片 Artifact | 可访问/可显示 | `f5f7cd81-bcaa-4460-bb78-cd86bef690b1`，16:9 真人产品主视觉 |
| 视频 Artifact | 可访问/可播放 | `50273ed7-dfae-48c1-af01-78590a9fb210`，5 秒竖屏真人持杯视频 |
| PPT/PDF Artifact | 可访问/可预览 | PPTX `447d2a9b-41ea-4ec6-b178-b931f0b00881`；PDF `40e6b72d-712a-40f1-9129-e6a327767615`，5 页、多版式且含视觉图片 |

截图：

- `output/playwright/workbench.png`
- `output/playwright/image-inline.png`
- `output/playwright/video-inline.png`
- `output/playwright/ppt-pdf-inline.png`
- `output/playwright/discovery-talent-market.png`

PDF 直链控制台唯一错误是浏览器请求 `/favicon.ico` 返回 404；PDF 本体正常显示。该错误不影响 Artifact 内容，
但可以在后续前端静态资源整理中消除。

## 5. 仍未完成且不能伪装为完成的项目

1. **Provider 真实验证**：本轮没有再次调用火山或 MiniMax；没有新的 route receipt、扣费和 failover 证据。
2. **商用品质**：现有图片、真人视频和 PPT 已能显示，不代表已达到豆包水平；仍需同输入开放样本、三名独立 reviewer 和盲评阈值。
3. **完整浏览器矩阵**：全新公司注册、普通成员/Agent 管理员负向权限、断网重连、Group 新建协作、审批、经验发布、计费结算尚未逐场景实跑。
4. **发布与生产**：本记录生成时尚未冻结 candidate commit；始终未 push、未发布、未迁移生产数据库、未核对生产 release identity。
5. **本地运行环境**：WeasyPrint 系统库、bubblewrap 隔离能力和非默认生产 secrets 仍应作为发布前置检查。

## 6. 下一安全阶段

1. 完成独立代码/架构复审后冻结 immutable candidate SHA；不要把旧浏览器左下角的 `dd0b49ad` 当成本轮候选版本。
2. 用全新 tenant 和最小角色集合补齐 Batch A/B/D 的非付费浏览器矩阵，记录 Task/Run/Delivery/Experience ID。
3. 获得明确费用上限后，再分别执行火山图片、当前账号可用视频、MiniMax fallback 的受控真实调用与 exactly-once 核对。
4. 使用相同开放需求与豆包做图片/视频/PPT 盲评；达到门槛后才把正式能力标记为商用。
5. 最后单独申请发布授权，执行 preflight、数据库备份、迁移、release identity 对齐、灰度和生产浏览器验收。
