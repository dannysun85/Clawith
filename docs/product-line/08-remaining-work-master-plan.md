# 产品线剩余工作总计划

## 0. 计划基线

- 日期：2026-08-03
- 已部署基线：`v1.11.17` / `1286865f08a9b09ab4f3bccfd2875f08fd990b15`
- 当前实现状态：`main` 位于已部署基线，含未提交的下一切片角色分类；尚未形成新的 immutable candidate SHA
- 分支：`main`（仅本地，未推送、未发布）
- 事实来源：`docs/product-line/01-09`、当前代码、测试、迁移与既有本地浏览器证据
- 总目标：把“私人助手是默认任务入口、Agent 是长期执行员工、Deliverable 是正式产物、Workspace 是工作现场”落实为可恢复、可审计、可验收的完整产品链路。

本计划只把有新证据的事项标记完成。`code_exists`、`tests_pass`、`local_browser_verified`、
`provider_verified`、`commercially_usable_proven` 和 `production_verified` 保持严格分离。

## 1. 成功标准

新的本地候选完成必须同时满足：

1. 注册公司、加入公司、私人助手创建/跳过/失败恢复均幂等，跨租户隔离成立；
2. 普通成员、`agent_admin`、`org_admin`、平台管理员的服务端权限和界面入口一致；
3. `Intent → Task → Run → Artifact → Review → Approval → Delivery → Experience` 可刷新恢复，旧证据不会错误指向新产物；
4. Agent、临时专家和 Group 三类执行路径都保留真实责任主体、对象 ID 和失败恢复信息；
5. 图片、视频和 PPT 的工作流支持局部修订与正式交付，但没有真实 Provider/真人盲评证据时不宣称商用；
6. Provider、模型、套餐、降级和故障切换由平台治理，普通用户只选择业务能力；
7. 后端、前端、迁移、合同校验、非付费多角色浏览器矩阵和独立评审通过；
8. 固化新的 immutable candidate SHA，且所有证据绑定该 SHA；当前工作树状态不满足此项。

## 2. 本地实施目标

| 目标 | 优先级 | 覆盖场景 | 实施范围 | 完成证据 |
|---|---:|---|---|---|
| R1 身份、Onboarding 与角色合同 | P0 | REG-01/02、AST-01/02/03、AGT-02、ENT-01 | 私人助手幂等、失败恢复、注册路径保持、tenant isolation、四类角色正负权限 | 定向测试、PostgreSQL 并发/约束证据、浏览器身份矩阵 |
| R2 任务恢复与 Group 协作 | P0 | WORK-01/02/03、GRP-01/02、DEL-02 | stale confirmation、重复提交、刷新/断网恢复、Group 参与者终态/部分失败/交接/审批、对象 ID 对照 | exactly-once 断言、读模型一致性、真实本地 Group 流 |
| R3 产物、审批、OKR 与 Experience 生命周期 | P0 | REV-01/02、DEL-01、EXP-01、OKR evidence | Artifact 替换 supersede 旧 review/approval、OKR 证据失效、Experience draft/publish/source-back 权限 | 状态机测试、权限负向、浏览器来源回跳 |
| R4 Provider readiness 治理 | P0 | SUB-01/02、ENT-02、VID-01、IMG-02 | 最后一次真实验证 receipt、能力恢复条件、路由/套餐/降级可解释、secret redaction、本地 Group planning/compact readiness | API/UI 合同、无付费 submit 证据、SaaS 管理页面 |
| R5 创意交付闭环 | P1 | IMG-01/02、VID-01/02、PPT-01/02 | 图片多候选与选择；视频受管首帧、镜头单元与局部重做；PPT 大纲确认、逐页布局、图片页分布和按页修订；保留 v1 兼容 | provider-free 合同测试、Artifact lineage、渲染/结构检查 |
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
| E3 套餐与商业政策 | 产品/财务确认私人助手数量、价格、超限和 Large/Max 视频权益 | UI、Entitlement、账单和对外说明一致 |
| E4 发布与生产验收 | 推送/发布/生产配置/迁移授权 | release identity 一致、监控健康、生产浏览器业务流通过 |

没有 E1/E2 时可以完成 provider-free 流程和质量门禁，但不得写成“图片、视频、PPT 已达商用”；
没有 E4 时只能标记本地候选，不得写成已发布或生产验证。

## 5. 当前实施状态（更新至 2026-08-04 本地证据）

