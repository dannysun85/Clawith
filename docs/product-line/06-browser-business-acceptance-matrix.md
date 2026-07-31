# 浏览器业务验收矩阵

- 状态：`acceptance-contract`
- 日期：2026-07-31
- 目的：下一产品线每个切片都必须通过真实浏览器业务流，不以单元测试、API smoke 或文件存在代替

## 1. 验收层级

每个场景分别记录：

1. `code_exists`：代码和迁移存在；
2. `tests_pass`：相关自动测试通过；
3. `local_browser_verified`：候选 SHA 的本地浏览器流程通过；
4. `deployed`：同一候选已获授权部署；
5. `production_verified`：生产 release identity、配置与真实浏览器流程通过；
6. `business_flow_proven`：真实 Provider/Artifact/审批/计费和业务质量均有证据。

不得用较低层级冒充较高层级。

## 2. 测试身份与前置数据

| 身份 | 用途 | 要求 |
|---|---|---|
| 新公司创建者 | 注册、公司创建、私人助手、首个任务 | 全新 Identity/tenant，无历史缓存 |
| 普通成员 | 日常任务、助理、员工、Group、产物 | `role=member` |
| 公司管理员 | 成员、员工治理、模板、订阅、审批 | `role=org_admin`；本地可使用 `admin@reeftotem.ai`，密码不得写入文档/日志 |
| Agent 管理员 | 受限员工配置 | `role=agent_admin`，只授予部分 Agent `manage` |
| 三名独立 reviewer | 图片/视频/PPT 人工质量检查 | 不同活跃 `Identity`，与创建者不同 |
| 平台管理员 | Provider、模型、路由、账号池、生产问题 | 无租户也能进入 SaaS Admin |

媒体业务流需准备：品牌文案、Logo、人物/产品参考图、合法测试音乐/配音脚本和明确的交付合同。不要为无关回归重复消耗付费 Provider Credits。

## 3. 关键场景矩阵

