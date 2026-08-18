# 浏览器业务验收矩阵

- 状态：`acceptance-contract`
- 日期：2026-08-17
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
| 公司管理员 | 成员、员工治理、模板、订阅、审批 | membership `role=org_admin`；凭据不得写入文档/日志 |
| 公司所有者 | 唯一 owner、管理员任免、所有权、删除 | membership `role=org_owner`，与普通管理员分开验证 |
| Agent 受托管理者 | 受限员工配置 | membership `role=member`，只对部分 Agent 授予对象级 `manage`；`agent_admin` 仅作旧数据兼容对照 |
| 三名独立 reviewer | 图片/视频/PPT 人工质量检查 | 不同活跃 `Identity`，与创建者不同 |
| 平台运营者 | Provider、模型、路由、账号池、生产问题 | 全局 platform role；无 membership 也能进入平台面，不因此获得公司治理权 |

媒体业务流需准备：品牌文案、Logo、人物/产品参考图、合法测试音乐/配音脚本和明确的交付合同。不要为无关回归重复消耗付费 Provider Credits。

## 3. 关键场景矩阵

| ID | 场景 | 关键步骤 | 必须断言 | 主要证据 |
|---|---|---|---|---|
| REG-01 | 创建新公司 | 注册/登录 → 创建公司 → Onboarding | 一个 tenant 成员；助手槽位幂等；无重复 Agent | UI 截图、API/DB ID、无错误日志 |
| REG-02 | 加入已有公司 | 邀请/SSO → 加入 → Onboarding | 新 tenant 下有独立助手；不读取其他 tenant 助手记忆 | 两 tenant ID、权限负向检查 |
| AST-01 | 命名私人助手 | 设置名称/风格/边界 → 完成 | 导航显示“我的助理 · 名称”；角色固定；不混入员工列表 | DOM、Agent access_mode、onboarding link |
| AST-02 | 跳过定制 | 点击跳过 → 进入工作台 | 创建安全默认助手；不是无响应；可稍后修改 | Agent ID、默认设置、刷新后保持 |
| AST-03 | 助手故障恢复 | 模拟创建失败 → 进入工作台 → 重试 | 不阻塞工作台；不重复创建；错误可理解 | 故障提示、幂等重试记录 |
| MFA-UX-01 | 高权限首次 MFA 设置 | owner/admin 首次登录 → 扫描二维码或展开手工密钥 → 验证 → 保存恢复码 → 再次登录 | 明确说明这是角色策略触发的首次设置，不暗示用户已设置；二维码与密钥来自同一 `otpauth`；动态码仅 6 位；密钥不写入浏览器持久存储 | desktop/390px DOM、验证器实测、storage 检查、审计事件 |
| MFA-UX-02 | MFA 恢复与负向 | 错码/重放 → 恢复码 → 管理员恢复边界 | 错码和重放拒绝；恢复码单次使用；跨公司/越权恢复拒绝；旧会话失效 | HTTP/UI 状态、审计、权限负向 |
| LEGACY-AST-01 | 历史助理归档与恢复 | 历史助理 → 归档 → 打开历史记录 → 恢复 | 原 Agent/session/Workspace/深链保留；归档后不可启动执行且不占员工名额；恢复幂等 | Agent ID 对照、状态/配额、审计、刷新恢复 |
| LEGACY-AST-02 | 历史助理转员工与撤回 | 历史助理 → 确认名额和隐私 → 转为员工 → 撤回到历史 | 转换后才进入拓扑并占员工名额；默认访问范围不扩大；超额在转换前拒绝；撤回后不在员工花名册 | topology/list/API/DB、403、quota、audit |
| WORK-01 | 首次自然语言任务 | 工作台描述结果并附文件 → 澄清 → 确认 | 用户不选 Skill/Tool/Provider；生成稳定 Intent/Task/Run | 页面录屏、对象 ID 链、请求 payload |
| WORK-02 | 执行者路由 | 分别发起私人、一次性、长期、多方任务 | 路由到助理/临时专家/员工/Group；理由可理解 | 责任主体、route decision receipt |
| WORK-03 | 刷新/断网恢复 | 任务运行中刷新/断网/重连 | 不重复付费、不丢状态、回到真实 Run | Run ID、Credits、provider receipt |
| AGT-01 | 招聘员工 | 广场员工市场 → 选择模板 → 确认 → 创建 | 创建长期 Agent；职责清晰；Tool/Skill 不在普通必填项 | Agent/template/grant 记录 |
| AGT-02 | 员工权限 | member 使用；受托管理者管理获授权 Agent；尝试未授权 Agent | membership role 不提升；use/manage 分离；负向请求 403/隐藏入口 | UI + API 权限证据 |
| GRP-01 | Group 协作 | 创建 Group → 添加人/Agent → 会话 → @ → 文件协作 | 成员可见；非成员拒绝；Group Workspace 独立 | group/session/run/file IDs |
| GRP-02 | Group 交接审批 | Agent 产出 → 人类 review/approval → 交付 | 责任主体、检查人和批准人可追溯 | timeline、review、approval receipt |
| IMG-01 | 正式图片交付 | 工作说明 → 火山 Seedream → Artifact → 检查 → 批准 | 正确画幅/尺寸；Logo/文字合同；Provider/Credits/Artifact 一致 | 原图、hash、route snapshot、质量报告 |
| IMG-02 | 图片故障/降级 | 阻断火山 → 检查 MiniMax 路线 | 正式合同不得静默降级；可等待或明确确认 degraded | preflight 文案、未重复扣费 |
| VID-01 | Small/Medium 视频 | 正式人物广告视频 → 预检 | 火山视频显示 unavailable；不等待不存在的 Seedance；不假成功 | capability reason、无 Provider submit |
| VID-02 | 当前 Large 火山视频 | Large Key → 脚本/分镜 → Seedance 2.0 → 后期 → 交付 | 套餐模型正确；人物/品牌/音画合同；局部镜头可重做 | provider task、MP4 probe、质量与审批证据 |
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
- Small/Medium 只验证正确 unavailable；当前 Large 必须在受控真实调用通过后才进入商业质量判断。

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