| 目标 | 当前状态 | 已取得证据 | 尚未包含 |
|---|---|---|---|
| R1 身份、Onboarding 与角色合同 | `local_browser_verified_normal_state + production_problem_verified` | `v1.11.17` 已上线原有“我的助理/Agent 员工”边界；当前本地切片新增 viewer-specific `product_role`、历史助理独立分组和不可招聘 Private Assistant 模板，保留旧 ID/会话/Workspace/深链；全量自动化、租户 API 和当前本地数据的真实浏览器验收已通过 | 本地缺少历史助理 fixture，仍需验证历史分组视觉；全新 tenant、普通成员/`agent_admin` 在最终候选上的再次登录；历史助理显式归档/转员工流程 |
| R2 任务恢复与 Group 协作 | `local_verified` | Work 草稿跨页面恢复且清理成功；Group 页面保留成员/Agent、会话与 `@` 唤醒入口；Group handoff/planning/task completion 合同进入全量测试 | Docker 不可用，本机未新起真实 Agent 容器执行一轮 Group 任务 |
| R3 产物、审批、OKR 与 Experience | `local_verified` | Work、Agent 对话、交付抽屉、OKR、团队经验库页面边界成立；旧产物 review/approval/evidence supersede 合同与来源回跳进入全量测试 | 生产通知、真实人工评审队列 |
| R4 Provider readiness 治理 | `local_provider_verified` | SaaS 页面严格区分已配置、账号验证、生成验证、人工质量；文本路由显示 MiniMax-M3 优先，图片/视频/语音显示火山 Agent Plan 主线路，音乐仅 MiniMax；2026-08-04 控制台确认 Large，本地同步为 `text/image/audio/video`，并为 Seedream、标准 Seedance 2.0、Seed TTS 取得 hash 绑定真实 Artifact | 最后一次真实验证回执的产品化持久展示；生产配置与真实调用验证；独立人工质量评审 |
| R5 创意交付闭环 | `local_real_artifact_and_flow_verified` | 火山图片、1080×1920 有声 Seedance 2.0 视频、语音和 6 页 PPTX/PDF 已真实生成；交付卡位于 Agent 消息区，详情在右侧抽屉，PDF 可预览、PPTX 可下载；同题类豆包样本已做实操缺陷对照 | 三人独立盲评、滚动真实客户样本、商用质量结论与生产验收 |
| R6 非付费浏览器与候选冻结 | `local_business_flow_verified_with_evidence_gap` | 上一候选 `cc6affe7` 的管理员 release identity 和复审证据可追溯；当前工作树已完成本地真实产物展示、媒体路由防错、平台管理员到公司工作区切换和回归测试，但含未提交变更 | 新候选的前端/迁移/浏览器收口、immutable SHA 绑定；普通成员/`agent_admin` 需在新候选上复验 |

### 2026-08-03 角色边界切片新鲜证据

- 生产 `v1.11.17` / `1286865f08a9b09ab4f3bccfd2875f08fd990b15` 只读页面仍显示当前“我的助理 · 小丽”和员工区内的旧“私人助理”，证明问题来自真实历史对象，不是凭空设计。
- 当前未提交工作树以 onboarding 关系识别当前 assistant，以内置 `private-assistant` 模板身份识别历史 assistant；前端不读取名称或可编辑 `role_description` 判断产品角色。历史对象只改变分组和员工计数，原 ID、会话、Workspace、权限与深链保持不变。
- 本地租户 API 返回 19 个可见 Agent：1 个 `personal_assistant`、18 个 `agent_employee`、0 个未标注角色；当前本地数据没有历史助理 fixture。员工市场返回 0 个 `private-assistant` 可招聘模板，证明未来重复创建入口已关闭。
- 新鲜门禁为后端 `4240 passed, 13 warnings`，前端 Node `109 passed`、Vitest `31 files / 162 passed`，生产构建 `7087 modules` 成功；Ruff、`git diff --check`、Agent 能力合同与六模态能力矩阵均通过。能力矩阵本身仍是 provider-free 注册检查；独立的 hash 绑定回执记录了本轮受控火山图片、视频和语音真实调用，二者不能互相替代。
- 本地源码运行时 `/api/health=200`，`/api/version` 为 `1.11.17`、基础 commit `1286865f`、release id `local-dirty-product-roles-20260803`，Alembic 为 `backfill_private_assistant_tpl (head)`。该 release id 明确表示未提交工作树，不是 immutable candidate。
- 使用 `admin@reeftotem.ai` 通过真实本地登录页进入平台控制台，并由可见的公司选择器切换到目标公司；没有读取或输出浏览器令牌。
- 本地真实浏览器已验证：侧栏仅在“我的助理”下显示当前“私人助理”；任务工作台切换到“指定 Agent 员工”后出现 18 个候选且不含私人助理；公司概览显示“共 18 名数字员工”；人才市场正常打开，热门推荐显示 15 个“聘用”入口且全文不含 `Private Assistant`/“私人助理”；当前助理 `/agents/719c8437-043d-410a-94bd-7b56dcfb952b/chat` 深链、会话和 Workspace 继续可达。页面没有来自 `http://127.0.0.1:3008` 的 console warning/error。
- 当前本地租户没有历史助理 fixture，所以“历史助理（N）”的实际视觉分组仍未在本地浏览器中出现；该分支已有自动化合同覆盖，生产只读证据则确认真实旧对象确实存在。当前状态可以写成 `local_browser_verified_normal_state + production_problem_verified`，但仍不得写成历史数据迁移已完整验收、immutable candidate、已发布或生产已修复。
- 本次浏览器验收只进行了登录、公司切换、页面读取和招聘市场查看；没有聘用 Agent、发送消息、创建任务或触发任何付费 Provider 调用。

本地文件证据：

- 图片：PNG，`4096×2304`；
- 视频：H.264 + AAC，`1364×768`，24 fps，`5.875s`；
- PPT：PPTX 8 页、8 个媒体文件；对应 PDF 8 页；
- 四个存量产物均重新从私有不可变快照读取，实际 `sha256` 和字节数与数据库记录一致。

本轮新鲜门禁为：后端 `4192 passed, 13 warnings`；creative v1 合同 `94 passed`；前端 Node 合同 `107 passed`、
Vitest `31 files / 158 passed`，生产构建 `7087 modules` 成功；Agent 能力合同为 `30` 个模板、`17` 个 Skill、
`140` 个 Tool；Ruff 与 `git diff --check` 均通过。真实公司租户浏览器会话再次确认 `/work`、`/groups`、
`/account` 与 `/enterprise#skills` 的入口、Group @ 唤醒、账号掩码和媒体能力治理；该次 2026-08-02 历史快照中的 Small Agent Plan 缺少火山视频主线路，
但聊天中的快速视频快捷入口会在存在 MiniMax fallback 时可用，正式交付仍需显式允许降级。无新增付费 Provider 调用。Benchmark 审计已生成图片/视频/PPT 的候选哈希、观察事实和
reviewer 模板，状态仍为 `awaiting_human_review`，不能写成商业就绪。上一候选的数据库 smoke、独立复审、
重启与管理员 identity 仍可追溯，但当前工作树有未提交变更，不能写成“当前候选多角色浏览器全部通过”。
历史交付 lazy adoption 的真实浏览器验证还确认：生成 Execution/Unit 投影时保留原请求时间，
且本轮验收曾触发的 4 条本地时间变化已按备份中的精确微秒值恢复。