| ID | 场景 | 关键步骤 | 必须断言 | 主要证据 |
|---|---|---|---|---|
| REG-01 | 创建新公司 | 注册/登录 → 创建公司 → Onboarding | 一个 tenant 成员；助手槽位幂等；无重复 Agent | UI 截图、API/DB ID、无错误日志 |
| REG-02 | 加入已有公司 | 邀请/SSO → 加入 → Onboarding | 新 tenant 下有独立助手；不读取其他 tenant 助手记忆 | 两 tenant ID、权限负向检查 |
| AST-01 | 命名私人助手 | 设置名称/风格/边界 → 完成 | 导航显示“我的助理 · 名称”；角色固定；不混入员工列表 | DOM、Agent access_mode、onboarding link |
| AST-02 | 跳过定制 | 点击跳过 → 进入工作台 | 创建安全默认助手；不是无响应；可稍后修改 | Agent ID、默认设置、刷新后保持 |
| AST-03 | 助手故障恢复 | 模拟创建失败 → 进入工作台 → 重试 | 不阻塞工作台；不重复创建；错误可理解 | 故障提示、幂等重试记录 |
| WORK-01 | 首次自然语言任务 | 工作台描述结果并附文件 → 澄清 → 确认 | 用户不选 Skill/Tool/Provider；生成稳定 Intent/Task/Run | 页面录屏、对象 ID 链、请求 payload |
| WORK-02 | 执行者路由 | 分别发起私人、一次性、长期、多方任务 | 路由到助理/临时专家/员工/Group；理由可理解 | 责任主体、route decision receipt |
| WORK-03 | 刷新/断网恢复 | 任务运行中刷新/断网/重连 | 不重复付费、不丢状态、回到真实 Run | Run ID、Credits、provider receipt |
| AGT-01 | 招聘员工 | 广场员工市场 → 选择模板 → 确认 → 创建 | 创建长期 Agent；职责清晰；Tool/Skill 不在普通必填项 | Agent/template/grant 记录 |
| AGT-02 | 员工权限 | member 使用；agent_admin 管理授权 Agent；尝试未授权 Agent | use/manage 分离；负向请求 403/隐藏入口 | UI + API 权限证据 |
| GRP-01 | Group 协作 | 创建 Group → 添加人/Agent → 会话 → @ → 文件协作 | 成员可见；非成员拒绝；Group Workspace 独立 | group/session/run/file IDs |
| GRP-02 | Group 交接审批 | Agent 产出 → 人类 review/approval → 交付 | 责任主体、检查人和批准人可追溯 | timeline、review、approval receipt |
| IMG-01 | 正式图片交付 | 工作说明 → 火山 Seedream → Artifact → 检查 → 批准 | 正确画幅/尺寸；Logo/文字合同；Provider/Credits/Artifact 一致 | 原图、hash、route snapshot、质量报告 |
| IMG-02 | 图片故障/降级 | 阻断火山 → 检查 MiniMax 路线 | 正式合同不得静默降级；可等待或明确确认 degraded | preflight 文案、未重复扣费 |
| VID-01 | 当前 Small 视频 | 正式人物广告视频 → 预检 | 火山视频显示 unavailable；不等待不存在的 Seedance；不假成功 | capability reason、无 Provider submit |
| VID-02 | 升级后火山视频 | Medium+ Key → 脚本/分镜 → Seedance → 后期 → 交付 | 套餐模型正确；人物/品牌/音画合同；局部镜头可重做 | provider task、MP4 probe、质量与审批证据 |
| PPT-01 | 正式 PPT | 来源 → 大纲确认 → 逐页生成 → 预览 → 下载 PPTX/PDF | 有多版式与所需图片；可编辑；无溢出；PPTX/PDF 一致 | PPTX、PDF、页面渲染、结构检查 |
| PPT-02 | PPT 局部修改 | 指定一页改文案/图片/图表 | 只创建目标页相关 revision，不全量重做 | revision lineage、前后对比 |
| REV-01 | 三人质量检查 | 创建者分配三名 reviewer → 分别提交 | 创建者不能算独立 reviewer；身份唯一；提交不可改 | assignment/identity/receipt |
| REV-02 | Artifact 变化 | review 中替换 Artifact | 旧 review 自动 superseded；旧批准不能交付新文件 | hash 变化、状态转换 |
| DEL-01 | 正式交付展示 | Agent 生成结果 → 打开详情 → 确认交付 | 结果在 Agent 消息行；详情在右侧；composer 无完成面板 | DOM 定位、Artifact 下载 |
| DEL-02 | 跨员工发现 | 工作台查看等待/进行中/最近完成 | 状态来自服务端读模型；深链回原 Agent/Group/Artifact | ID 对照、刷新一致性 |
| SUB-01 | 套餐与 Credits | 查看权益 → 启动付费任务 → 预留/结算 → 看流水 | 估算与实际区分；exactly-once；不足时有升级动作 | reservation/ledger/order |
| SUB-02 | 套餐限制 | 禁用某 modality/tier → 发起任务 | Provider 调用前拒绝；普通用户看业务原因，不看 Key | 无 Provider receipt、用户提示 |
| ENT-01 | 企业配置 | 管理员配置成员、员工、Skills/Tools、审批 | 权限正确；变更可审计；普通成员不可进入 | UI、403、audit event |
| ENT-02 | 平台配置 | 平台管理员调整文字/媒体路由 | 租户用户看不到 Provider Key；路由版本化、可回滚 | route diff、audit、secret redaction |
| EXP-01 | 经验沉淀 | 已交付工作 → 生成经验 draft → 人工发布 | draft 不可被 Agent 检索；published 可检索；保留来源 | experience status、search result、source IDs |

## 4. 跨场景非功能断言

每个相关场景同时覆盖：

- tenant isolation：跨租户 IDOR 为 403/404，文件 URL 不泄露；
- idempotency：重复点击、刷新、重连不重复创建/扣费/外发；
- permissions：菜单隐藏不是唯一保护，服务端必须拒绝；
- secrets：Key、token、完整用户敏感输入不进入日志、URL 或错误详情；
- accessibility：键盘、焦点、语义标签、状态非纯颜色；
- responsive：至少 desktop 与窄视口验证首次任务、审批和交付；
- release identity：浏览器证据必须绑定 commit SHA、前后端版本和配置快照；
- observability：失败包含隐私安全的 correlation ID 和可操作 reason code；
- cost safety：非必要回归不调用付费 Provider，真实调用需明确测试目的和上限。