最终独立测试工程师还必须提交：场景逐项结果、candidate SHA、身份/Tenant、关键对象 ID、截图或录屏路径、
console/network 异常、fixture 清理结果和未执行的外部门禁。任何 P0/P1 失败都必须复现、修复并由测试工程师
重新执行；实现者自己的定向测试不能替代该签收。

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

## 11. IAM-01 至 IAM-16 身份与权限专项

下一身份重构候选必须在本文件原有 REG/AST/AGT/ENT 场景之外，完整执行
[`10-identity-membership-permission-product-plan.md`](./10-identity-membership-permission-product-plan.md) 第 8 节的 IAM-01 至 IAM-16：

- 五类身份：普通成员、Agent 受托管理者、公司管理员、公司所有者、平台运营者；
- 两个 Tenant：验证新增 membership、切换、缓存/WebSocket 收敛和跨租户 IDOR；
- 三种入口状态：仅邀请、仅可创建公司、邀请与创建权益并存；
- 高风险动作：管理员任免、所有权转移、成员停用、退出、支持会话和公司删除；
- 主动退出：服务端先列出 Agent 所有权、任务、审批、交付、受托授权和个人凭证；未交接 Agent 硬阻断，完成后撤销 membership grant/凭证并安全切换或退出登录；
- 管理停用：即使存在待交接责任也能在明确确认后立即切断 membership；只显示计数和公司可见对象，private Agent 与其任务/审批/交付细节必须脱敏；
- 权限热变更：撤销 Agent manage、停用 membership、支持会话过期后旧页面和旧连接不可继续写；
- 私人边界：所有公司和平台角色均不得读取非本人私人助手内容。

每个场景必须同时取得 UI 正向、API 负向、审计事件和测试数据清理证据。现有 `agent_admin` 浏览器证据只证明旧兼容合同，不足以证明新对象能力与产品面合同完成。

## 12. 2026-08-15 IAM G6 本地工作树验收记录

本轮使用五类身份、两个临时 Tenant 和 desktop/390px 视口执行 IAM-01 至 IAM-16。运行时页脚显示的
`v1.11.40 (61c2d721)` 只是当前工作树的基础 release identity；因为本轮实现尚未固化为 immutable
candidate，不能把该 SHA 写成当前改动的候选证明。结论限定为 `local_browser_verified`，不代表已部署、
生产已验证或商业流程已证明。