本轮新增的 provider-free 能力矩阵校验为：
`cd backend && .venv/bin/python scripts/validate_multimodal_capability_matrix.py --json`，
并已接入 `validate_agent_capabilities.py`。当前输出覆盖 `text/image/video/voice/music/presentation` 六项，
均为 `ready`；它逐项核对入口模板、角色 Skill、Tool 注册、typed runtime adapter 和默认/显式授权路径，
同时明确 `provider_health_verified=false`。这证明本地注册和授权合同没有漂移，不证明账号套餐、真实 Provider
生成、人工质量或生产路由已经通过。

本轮又修复了该校验脚本的机器输出边界：WeasyPrint 原生依赖缺失时的导入诊断不再污染
`--json` 的 stdout；现在 stdout 可直接交给 `json.loads`，诊断保持在导入隔离范围内。新增 CLI 回归与相关文档转换
定向测试共 `14 passed`，不改变 PPT/PDF 的运行时降级语义。

在上述门禁基础上又新增两条运行时路由防错回归：火山 Agent Plan `Small` 套餐在视频请求时必须在 Provider I/O
之前 fail closed，通用/旧版火山凭证不能被重新解释为 Agent Plan。火山适配器定向测试当前为 `30 passed`，与媒体
能力测试合计 `45 passed`；这些测试均不调用外部 Provider。

本轮又把 reviewed provider/model route policy 纳入同一组无 Provider 校验：
`validate_media_route_policy()` 固定核对图片、视频、语音的运行时顺序为
`volcengine_agent_plan -> minimax`、音乐为 `minimax-only`，并核对 Small/Medium 不接收新视频任务、
Large/Max 使用 `doubao-seedance-2.0`，Fast/Mini 只允许在合资格套餐内由管理员策略显式选择，
且退休的 `doubao-seedance-1.5-pro` 不得进入新套餐映射。该校验只验证代码策略没有漂移，不探测密钥、套餐额度或
Provider 生成；本轮定向测试 `49 passed`，能力矩阵与 Agent 能力脚本均返回 `ready/valid`。

本轮又修正了火山适配器的 Seedance 1.5 Pro provider ID：新任务从错误的
`doubao-seedance-1-0-pro-250528` 改为官方 `doubao-seedance-1-5-pro-251215`；旧 ID 仅保留为入站兼容别名，
用于读取已持久化任务/回执，不能被新任务提交。针对别名、请求 payload、能力和媒体路由的定向回归为
`60 passed`，`ruff` 与 `git diff --check` 均通过；这仍属于 provider-free 合同验证，不等于真实视频生成成功。

本轮同时执行了一次本地 Provider readiness 的脱敏只读检查（未打印密钥、未修改数据库、未调用外部 Provider）。
数据库状态为：当时启用账号池有 3 条记录，其中 2 条为已验证且健康（MiniMax 1 条、火山 Agent Plan 1 条），另有 1 条
MiniMax `video` 专用记录仍为 `unverified`，不会进入运行时 verified pool；健康 MiniMax 的已验证媒体能力为
`audio/image/music/video`，火山 Agent Plan `Small` 的已验证媒体能力为 `audio/image`。因此当前本地运行时可以
证明当时的账号验证和能力边界，也不能把账号验证 receipt 当成真实生成或
商业质量证据；按这组已验证能力计算出的路由状态为：图片 `available`（火山 + MiniMax）、视频 `degraded`
（仅 MiniMax，应急路线）、语音 `available`（火山 + MiniMax）、音乐 `available`（MiniMax-only）。视频主线路
在当时仍需具备相应套餐/模型资格的火山账号后，再按 E1 做付费生成验证；2026-08-04 的 Large 升级证据已在上表和后续复验条目覆盖该资格缺口。

随后又对当前本地数据库做了第二次只读注册核验：共 `32` 个 Agent，`compose_video_audio` 已注册、默认启用，
分配到 `29` 个 Agent 且全部启用；`brand-safe-media` 已注册并同步到 `40` 个模板，同时包含旁白广告流程和
同步对白的音频门禁。该结果证明 Skill/Tool 授权链仍在当前数据库中生效，不代表 Provider 额度或媒体质量已通过。

文字与媒体优先级又通过本地 `resolve_route`/Provider-order 只读快照复核：Lite/Pro/Ultra 的聊天文字主路由均为
`minimax/MiniMax-M3`，对应火山 Agent Plan 文字模型分别为 `doubao-seed-2.0-mini`、
`doubao-seed-2.1-turbo`、`doubao-seed-evolving` 的故障切换；图片、视频、语音的运行时 Provider 顺序均为
`volcengine_agent_plan -> minimax`，音乐为 `minimax-only`。这证明当前本地路由策略与产品目标一致，但仍不证明
两条线路都已完成真实生成、额度可用或质量达标。

2026-08-02 17:30 又做了一次新鲜的只读数据库核验：`alembic current` 返回
`backfill_private_assistant_tpl (head)`；`model_routes` 中 Lite/Pro/Ultra 的文字主路由均为
`minimax/MiniMax-M3`（priority `951`），其下一跳分别指向火山 Agent Plan 文字路由（priority `950`），
没有发现跨模态回退链。账号池脱敏快照仍为 3 条启用记录：健康且有当前验证回执的 MiniMax 全媒体账号 1 条、
健康且有当前验证回执的火山 Agent Plan `small` 账号 1 条（仅 `audio/image`）、未验证的 MiniMax `video`
应急记录 1 条；因此视频仍只能显示 `degraded`，不能因为代码路由存在就宣称火山视频资格已经具备。
本次针对路由完整性的回归在新增“MiniMax-M3 文字主路由 → 火山 Agent Plan 同档位 fallback”合同后为 `166 passed`，能力矩阵和 Agent 能力脚本均返回 `ready/valid`；这些证据仍属于
本地代码/数据库状态，不等同于真实 Provider 生成或生产环境状态。