## 5. Provider 与质量专项

### 文字

- 在 Lite/Pro/Ultra 分别验证实际 Primary 是 MiniMax-M3；健康故障时才进入兼容 fallback。
- 验证 Tool calling、上下文预算、Credits、模型 route snapshot 和 failover 安全点。

### 图片

- 至少覆盖品牌海报、人物广告、产品图、信息图和带精确文字/Logo 的组合设计。
- 不能只检查 HTTP 200；必须检查原始 Artifact、画幅、文字、品牌、人物/产品一致性和商用评分。

### 视频

- 至少覆盖真人广告、产品展示、口播/旁白和短剧情；每类使用开放输入而非固定 prompt。
- 当前 Small 只验证正确 unavailable；只有 Medium+ 受控真实调用通过后才验证商业质量。

### PPT

- 至少覆盖商业方案、数据汇报、培训材料和品牌提案；检查多版式、图片、图表、可编辑性和事实引用。
- 豆包 Benchmark 用相同输入和交付合同做盲评，不固定用户只能使用某一种模板或 prompt。

## 6. 执行批次

1. `Batch A — 身份与入口`：REG、AST、WORK 基础流。
2. `Batch B — 员工与协作`：AGT、GRP、权限负向。
3. `Batch C — 正式交付`：IMG、VID、PPT、REV、DEL。
4. `Batch D — 治理与计费`：SUB、ENT、secret/IDOR/exactly-once。
5. `Batch E — 复用与发布`：EXP、全链路回归、release identity、生产验证。

任一批次的 P0/P1 失败都阻止下一环境发布；Provider 商业质量未通过不阻止非媒体产品逻辑开发，但必须保持对应正式能力关闭。

## 7. 验收记录模板

每次执行至少记录：

```text
candidate_sha:
environment:
frontend_release:
backend_release:
worker_release:
config_snapshot_hash:
scenario_id:
actor/tenant:
task/run/deliverable/artifact_ids:
provider/model/tier/modality:
credits_reserved/settled:
result:
evidence_paths_or_urls:
known_gaps:
```

## 8. 完成标准

- 六类核心角色、所有一级入口和完整对象链在浏览器中可解释、可恢复、可审计。
- 图片、视频、PPT 的“能调用”与“达到商用”分别有真实 Artifact 和盲评证据。
- 本地通过、已部署、生产验证和商业流程证明被分别记录。
- 未具备的能力保持关闭或明确 degraded，不向客户展示假完成。

## 9. 2026-08-01 本地工作树验收记录

本节绑定的是提交前工作树与本地 `v1.11.9` 开发环境，不是 immutable candidate 证据。浏览器中的前端热更新已包含本轮路由修复，但当时后端 release identity 仍为失效候选 `1276da37`；最终 candidate SHA 只能在完整门禁、独立评审、提交和前后端重启后重新验证。未调用新的付费 Provider，也没有部署或生产验证。