| 场景 | 本地实跑结果 | 负向/边界证据 | 状态与缺口 |
|---|---|---|---|
| IAM-01 注册账号 | 注册凭证只创建账户身份和账户级权益，不隐式创建公司管理员 membership | 注册凭证与公司邀请使用不同对象、接口、状态机和审计动作 | `local_browser_verified` |
| IAM-02 创建公司 | 账户权益创建 Alpha/Beta 两家公司，创建者原子成为唯一 `org_owner` | 无权益、重复幂等键和并发路径由 API/事务测试拒绝 | `local_browser_verified + tests_pass` |
| IAM-03 发出邀请 | owner/admin 发出带 tenant、email、role、expiry 的公司邀请，并完成撤销 | member 与 Agent 受托管理者无邀请入口且 API 拒绝 | `local_browser_verified` |
| IAM-04 接受邀请 | 新成员与已有身份均按邀请声明角色创建一次 membership；接受、撤销均有审计 | 错邮箱、过期、已撤销、重放和跨 Tenant 均拒绝 | `local_browser_verified + tests_pass` |
| IAM-05 第二家公司 | 同一 Identity 同时保留 Alpha/Beta membership；主动退出 Alpha 后原子切到 Beta | Alpha 旧 token 返回 `401`，Beta 新 token 返回 `200`；旧公司数据不留在页面查询缓存 | `local_browser_verified` |
| IAM-06 SSO/JIT | 本机 Alpha 未配置可用外部 IdP，公共 SSO provider 查询 fail-closed 返回 `403 Organization SSO is unavailable` | 自动化证明 JIT 新建用户固定为 `member`，不会因首位加入而升级管理员 | `tests_pass + local_config_negative`；真实外部 IdP 往返未验证 |
| IAM-07 公司初始化 | 新建与加入路径均按服务端 `entry_mode` 进入公司资料、成员资料、私人助手、开工四步并可刷新恢复 | 重试不重复创建 Tenant、owner 或助手槽位 | `local_browser_verified` |
| IAM-08 私人助手 | 每个 membership 使用独立私人助手；主动退出前正确要求删除本人未交接的私人助手 | owner/admin/platform operator/Agent 受托者读取他人私人助手 API 均拒绝 | `local_browser_verified` |
| IAM-09 Agent use/manage | Product Manager Agent 完成 admin → 受托管理者 → admin 的授权、创建者移交和回流 | 未授权 manager 的 Agent GET/PATCH 均为 `403` | `local_browser_verified` |
| IAM-10 公司管理面 | owner/admin 可进入公司管理；成员和 Agent 受托者仍保留员工工作面 | 普通成员与受托者的管理路由/API 拒绝；键盘 Tab/Enter 可进入“成员与邀请” | `local_browser_verified` |
| IAM-11 所有权 | 两次目标确认式 ownership request/accept 均完成；公司删除进入 30 天可恢复期并完成 restore | org_admin 与 platform operator 不能替 owner 转移或删除公司 | `local_browser_verified`；到期物理删除未获授权、未执行 |
| IAM-12 平台运营面 | tenantless platform operator 可进入独立运营面，不借用公司 membership | 公司角色不能进入平台 Provider/路由治理；平台身份不自动获得公司管理权 | `local_browser_verified` |
| IAM-13 支持会话 | 创建 metadata/diagnostics 范围会话，只返回公司元数据与聚合计数，并记录 summary read 审计 | 跨 Tenant、过期/结束会话和私人 Agent 读取均 `403`；响应不含 email、token、内容或 Workspace | `local_browser_verified + tests_pass` |
| IAM-14 权限热变更 | 开着 Agent 设置页撤销全部对象权限后，3 秒复核自动显示“访问权限已失效”，不再渲染保存/移交控件 | 旧 manager 会话 Agent GET/PATCH 均 `403`；React Query 后续 `403` 不再沿用旧成功数据提供编辑能力 | `local_browser_verified` |
| IAM-15 停用/退出 | 管理停用/恢复与本人主动退出均实跑；退出先阻断责任对象，完成助手删除后切到仍有效公司 | 退出后的 Alpha membership/token 失效，Beta membership 保留；脱敏 preflight 不泄露 private 工作细节 | `local_browser_verified` |
| IAM-16 旧数据迁移 | 角色、旧邀请码、owner 候选和 Agent grant 迁移合同均进入 PostgreSQL fresh/historical/downgrade/re-upgrade smoke | 不确定 owner 进入 resolution，不静默猜测；`agent_admin` 只迁移对象 grant，不获得公司治理权 | `migration_smoke_pass + tests_pass` |

额外非功能证据：普通成员工作台、公司管理员管理面、公司所有者 ownership 面和平台运营面均在
390px 视口无横向溢出；权限、邀请、支持、所有权、停用/恢复、退出、删除/恢复等动作留下对应审计事件。
本轮 QA 数据已按固定 Tenant/User/Identity/Agent ID 清理并扫描所有 UUID 引用为零；8 个 Agent 工作区
移入本机废纸篓以便恢复。浏览器录制保存在
`/Users/sun/.config/browser-harness/agent-workspace/recordings/astra-g6-iam-20260815`。