指定本地 Agent `b4f0f5d8-4fb8-40d1-b10c-3fb7bdab6864`（Pro）再次读取运行时媒体能力视图：图片、语音、音乐为
`available`；视频为 `degraded`，`available_providers=["minimax"]`，原因码为
`commercial_primary_unavailable`，界面动作提示为等待火山主线路恢复或明确确认应急质量。这证明 Agent 端的能力
展示与账号池/套餐事实一致，而不是只在配置页面显示“支持视频”。

本轮又把账号套餐解释透传到 Agent 能力状态：当火山 Agent Plan 的已配置套餐只有 `plan=small` 且视频被套餐能力
过滤时，能力仍标记为 `degraded`，但聊天中的快速视频按钮允许插入需求，由平台自动选择 MiniMax fallback；Provider 和
套餐细节不泄露给普通用户。该行为不改变 `commercial_primary_unavailable` 门禁，正式交付仍必须显式允许降级。

### 本轮本地真实浏览器证据（2026-08-02）

使用当前工作树启动 `./restart.sh --source`，本地 PostgreSQL 已就绪，后端 `8008`、前端 `3008` 和 Vite API
proxy 均通过健康检查。通过本地测试账号 `admin@reeftotem.ai` 登录后，先验证平台管理员上下文，再通过已授权的
公司切换接口进入 `深圳前海瑞孚图腾科技有限公司`；切换响应只在浏览器本地会话中使用，未输出或保存令牌到证据。

在同一公司上下文中，Playwright 只读浏览器验收结果如下：

| 入口 | 结果 | 可见事实 |
|---|---|---|
| `/work` | `200` | 默认任务工作台显示“私人助理”、Agent 员工、临时专家、Group 协作、普通/图片/视频/PPT/报告任务类型以及正式交付列表 |
| `/dashboard` | `200` | 公司概览、数字员工、活动任务和 Token 摘要可读 |
| `/plaza` | `200` | 经验库与员工市场入口可读 |
| `/groups` | `200`，自动回到已有 Group 会话 | Group 成员、Agent、会话和 `@` 唤醒消息可读 |
| `/enterprise` | `200` | 公司设置、审批、Tools、Skills、OKR、邀请、配额和用户入口可读 |
| `/okr` | `200` | 当前季度、公司目标及完成状态可读 |
| `/account/subscription` | `200` | Scale 套餐、Credits、Seats 和消耗明细可读 |
| `/account` | `200` | 账号池页面显示 Provider/模态、掩码 Key 和鉴权状态，不显示明文密钥 |
| `/agents/b4f0f5d8-4fb8-40d1-b10c-3fb7bdab6864/chat` | `200` | Agent 聊天、历史媒体/PPT 会话、Workspace 文件树和交付抽屉可读；Image/Speech/Music/Video 均可插入需求，Video 快速生成由平台自动选择 fallback，正式 Deliverable 仍按 `degraded` 门禁 |

在上述证据之后，本轮又使用隔离的无头 Chromium 对**当前工作树**重新登录并逐一打开上述九个路由；所有页面最终 URL 均保持在本地同源、标题为 `Astra`，没有发生 `5xx` 响应。同期 `/api/health` 返回 `200`，`/api/version` 返回版本 `1.11.14`、commit `1a81291a`、空 `release_id`。这补齐了当前工作树的只读页面可达性证据，但仍不等价于新 immutable candidate、真实生成或生产验收。

该矩阵证明当前候选的登录、公司上下文切换、主要产品入口和媒体治理 UI 可用；它不证明真实 Provider 生成、人工
质量评审、豆包 Benchmark 或生产环境可用。浏览器只读验收未发送新消息、未创建新交付、未上传文件、未触发图片/视频/
语音/音乐生成，也未改变远程环境。内置 Codex 浏览器的 webview 未附着，因此本轮浏览器证据由本机 Playwright
Chromium 产生；Chromium 自检通过。

随后在同一公司上下文的 `/work` 中填入一个不涉及媒体的私人助理任务并执行“检查工作说明”（不点“确认并开始执行”），
页面生成了待确认工作说明：执行者为“私人助理”、状态为“执行者可用”、交付边界为“可直接查看的任务结果”，并明确费用按实际
用量结算、正式媒体生成仍需单独预检。这证明 `Intent -> Task` 的本地确认门已连通，同时没有触发文字 Provider、扣除 Credits
或创建运行记录；完整的 `Task -> Run` 仍需获得 Provider 调用授权后再执行。

本轮另以专用本地 smoke 身份执行了注册业务流（邀请码开关、无邀请码拒绝、非法邀请码拒绝、有效邀请码注册、一次性
消费、UI 注册码字段可见），API+UI 全部通过，测试结束已恢复邀请码开关并停用临时邀请码。由于本地没有 SMTP，流程只在
临时开发进程中开启 `ALLOW_UNVERIFIED_LOCAL_SIGNUP=true` 后执行，随后已重启恢复默认关闭；这不是生产配置，也不代表
生产注册链路的邮件投递已验证。该 smoke 还暴露出当前默认本地进程在无 SMTP 时会按安全策略返回 `503`
（`Password registration is temporarily unavailable`），因此新候选的无 SMTP 注册验收必须显式标记为配置前置条件。