| 场景 | 当前证据 | 结论 | 尚未证明 |
|---|---|---|---|
| WORK-01/02 | `/work` 依次完成普通任务、指定 Agent、临时专家和 Group 的 preflight；工作说明确认与执行分离 | `local_browser_verified` | 全新用户首次任务、断网恢复 |
| GRP-01 | 选择现有双 Agent Group 后，服务端因 planning/compact 模型未配置而 fail-closed，未创建虚假运行 | `local_browser_verified` | 模型配置完成后的真实多人终态聚合 |
| IMG-02/VID-01 | SaaS 媒体路由显示图片/视频以火山 Agent Plan 为主线路、MiniMax 为非等价应急线路；当前配置缺少账号验证 receipt，因而两类能力均显示“当前可用：无”并 fail-closed，未提交付费任务 | `local_browser_verified` | 当前账号只读验证、真实 Provider Artifact 与商业质量 |
| PPT-01（流程） | PPT Brief preflight、工作说明、正式交付入口、已有 PPTX/PDF 深链和右侧工作现场可达；抽样存量交付为 PPTX 8 页/8 个媒体文件、PDF 8 页 | `local_browser_verified` | 本候选上的新一轮付费生成与豆包盲评 |
| DEL-01/02 | 工作台读取服务端工作索引；正式结果留在 Agent 消息中，详情在右侧抽屉，输入框没有完成面板；历史 PPT/图片/视频按已验证 Artifact 投影为 18/18、10/10、13/13 制作步骤完成 | `local_browser_verified` | 高并发刷新与断网重连 |
| OKR evidence | 创建本地验收 OKR，选择已批准正式 Deliverable 作为证据，进度达到 100%，来源深链可达 | `local_browser_verified` | Artifact 替换后旧证据的浏览器 supersede 流 |
| AST-01/AGT-02 | 迁移后管理员自己的私人助手只出现在“我的助理”；普通成员看不到他人私人助手；`agent_admin` 只能编辑被显式授予 `user/manage` 的 Agent，未授权 Agent 设置只读 | `worktree_browser_verified` | 新 candidate SHA 上的同路径复验、跨租户 IDOR API 专项 |
| 导航/权限入口 | 普通成员与 `agent_admin` 均无法进入 `/enterprise`、`/invitations`、`/account`、`/admin/saas`；平台页面服务端/页面守卫拒绝非平台管理员；公司管理员正向入口可达；desktop 与 390×844 窄视口通过 | `worktree_browser_verified` | 新 candidate SHA 上的 release-identity 绑定复验 |
| SaaS readiness | 文本路由显示 MiniMax-M3 的优先级高于火山文本兼容路由；媒体页区分已配置、账号验证、生成验证和人工质量，并在缺少当前配置 receipt 时不宣称可用 | `local_browser_verified` | 最后一次真实 Provider 验证 receipt 的持久展示 |

已覆盖页面包括工作台、公司概览、OKR、发现中心、Groups、企业设置、邀请、订阅、平台运营、SaaS 模型路由与媒体路由。测试中临时创建的 `agent_admin` 授权 Agent 和权限行已删除，测试成员角色已恢复为 `member`。REG-01/02、运行中断网恢复、跨租户 API IDOR 专项、Provider 真实调用、豆包 Benchmark、发布和生产验收仍保持未完成，不能从本记录外推。

## 10. `cc6affe7` 实现候选复验

本轮实现已固化为本地提交 `cc6affe7aa1ad35f5bc1e4be0ab4a7247067b248`。在前后端重启后，
`/api/version` 返回 `cc6affe7`，浏览器页面页脚也显示同一 release identity。管理员身份完成了以下
SHA 绑定复验：

- `/work` 可打开，导航分别显示一个“我的助理”和“Agent 员工”分区；
- `/invitations` 与 `/enterprise` 在公司管理员身份下可进入，`/admin/saas` 在平台管理员身份下可进入；
- 从工作台打开正式交付抽屉，可以看到 PPTX/PDF Artifact 下载入口；
- 交付状态和操作位于 Agent 消息与右侧工作现场，composer 中不存在“文件已生成，等待质量检查”完成面板。

普通成员与 `agent_admin` 的负向浏览器矩阵是在同一实现工作树提交前完成；提交后未修改相关实现，且
候选上的完整后端 `4088 passed` 和对象级授权定向测试仍覆盖这些拒绝路径。由于现有普通成员测试账号的
凭据不应被擅自重置，本轮没有伪造“新 SHA 上重复登录”的浏览器证据。该项保持为
`tests_pass + prior_worktree_browser_verified`，不是 `candidate_browser_verified`。REG-01/02、断网恢复、
真实 Provider、豆包盲评、发布与生产验证继续保持未完成。

本文件所在的最终证据提交不能在自身内容中写入自己的 SHA；其精确 SHA、`/api/version` 返回值和浏览器
页脚对照由提交后的收口记录绑定。该 release-identity 对照属于候选冻结证据，不再反写成新的提交。