## 13. 2026-08-16 IAM-17/18 邮件交付增量验收

本轮以专用本地公司所有者进入 `/company-admin/members`，实跑 mail-first 邀请、配置未就绪状态、
旋转重发和密码二次认证后的单次人工链接。正常邀请列表只显示脱敏邮箱、角色、业务状态、投递状态和
错误码，不回显原始 token；人工链接只在本次响应显示，刷新页面后消失。未配置 SMTP 时 UI 明确显示
“邮件配置未就绪”，没有伪报发送成功；重发后旧邀请变为 `revoked/cancelled`，人工链接再次轮换旧凭证。

持久化复核确认三次邀请动作分别留下 `organization_invitation_issued`、
`organization_invitation_resent` 和 `organization_invitation_manual_link_issued` 审计；outbox 中没有明文
`ORG-` 标记。页面控制台无 error。专用 Identity、User、Tenant、Onboarding、邀请、outbox 与审计记录
均按固定 ID 精确清理，清理后关联记录计数为零。

自动化证据包括：邮件/邀请相关后端 `109 passed`，前端合同 `12 passed`，Ruff、compileall、生产构建、
PostgreSQL fresh/historical/downgrade/re-upgrade migration smoke，以及真实 PostgreSQL + loopback SMTP
capture（`smtp_accepted`）。因此 IAM-17/18 当前结论是 `local_browser_verified + local_smtp_verified`；
它不代表外部 SMTP Provider、真实收件箱、部署或生产验证。

## 14. 2026-08-16 IAM-19/20 MFA 增量验收

本轮将 MFA 固定在全局 `Identity`，而不是某一条 membership。`org_owner`、`org_admin` 与平台运营者的
密码登录在未绑定时只返回短时 bootstrap challenge，不签发 access token；绑定后每次密码登录都必须用
TOTP 或一次性恢复码完成二次验证。普通 member 可选择启用和关闭，但一旦启用，同一 Identity 的全部公司
membership 都受同一套验证器和恢复码保护。

真实本地 PostgreSQL + HTTP smoke 完成 35 项断言：确认式绑定、TOTP 登录、challenge 防重放、恢复码单次
消费与轮换、旧 `auth_version` token 立即 `401`、高权限禁止关闭、普通成员可关闭、单公司普通成员的公司
管理员恢复、多公司 Identity 必须平台运营者恢复、跨 Tenant 与管理员目标拒绝。19 条 MFA 审计记录只包含
身份/方法/计数/范围元数据；TOTP seed 使用认证加密 envelope，恢复码只保存 Identity 域分离 HMAC。两次
smoke 均精确删除 QA Identity/User/Tenant/challenge/recovery/audit，最终关联记录为零。

桌面浏览器实跑了公司所有者首次登录 → 强制绑定 → 仅显示一次的 10 枚恢复码 → 明确勾选已保存 → 工作台，
以及再次密码登录 → MFA challenge → 工作台；账户安全页正确显示 Identity 级说明、强制策略、启用时间、
剩余恢复码和轮换入口，不为高权限账号展示关闭动作。页面截图和 DOM 均无控制台错误，专用浏览器 fixture
清理后记录为零。自动化证据为 MFA/迁移合同 `24 passed`、Ruff、compileall 和前端生产构建通过。

IAM-19/20 当前结论为 `local_browser_verified + local_http_postgres_verified`；390px 复验、全量回归和
候选 SHA 绑定归入 G12。该结论不代表部署、生产验证或外部身份服务已验证。

## 15. 2026-08-16 IAM-21/22 企业 SSO 增量验收

G10 在现有 Google Workspace/OIDC 适配器上增加测试专用 loopback IdP，没有新增通用 OAuth2 产品入口。
授权请求使用服务端保存且一次消费的 opaque state、同浏览器绑定、OIDC nonce 与 PKCE S256；callback 以
JWKS/RS256 校验签名，并严格核对 issuer、audience、expiry、nonce、subject、verified email 和 hosted domain。
授权码另有单次 claim，重复 code 即使配合新 state 也会拒绝并审计。