同一 smoke 注册用户随后通过真实本地浏览器完成登录；由于没有公司成员关系，访问 `/work` 被正确守卫并重定向到
`/setup-company`，页面明确要求管理员邀请码。该结果证明“注册成功 ≠ 已进入工作台”的租户边界仍然生效；它没有替代
“平台管理员创建公司 → 邀请用户加入 → 私人助手初始化 → 首次任务”的完整新租户验收，后者仍列为 R1/R6 的未完成矩阵。

补充的一次只读复验进一步核对了指定 Agent 的能力 API 和聊天按钮：媒体能力接口返回 `200`，图片与语音均为
`available` 且按实际运行时顺序列出火山 Agent Plan → MiniMax，音乐为 MiniMax-only，视频为 `degraded`、仅列出 MiniMax，原因码为
`commercial_primary_unavailable`。页面上的图片、语音、音乐、视频按钮均可用并提示“填写需求后再发送”；视频按钮的
快速路径由平台自动选择 fallback，正式交付仍提示质量检查和降级确认。该复验仍未触发任何生成或 Provider 外发，证明的是能力治理与
产品门禁的一致性，而不是媒体质量或商业可用性。

本轮又用 `seed=20260802` 生成了一套隔离的开放场景评测输入（保存于本机 `/private/tmp`，不进入仓库）：
公开 manifest 18 条、restricted holdout 6 条，图片/视频/PPT 各 6 条，覆盖 8 个行业、多语言和多画幅；
holdout commitment `20af14edf50827c3bcde339012286be7256201183e37571cf99a31ff707ca457` 校验通过，文件权限为
`0600`，公开 manifest 未发现 Provider、模型或本地路径泄漏。该证据只证明 Benchmark 输入的随机化、隔离和可
复现性，不代表已完成真实生成、质量评分或商业放行。

本轮继续审计同一运行包得到稳定的 `run_fingerprint_sha256=582d3f4b43bbf1b93e2e6274b029f20b6d32045c2a505b284ffc73f9c76059d6`，
图片/视频/PPT 分别仍为 `awaiting_human_review`（候选哈希与 3 份评审模板齐备，正式评审结果为 0）。候选冻结脚本
按当前工作树再次拒绝 `source repository has uncommitted changes`；这证明发布前门禁仍在 fail-closed，而不是把脏工作树或
未评审样本误报为候选或商用就绪。

2026-08-02 又对同一运行目录执行了 `audit_creative_benchmark_run.py` 的新鲜复核，输出
`/private/tmp/clawith-benchmark-current-20260802/audit-latest-goal-continuation-2.json`（此前的
`audit-latest-goal-continuation.json` 与 `audit-latest-final-rerun.json` 仍保留）；指纹和上述结果保持一致，
`issues=[]`，三类模态的 `formal_result_count=0`、`commercially_usable_count=0`。该复核只读取本地候选，
没有调用 Provider、没有产生费用，也没有改变 Benchmark 文件或生产环境。

本轮又用当前完整 HEAD 重新运行候选冻结脚本，输出
`/private/tmp/clawith-benchmark-current-20260802/candidate-manifest-goal-continuation.json`，结果为
`rejected: source repository has uncommitted changes`；这再次确认脏工作树不能被误当成 immutable candidate。

### 补充：图片主导型 PPT 覆盖率门禁

为修复“PPTX 结构完整但图片只是小卡片，实际画面很空”的质量缺口，当前候选新增统一视觉策略：

- 只对图片主导型 brief（例如“图文并茂 / 人物广告 / 故事板 / photography”）启用；文字、数据和纯编辑型 deck 不受影响。新增了组合语义识别：当产品/品牌 brief 同时包含上市、广告、外观、交互、渠道物料等视觉意图时，即使没有写出“图文并茂”，也必须进入图片主导型合同。
- 同一识别逻辑同时用于 Prompt、PPT 源合同、最终 PPTX 复核和启动前图片能力预检，避免 Benchmark 这类“产品上市方案 + 三镜头脚本”被误判为文字型 deck。
- Agent 的 `PRESENTATION_VISUAL_POLICY`、HTML 源合同和最终 PPTX 结构核验共用 `minimum_picture_coverage_ratio=0.35`。
- 最终 PPTX 以每页图片可见几何面积计算全 deck 平均覆盖率；低于阈值时，PPTX 与配套 PDF 均不能进入 candidate/approval。
- 失败会写入稳定错误码 `presentation_picture_coverage_below_minimum`，交付抽屉展示可执行的修订提示，而不是只显示通用转换错误。
- 本地现有评审包复测（匿名标签 A=豆包对照、B=Astra 本地）：候选 A 为 `0.070771`，门禁失败；候选 B 为 `1.000000`，门禁通过。该结果只证明结构性覆盖率，不证明语义、审美、无水印或商业可用性。
- 本轮对评审包中当前路径又做了一次独立结构复测：本地 image-led PPT 的 `observed_mean=1.000000`、覆盖率门禁通过；豆包对照 PPT 的 `observed_mean=0.070771`、覆盖率门禁失败。两份文件的 PPTX/PDF、页数/画幅和可编辑性结构门禁均通过；该复测不改变人工评审未完成的状态，也不把覆盖率当成商业质量结论。
- 2026-08-02 又用 `inspect_creative_artifacts.py` 对同一评审包做了无 Provider 的独立复核：3 个图片候选均通过解码与 `9:16` 画幅，2 个视频候选均通过解码、`9:16`、约 10 秒和 AAC 音频；图片/视频的 `fact_safety` 与 `no_unrequested_watermark` 仍为待人工判定。豆包 PPT 的 `minimum_picture_coverage=false`，Astra 本地 PPT 为 `true`；两者的 PPTX/PDF、8 页、`16:9` 和可编辑性均通过。复核输出保存在 `/private/tmp/clawith-benchmark-current-20260802/`，批次审计仍为 `awaiting_human_review`，正式评审结果为 0。
- Benchmark 审计现在同时绑定批次中的 `scenario` 与公开评审包的 `brief`、`requirements`、`hard_gates`、`quality_dimensions` 及 Artifact contract 的模态/画幅；只改 `scenario_id` 而替换评审合同会被判为 `invalid`，避免“同一任务 ID、不同实际任务”的误报。
- 审计输出还生成独立的 `run_fingerprint_sha256`，由批次、匿名包、Artifact、评审目录和私有归因键内容组成；它用于识别 Benchmark 运行是否被替换，不等同于 Git immutable candidate SHA，也不暴露 Provider 身份。
- Provider Benchmark receipt 现在额外记录 `benchmark_plan_sha256` 与 `benchmark_case_sha256`；火山/MiniMax 的真实生成结果若不绑定同一计划和 case 内容，不能进入后续盲评批次。
- `creative_provider_benchmark.py` 现在在凭据选择和 Provider 外发之前强制执行计划内的成本护栏与逐次人工授权：逐 Provider、逐图片/视频 case 的成功生成次数达到上限即 `cost_guardrail_exhausted`，`automatic_quality_retries` 非 0 时直接拒绝，且未传 `--confirm-paid-provider-call` 时以 `explicit_paid_provider_call_authorization_required` fail closed；成功/失败 receipt 都记录护栏快照，避免重复运行或误运行隐性消耗额度。
- 新增 `backend/scripts/freeze_creative_benchmark_candidate.py`：只有三类都达到 `commercial_ready`、所有评审结果已封存、源仓库干净且 `source_revision` 等于当前 HEAD 时才允许写入 candidate manifest；当前 `awaiting_human_review` 和脏工作树会 fail closed。