真实本地 PostgreSQL + Redis + Vite + Backend + IdP HTTP smoke 完成 46 项断言：公共 Google/GitHub 注册入口
保持 `410` 且不创建数据；错误 state、错浏览器、错 Tenant、state/code 重放、Provider 禁用均 fail closed；
首次 JIT 只创建一个 `member` membership，不生成密码、不提升管理员或 owner，并返回可进入普通员工工作面的
完整 access snapshot。相关后端定向回归为 `102 passed`，前端 SSO/MFA 合同为 `7 passed`，Ruff 与 compileall
通过。

桌面浏览器从公司登录页进入明确标注 `NOT A REAL PROVIDER` 的本地授权页，批准后落到 `/work`；侧栏显示
“成员”，没有公司管理或平台运营入口。页面控制台没有 error，fixture 的 Tenant、User、Identity、membership、
provider、SSO session 和审计均精确清理。IAM-21/22 结论为
`local_browser_verified + local_oidc_emulated`；390px 复验和候选 SHA 绑定归入 G12，真实 Google Workspace
管理员配置、外部网络往返与 `provider_verified` 仍未验证。

## 16. 2026-08-16 IAM-23 到期清理增量验收

G11 新增 `TenantDeletionJob`、`TenantDeletionHold` 与无 Tenant 外键的最小
`TenantDeletionTombstone`。owner 发起删除仍只进入 30 天可恢复期；到期物理清理没有公共 API 或网页按钮，
只允许受控 CLI/worker 在显式开发开关、loopback PostgreSQL、专用数据库名和隔离 Tenant slug 同时满足时执行。
平台运营面只暴露队列、无删除 dry-run、法务/运营暂停和解除暂停，并明确提示 reason code 不得包含个人或业务内容。

专用 PostgreSQL 演练完成 32 项断言：未到期拒绝、legal/operations hold 与幂等解除、未知跨租户依赖和
无主键表阻断、schema drift、重复 dry-run、文件删除部分失败后重入、真实隔离清理、重复执行、其他 Tenant 与
跨公司 Identity 保留、恢复竞态以及不含公司名称的墓碑 receipt。迁移还先构造过部分表结构并确认 upgrade
fail closed，随后 fresh head 完成清理演练；临时数据库和文件目录均被销毁。

桌面浏览器以临时平台运营者经 MFA 进入 `/admin/platform/companies`，看到到期公司和清理阶段，依次完成
dry-run、operations hold 和 release；状态按 `scheduled → dry_run_passed → held → scheduled` 更新，页面无
物理删除动作且控制台无 error。5 个临时 Identity/User、3 个 Tenant、hold/job/audit/challenge 和本机临时凭据
文件已精确删除。相关后端定向回归为 `105 passed`，前端 Node `125 passed`、Vitest `207 passed`、生产构建、
Ruff 与 compileall 均通过。

IAM-23 当前结论为 `local_browser_verified + isolated_postgres_purge_verified`。它不代表生产数据已清理、已部署
或生产恢复策略已获批准；G12 仍需完成全量门禁、desktop/390px 多角色矩阵、独立终审和候选 SHA 绑定。

## 17. 2026-08-16 IAM-24 / G12 候选收口验收

G12 在同一工作树完成了 desktop 与 390×844 两套浏览器矩阵。公司所有者、公司管理员、普通成员、第二公司
所有者和 tenantless platform operator 均使用各自真实权限进入产品面；owner 可进入 ownership，公司管理员只能
进入公司管理，普通成员只能进入工作面，第二公司所有者只看到第二家公司，platform operator 只能进入平台运营面。
所有 390px 页面均无横向溢出；公司角色访问平台路由、普通成员访问公司管理、平台身份访问公司治理均被重定向或拒绝。

当前最终树的自动化证据为：后端 `4496 passed`；前端 Node `125 passed`、Vitest `207 passed`（38 files）和
TypeScript/Vite production build；Ruff、compileall、`git diff --check`；Agent capability contract
（30 templates、17 skills、141 tools）与 creative v1 contract `115 passed`。真实本地 smoke 还包括 loopback SMTP、
Identity MFA `35` 项断言和 `19` 条审计、OIDC emulator `46` 项断言、tenant purge `32` 项断言，以及 PostgreSQL
fresh/historical/downgrade/re-upgrade migration 全链。

独立 code reviewer 结论为 `APPROVE`。独立 architect 首轮发现 `auth.py` 的 tenant-switch redirect 存在启动级语法
阻断；修复为先生成 `redirect_fragment` 再拼接 URL 后，API 编译、应用启动、认证/权限专项 `100 passed`、后端全量、
OIDC HTTP smoke 和完整迁移均重新通过，architect 复审结论转为 `CLEAR`。这条记录保留失败发现与修复过程，不用最终
通过结果覆盖曾经存在的 blocker。

浏览器、HTTP 和一次性数据库 fixture 均已精确清理；未遗留 `clawith-g12*` 临时凭据文件或 G11/migration 专用数据库。
本文件随本地 immutable candidate commit 一并固化；由于 commit 不能在自身内容中自指其最终 SHA，准确 SHA 必须由
commit 创建后的 `/api/version`、页面 footer 和交付报告共同记录。当前证据仅支持
`immutable_local_candidate + local_business_flow_proven`，不支持外部 SMTP、真实企业 IdP、生产 purge、已部署或
`production_verified`。

## 18. 2026-08-17 G7 本地集成与浏览器业务流收口

本轮证据绑定未提交的本地工作树，基础运行时 release identity 为 `v1.11.40 (73714112)`；因此结论是
`local_browser_verified`，不能外推为 immutable candidate、已部署或生产已验证。测试没有调用真实 SMTP，
没有发送外部邮件，也没有调用付费模型 Provider。

自动化门禁全部新鲜执行：后端 `4507 passed`；前端 Node `133 passed`、Vitest `208 passed`（38 files）与
production build（6459 modules）；Ruff、compileall、`git diff --check`；能力合同为 30 templates、17 skills、
141 tools、114 runtime-typed，六模态矩阵 ready，creative v1 contract `115 passed`。PostgreSQL
fresh/historical/downgrade/re-upgrade 与 tenant purge 演练均到唯一 `legacy_assistant_lifecycle (head)`；本地
loopback SMTP、MFA HTTP/PostgreSQL（35 assertions、19 audit rows）与 OIDC emulator（46 assertions）均通过。

浏览器使用一次性 fixture 覆盖 owner、org_admin、member、Agent 管理受托者、第二 Tenant owner 和 tenantless
platform operator：

- owner 在 `/employees` 看见当前私人助理、长期员工与历史助理整理区，并完成
  `archive → restore → convert_to_employee → return_to_history` 往返；Agent ID、聊天和 Workspace 深链保持稳定；
- Agent 管理受托者只看见获授 `manage` 的数字员工，可进入其设置并管理权限；读取私人助理、其他 Agent 或第二
  Tenant 均为 `403`，公司与平台管理路由不可达；
- org_admin 可进入公司管理但不能进入平台运营；member 只保留员工工作面；第二公司 owner 只看到其 Tenant；
  platform operator 只看到平台运营、公司列表和系统邮件，不借用任何公司 membership；
- 注册凭证与公司邀请在平台页面继续明确为两类对象；系统邮件页未保存完整配置时“发送测试邮件”保持禁用；
- 工作台只输入业务意图，不要求用户选择 Provider、model、Skill 或 Tool；本地无可用执行能力时 preflight 返回
  `unavailable`，明确不创建 Task、不扣 Credits；刷新后草稿保留但必须重新 preflight；
- `/work`、`/employees`、`/company-admin`、`/account/security` 与平台系统邮件页在 390px 下均无横向溢出；
  公司身份访问平台路由、普通成员访问公司管理和跨 Tenant API 均被拒绝。

录屏位于
`/Users/sun/.config/browser-harness/agent-workspace/recordings/clawith-g7-20260817`（123 frames）。fixture 清理
后本轮 tag 关联的 Identity、Tenant、Agent 均为 0。Group 的真实多人 Provider 执行、真实外部收件箱、真实 Google
Workspace、付费生成、发布和生产业务流仍是外部门禁；这些未执行项不会被本轮自动化或本地 emulator 冒充。

## 19. 2026-08-18 四类 P0 新增验收合同

本节是新合同，不回写第 18 节的历史 QA 结论。当前状态均为 `not_run`，只有新的实现与新鲜证据才能更新。
详细产品边界见 `12-four-p0-product-closure-plan-2026-08-18.md`。