### 2026-08-02 视觉复核补充（非正式评审）

- 本轮重新查看了评审包联系图，并重新运行无 Provider 的 Artifact 结构检查；复核产物写入
  `/private/tmp/clawith-benchmark-current-20260802/`，不进入仓库，也没有新增 Provider 调用。
- 图片：火山/MiniMax/既有豆包候选均通过文件解码和 `9:16` 画幅门禁；当前联系图中火山候选的光影和广告构图可继续评审，MiniMax 候选的产品与手部精细度偏弱，当前浏览器豆包样本构图较稳定但带有 `AI生成` 水印。三者的 `fact_safety` 与 `no_unrequested_watermark` 仍必须由正式评审判定，不能只凭联系图放行。
- 视频：本地 MiniMax 候选通过 H.264/AAC、约 10 秒和 `9:16` 门禁，但帧联系图显示人物发型/外观跨镜头漂移，开盖交互和镜头连续性不足，不能作为商用广告交付；豆包对照样本的叙事和人物/产品连续性更好，但存在可见生成水印，仍不能直接放行。
- PPT：本地 image-led 候选通过 `minimum_picture_coverage_ratio=0.35`（观察均值 `1.000000`），对照候选的观察均值为 `0.070771` 并被覆盖率门禁拒绝；两者的 PPTX/PDF、8 页、`16:9` 和可编辑性结构均通过。覆盖率只证明图片布局底线，不能替代溢出、事实、来源和审美评审。
- 因此本批次仍保持 `awaiting_human_review`，正式结果为 `0`；下一步是按同一量表完成独立盲评并补齐水印/事实/连续性/可交付性结论，而不是把机器结构结果升级为 `commercially_usable_proven`。

### 5.1 目标边界（必须保持）

当前 active goal 是本地候选实现与验证目标：代码、迁移、Skill/Tool 治理、Provider 路由、
本地真实业务流和 Benchmark 证据均在本地工作树中完成。远程生产环境不是当前候选的写入目标，
也没有发生远程配置同步、部署、迁移或生产数据修改。真实 Provider 生成、豆包动态 Benchmark、
immutable candidate SHA 和发布/生产验收分别属于 E1、E2、E4 外部门禁；在获得相应授权并完成证据前，
只能标记本地状态，不得写成 `provider_verified`、`commercially_usable_proven` 或 `production_verified`。

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

### 2026-08-02 全量回归续验（当前 Goal）