| ID | 正向浏览器流程 | 负向/旁路断言 | 当前状态 |
|---|---|---|---|
| BILL-01 | member 打开“我的用量”，看到个人安全投影和 entitlements | Network 不请求公司 summary/ledger/orders/profile | `not_run` |
| BILL-02 | — | member 直调敏感 billing API 为 403，敏感 DB/Provider 零调用 | `not_run` |
| BILL-03 | admin 只看公司聚合；owner 看完整账单管理 | admin manage 403；platform role 不继承 tenant billing | `not_run` |
| BILL-04 | owner 查看 current tenant order | foreign order ID 为 404，无 actor/payment/PII | `not_run` |
| BILL-05 | member 仍可看 plans/packs/entitlements | 权限收紧不误伤 Runtime entitlement | `not_run` |
| BILL-06 | 同一 Identity 切换两个 tenant | 无旧余额、订单、流水或 query cache | `not_run` |
| OKR-01 | 两名 member 都看 company Objective 和各自用户内容 | 不能互看用户目标、KR、evidence、日报 | `not_run` |
| OKR-02 | Agent manager 看被授权 Agent 投影 | 无其他 Agent/人类日报 | `not_run` |
| OKR-03 | admin 管理 current tenant OKR | 不可见/foreign ID 为 404，owner name 不泄漏 | `not_run` |
| OKR-04 | admin 执行管理型 outreach | member 为 403，且 chat/background task/LLM 零调用 | `not_run` |
| OKR-05 | admin 写合法 owner/target | foreign/nonexistent target 失败且零 commit | `not_run` |
| OKR-06 | REST 与 Agent Tool 显示相同 viewer scope | Tool 不返回 full-board 旁路 | `not_run` |
| OKR-07 | GET settings/periods | 零 insert/update/commit | `not_run` |
| OKR-08 | tenant member 按角色读取 | platform-only 无 membership 时无 tenant 内容 | `not_run` |
| OVR-01 | member 打开公司概览的个人投影 | 不请求 token aggregate，不显示公司 token/cache/Credits | `not_run` |
| OVR-02 | member 查看 viewer-scoped topology | 三个资源字段为 `null`，前端不做 node 求和 fallback | `not_run` |
| OVR-03 | Agent manager 看被管理 Agent 非财务摘要 | 不显示公司资源总量 | `not_run` |
| OVR-04 | admin/owner 看 current tenant 聚合 | SQL 与 UI 不混入其他 tenant | `not_run` |
| OVR-05 | dual-scope 身份进入 tenant | platform role 不继承 analytics | `not_run` |
| OVR-06 | tenant/capability 热切换 | 旧卡片、Query cache、WS 增量不可见 | `not_run` |
| WORK-01 | 用户只输入 intent/约束，系统提议 executor 并解释 | 不显示 Provider/model/Skill/Tool | `not_run` |
| WORK-02 | 用户在高级设置手工覆盖 | private/stopped/expired/foreign 候选不可选 | `not_run` |
| WORK-03 | 推荐后改变 intent/ACL/readiness | 旧 fingerprint 失效；重放不重复 Task/Run/Credits | `not_run` |
| WORK-04 | reviewer/final approver/L3 approver/recovery 各自处理 inbox | 无关用户无 action | `not_run` |
| WORK-05 | Run failed 后 Task 回 pending | creator attention 仍存在且可幂等恢复 | `not_run` |
| WORK-06 | `/work/:taskId` 查看全部 attempts/revisions/Group children | 不把部分成功误判完成 | `not_run` |
| WORK-07 | 从 action link 调原领域 API | 撤权后的旧页面 mutation fail closed | `not_run` |
| WORK-08 | AgentDetail formal delivery handoff | 旧 getTask 响应与深链不回归 | `not_run` |
| WORK-09 | 被授权用户打开 detail/inbox | foreign tenant、无关 admin/manager 均无数据 | `not_run` |
| CROSS-01 | 同一 Identity 双 membership 完整切换 | 所有页面、API、cache、WS 只认 current tenant | `not_run` |
| CROSS-02 | platform operator + 普通 membership | 不继承 billing/OKR/analytics/private Work | `not_run` |
| CROSS-03 | foreign object IDs 跨域直达 | 响应、日志、后台任务均无泄漏 | `not_run` |
| CROSS-04 | 撤权、停用、切 tenant 后继续操作旧页面 | 轮询、mutation、WS、Tool、action link 全部 fail closed | `not_run` |

最终浏览器矩阵必须同时覆盖 desktop 与 390px，并记录 Network、角色、tenant/object IDs、稳定错误码、
release identity 和 fixture 清理证据。provider-free P0 可用本地 stub；不允许为了通过本节触发真实支付、外联或
付费 Provider。

## 20. 2026-08-19 工作台与协作群组 P0 收口验收

本轮保持左侧导航和既有 Agent 消息界面不变，完成的是两个产品面的任务闭环：工作台作为当前用户的统一任务控制面，
协作群组作为多人和 Agent 的可见协作现场。两者读取同一条
`Task -> AgentRun/Event -> Deliverable/Artifact -> Review -> Approval -> Delivery` 真相链，Group 没有第二套任务
状态机。普通消息、`@Agent` 执行和 Workspace 文件均不自动创建 Task；只有用户从一条已持久化消息执行显式确认，才会
创建带稳定来源、唯一 `primary_owner` 和可选 collaborators 的正式任务。

本节先绑定最终提交前工作树。它随本地 candidate commit 一并固化，但 commit 无法在自身内容中自指最终 SHA；准确 SHA
必须在提交后通过 `/api/version`、页面 footer 和最终报告补充证明。本轮没有调用真实邮件、支付、外部服务或付费 Provider，
也没有部署或修改生产。

| 场景 | 正向与状态证据 | 负向/恢复证据 | 结论 |
|---|---|---|---|
| WORK-01/02 | 普通用户只提交业务意图；服务端给出可解释 executor proposal，高级设置保留 manual override | private/stopped/foreign/无权限候选 fail closed；旧显式 executor payload 保持兼容 | `tests_pass + local_browser_verified` |
| WORK-04/05 | `/work` 显示“待我处理 / 进行中 / 最近完成”；失败 Run 向 creator 投影 `task_recovery`，retry 生成新 attempt | 无权用户没有 action；旧 action 在撤权/移除后 fail closed | `tests_pass + local_browser_verified` |
| WORK-06 | `/work/:taskId` 展示任务来源、独立状态轴、全部 Runtime attempts、正式交付事实和原现场深链 | `run_failed` 后的 `delivery_succeeded` 只显示为“执行失败 · 结果通知已送达”，不误判执行成功 | `tests_pass + local_browser_verified` |
| GRP-01/02/03 | Group 消息保留原样；显式“创建正式任务”先开确认面板并选择唯一第一责任 Agent | 普通消息、`@Agent`、关闭/取消面板均零正式 Task；system/hidden/foreign source 不可转换 | `tests_pass + isolated_postgres_browser_verified` |
| GRP-04/05 | 显式转换保存服务端来源快照、primary owner/collaborators、TaskLog 和稳定 Task/Run ID | 同 `client_request_id` 与同 source message 重放返回既有 Task；最终仅 1 Task / 1 首 Run / 0 重复 Credits | `tests_pass + isolated_postgres_verified` |
| GRP-06/08/09 | active Group 成员读取 collaboration-safe Task/Run 投影，creator 读取完整详情 | non-member/removed member/cross-tenant/篡改 session 或 message 均 403/404/422，Artifact/Review/Approval/Delivery 详情不越权 | `tests_pass + hostile_api_verified` |
| GRP-07/10/12 | Group“关联任务”与 Work 详情双向深链；desktop 与 390px 均显示责任 Agent、失败、待处理和结果通知状态 | 必要 participant 失败不显示完成；页面无横向溢出 | `local_browser_verified` |

独立测试工程师保留了两次拒绝签收的历史，而不是用最终通过覆盖：第一轮发现
`run_created -> run_failed -> delivery_succeeded` 被 UI 混成“执行失败”和“Runtime 成功”；修复 lifecycle/notification
分轴后，第二轮又因 Group 卡片只显示失败、未显示“结果通知已送达”而 `FAIL`。补齐 Group 卡片双状态后，第三轮在隔离
PostgreSQL、backend `8029`、frontend `3038` 和本地 Runtime stub 上 `PASS`：WorkDetail 与 Group card 的 desktop/390px
都显示失败、可恢复和通知已送达，普通消息/`@Agent` 不自动建 Task，显式转换、幂等和 cross-tenant `404` 均通过。

新鲜自动化证据为：后端 Ruff、compileall 和 full regression（最终独立 reviewer `4656 passed`）；前端 Node
`147 passed`、Vitest `209 passed`（38 files）与 TypeScript/Vite production build（6465 modules）；相关 Work/Group
回归 `19 passed`；`git diff --check`；PostgreSQL fresh/historical/downgrade/re-upgrade migration smoke。独立 code reviewer
最终结论为 `APPROVE`，P0/P1 为 0。

第三轮隔离数据库、token/state/evidence 文件和 `8029/3038` 进程均已删除，数据库/端口残留为 0。浏览器记录保存在
`/Users/sun/.config/browser-harness/agent-workspace/recordings/clawith-p0-g7`；主流程补充记录保存在
`/Users/sun/.config/browser-harness/agent-workspace/recordings/clawith-g12-work-group-e2e`。本节最高只支持
`local_business_flow_proven`；`immutable_local_candidate` 需提交后 release identity 复核，`deployed` 与
`production_verified` 仍为 false。