- 后端全量回归：`backend/.venv/bin/pytest -q`，本次新鲜复跑为 `4213 passed`，仅保留依赖库弃用警告。
- 续验时本地运行时仍返回 `/api/health=200`、版本 `1.11.14`、commit `1a81291a`、空 `release_id`；能力注册脚本仍为 `ready/valid`，但 `provider_health_verified=false` 保持不变。这是本地运行时证据，不是远程发布或真实 Provider 生成证据。
- 按模态重新生成了只读 parity 审计：图片要求 `minimax + volcengine_agent_plan + doubao`，视频要求当前可用的 `minimax + doubao`，PPT 只要求同一 plan/case。三个输出分别为 `/private/tmp/clawith-benchmark-current-20260802/benchmark-parity-image-20260802.json`、`benchmark-parity-video-20260802.json`、`benchmark-parity-presentation-20260802.json`，均为 `status=valid` 且所有 Artifact hash 校验通过；视频未把当前火山 Small 账号伪装成可用 Provider。
- 前端回归：`pnpm --dir frontend test` 通过（Node `107 passed`、Vitest `158 passed`）；`pnpm --dir frontend build` 的 TypeScript 检查与 Vite 生产构建均通过。
- 质量门禁：目标多模态模块的 Ruff 检查与 `git diff --check` 均通过。
- 注册/授权矩阵：`validate_multimodal_capability_matrix.py` 输出 `status=ready`、`errors=[]`，六类能力均 ready；`provider_health_verified=false` 仍被明确保留，不能当作真实 Provider 生成证据。
- Benchmark 新鲜审计：`/private/tmp/clawith-benchmark-current-20260802/audit-goal-continuation-live-20260802202103.json`，指纹仍为 `582d3f4b43bbf1b93e2e6274b029f20b6d32045c2a505b284ffc73f9c76059d6`；图片 3/3、视频 2/2、PPT 2 候选及 4 个配套文件的结构证据有效，但三类均为 `awaiting_human_review`，正式评审与商用结果均为 `0`。
- 本轮没有调用真实 Provider、没有消耗 Credits、没有同步远程配置，也没有发布版本。当前工作树仍未形成 immutable candidate；候选冻结继续对脏工作树和未封存评审 fail closed。
- 追加预检确认：本机真实账号池包含火山 Agent Plan `small`（`text/image/audio`）与 MiniMax（`text/multimodal/image/audio/video/music`）；火山视频路线在 Provider 外发前以 `capability_mismatch` 拒绝，MiniMax 视频 fallback 可选。尝试启动一份受成本护栏约束的火山图片 Benchmark 时被外部执行审批拒绝，未发出请求、未产生费用；后续必须由用户明确授权“本轮 1 次火山图片 + 1 次 MiniMax 视频真实生成”后才能进入 E1/E2。
- 本地凭证池只读复核（2026-08-02）：火山 Agent Plan 账号状态为 `healthy`、`plan_tier=small`、能力为 `text/image/audio`；MiniMax Token Plan 状态为 `healthy`、能力覆盖 `text/multimodal/image/audio/video/music`。这只是本地凭证健康/资格快照，不等同于本轮 Provider 生成 receipt，也不改变 `provider_health_verified=false` 的 Benchmark 门禁。
- 本地角色入口短路径浏览器复验（2026-08-02）：`member` 登录后 `/work` 可用，访问 `/enterprise` 正确回到 `/work`，`/account/subscription` 可用；`org_admin` 登录后 `/enterprise` 与 `/invitations` 可用，访问 `/account` 正确回到 `/work`，`/account/subscription` 可用。两种身份均无页面异常或 5xx；临时身份和租户已清理。该证据补齐角色路由，不代表新租户注册、Agent 招聘、Group 协作或真实媒体 Provider 已验收。
- E1 成本/资格预检（2026-08-02）：以 `video_smart_thermos` 调用 `creative_provider_benchmark.py` 的火山视频路径并显式指定 `doubao-seedance-2.0-mini`，本地凭证在 Provider I/O 前因 `capability_mismatch` fail closed，退出码 `2`，回执 `provider_accepted=false`、`artifact_path=null`。这证明当时 Small 账号不会被误发视频请求，也没有产生费用；它不是视频生成失败。2026-08-04 控制台确认账号升级为 Large，本地元数据同步为 `large + text/image/audio/video`，标准 `doubao-seedance-2.0` 随后真实生成 1080×1920 H.264/AAC Artifact 并取得 hash 绑定回执；该单样本仍不能替代商用质量或生产验证。
- 新增 `backend/scripts/record_external_creative_benchmark.py` 及对应测试，用于把豆包等外部产品已生成的图片/PPT 文件以只读方式复制到隔离评测目录，并绑定 benchmark plan/case hash、源文件/副本 hash 和结构观察。该入口仅记录 `external_artifact_imported`，`generation_performed=false`、`acceptance_observed=false`，不调用 Provider、不扣 Credits、不把外部文件标记为商用完成；图片、视频、PPTX/PDF 的导入、缺失配对、重复导入和非 allowlist Provider 已通过 5 条定向测试。
- 又用此前已保存的豆包同题样本做了一次 CLI 导入实证（不联网、不调用 Provider）：`image_smart_thermos`、`video_smart_thermos`、`ppt_smart_thermos` 均绑定同一 `benchmark_plan_sha256=e636e3f82c450794d2bfe21e9052da72811fe93b9ff68c42e17cad12fe572b86`，分别通过图片解码/画幅、视频解码/音频/时长画幅、PPTX/PDF/页数画幅/可编辑性/图片覆盖率结构观察；三份回执均为 `evidence_level=external_artifact_imported`、`generation_performed=false`、`acceptance_observed=false`。导入结果仅证明文件可观察，图片/视频水印与事实、PPT 来源/溢出等语义门禁仍是未知，不能升级为商用质量或 Provider 已验证。
- 2026-08-02 又对上述三个豆包外部回执分别执行 `verify_creative_benchmark_provenance.py`：图片、视频、PPT 三份回执均返回 `status=valid`、`artifact_verified=true`、`issues=[]`，仍保持 `evidence_level=external_artifact_imported`；该复核没有联网、没有调用 Provider、没有产生费用，也不改变三类正式评审为 `0` 的结论。
- 新增 `backend/scripts/verify_creative_benchmark_provenance.py` 及 7 条测试，统一校验单文件 Provider 回执和多文件外部导入回执的 plan/case/brief hash、模态对应的 Artifact 类型、Artifact 内容 hash、重复 Provider-case 组合、旧回执缺失 provenance，以及可选的每个 case Provider parity 门禁。对上述 3 份豆包导入回执的审计结果为 `status=valid`、`artifact_verified=true`；对历史 MiniMax 回执的复核明确返回 `invalid`（缺少当前 plan/case hash），因此旧样本不能直接作为新候选证据复用。当前迁移后的图片样本使用 `minimax + volcengine_agent_plan` 要求执行 parity 校验并通过。
- 新增 `backend/scripts/migrate_historical_benchmark_receipt.py` 及 3 条测试。它只在历史回执的 benchmark/case、canonical brief hash 和 Artifact hash 全部匹配时生成新的 `historical_receipt_provenance_bound` 副本，记录原始回执 hash，内部 `credential_id` 只保留 SHA-256，拒绝覆盖源文件或已有输出。当前已对历史 MiniMax 图片、火山图片和 MiniMax 视频样本生成迁移副本，三份回执通过 provenance verifier；这提升了可追溯性，但不把历史样本升级为新的 Provider 生成或商业质量结论。
- 当前工作树的本地浏览器矩阵又完成一次无私有内容输出的复验：使用 `admin@reeftotem.ai` 登录并切换到目标公司后，`/work`、`/dashboard`、`/plaza`、`/groups`、`/enterprise`、`/okr`、`/account/subscription`、`/account` 和指定 Agent Chat 均可达，`Groups` 按设计进入已有会话，所有页面无 `5xx`。Agent Chat 的固定媒体入口显示图片、语音、音乐、视频四个 `available` 控件；点击图片/视频只把受控请求插入 composer，未发送消息、未产生生成/运行请求。该证据补强本地入口与“先插入需求、再由用户发送”的交互，不等同于真实 Provider 生成或商用质量。
- 2026-08-04 升级后复验：同一租户的 SaaS 媒体路由中图片、语音、视频 Lite/Pro/Ultra 均显示 `volcengine_agent_plan -> minimax`，主线路为火山，凭证为 `plan=large`；音乐仍为 MiniMax-only。页面同时明确显示“账号已验证，生成未验证”，因此该结果证明资格与路由已对齐，不代表真实生成或商用质量通过。
- 同一浏览器会话又验证 Agent 详情身份：当前 `私人助理` 显示唯一 `agent-product-role=我的助理`，`抖音运营经理` 显示 `agent-product-role=Agent 员工`；详情 API 不再丢失列表 API 已判定的 viewer-specific 产品角色。
- 选取本地已有的 `succeeded` PPT 交付（2 个 Artifact）做只读浏览器投影复验：交付卡片位于 Agent 聊天消息区；点击“查看交付详情”后出现唯一的右侧 `deliverable-detail-drawer`，其中存在 PPT 预览 iframe 与 2 个 Artifact 下载链接；composer 中不存在“文件已生成，等待质量检查”面板。该证据确认 `Agent message -> Workspace detail drawer -> Artifact preview/download` 边界已落地，不代表该历史文件的 Provider/商业质量门禁已重新通过。
- 同一抽屉中的两个下载响应也通过本地会话复核：PDF 返回 `200`、`application/pdf`、`1542627` bytes；PPTX 返回 `200`、`application/vnd.openxmlformats-officedocument.presentationml.presentation`、`3875751` bytes。只验证传输与 MIME，不读取或评价文件内容。
- 普通成员媒体能力接口现已按产品边界脱敏：`/api/agents/{agent_id}/media-capabilities` 对非平台管理员只返回业务能力、可用性和质量状态，`tool_name` 统一为 `media_generation`，不返回 Provider、套餐、`route_reason` 或内部线路细节；平台管理员仍保留完整诊断。新增接口回归覆盖成员与平台管理员两种视图，本轮相关后端测试 `43 passed`，前端测试与生产构建也已通过。
- 2026-08-02 又修正普通用户的 `degraded` 提示：不再显示“平台将自动选择线路”这种可能掩盖质量差异的文案，改为明确说明正式交付需确认质量差异，或保存工作说明等待主线路恢复；提示不泄露 Provider、套餐或内部线路。媒体能力、降级路由和 Tool 门禁相关测试共 `122 passed`，Ruff 与差异检查通过。
- 同步修正 Agent Chat composer 的降级按钮文案，前端不再把快速生成与正式交付混成同一承诺；本轮前端 Node `108 passed`、Vitest `158 passed`、TypeScript/Vite build 均通过。
- 降级能力按钮新增警示色和 `data-capability-state="degraded"`，不依赖悬停才能发现状态；按钮仍只显示业务能力名称，不显示 Provider/套餐信息。
- 本轮又在本机权限上下文执行 `./restart.sh --source`：PostgreSQL、Backend、Frontend 和 Vite API proxy 均启动成功，`/api/health` 返回 `status=ok、version=1.11.14`，`/api/version` 返回 commit `1a81291a`、空 `release_id`。这只证明本地运行时可启动，不代表远程部署或真实 Provider 生成。
- Benchmark CLI 新增逐次授权门禁：在隔离目录执行火山视频 case 且不传 `--confirm-paid-provider-call` 时，回执为 `explicit_paid_provider_call_authorization_required`，`provider_accepted=false`、`artifact_path=null`、`credential_id=null`；直接把 PPT case 交给该 CLI 则以 `presentation_requires_artifact_pair` fail closed。两种预检都没有选择凭据、没有发出 Provider 请求，也没有消耗额度。相关定向测试此前为 `24 passed`；本次补入普通成员媒体能力脱敏回归后，复跑 `creative_provider_benchmark.py`、`record_external_creative_benchmark.py` 与 `agent_media_capabilities_api.py` 共 `26 passed`，Ruff 与差异检查均通过。
- 2026-08-02 目标续跑证据：`validate_multimodal_capability_matrix.py --json` 返回 `status=ready`、`errors=[]`、`route_policy_verified=true`，并明确保留 `provider_health_verified=false`；`audit_creative_benchmark_run.py` 的运行指纹仍为 `582d3f4b43bbf1b93e2e6274b029f20b6d32045c2a505b284ffc73f9c76059d6`，三种模态均为 `awaiting_human_review`，正式评审与商用结果均为 `0`。该次历史续跑未调用 Provider、未消耗 Credits、未修改远程环境；2026-08-04 的受控真实样本是后续独立证据。
