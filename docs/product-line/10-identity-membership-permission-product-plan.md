# 身份、公司成员关系、权限与产品面重构计划

## 0. 文档状态

- 日期：2026-08-15
- 状态：`implementation_in_progress_g5`（G1–G4 已完成，G5 收口中，G6 全量与浏览器门禁未执行）
- 范围：本地产品合同、数据与权限重构、界面产品面、迁移兼容、自动化与真实浏览器验收
- 基础事实：保留现有任务优先工作台、私人助手、数字员工中心/协作网络、Agent 消息界面、Group、Deliverable、Workspace 和平台治理能力
- 非目标：不部署、不推送、不修改生产、不调用付费 Provider、不改变 Agent Runtime；不以文档或单元测试通过代替真实本地业务流

本计划解决的不是“管理员多显示几个菜单”，而是把以下四类事实彻底分开：

1. `Identity`：这个自然人是谁、如何登录、是否为平台运营者；
2. `OrganizationMembership`：这个人在某家公司是什么成员角色；
3. `ObjectGrant`：这个人对某个 Agent、Group、Artifact 或审批对象能做什么；
4. `ProductSurface`：当前进入员工工作台、公司管理面还是平台运营面。

邀请、公司创建、公司切换和平台支持访问都是上述事实的状态转换，不是新的角色。

## 1. 总目标与完成定义

### 1.1 总目标

把当前混合的 `platform_admin / org_admin / agent_admin / member` 单线角色模型重构为：

- 全局身份与平台运营权；
- 每公司一条明确成员关系；
- 公司所有者、公司管理员和普通成员三类公司角色；
- 基于具体 Agent 的 `use/manage` 对象授权；
- 服务端下发的有效能力与可访问产品面；
- 普通员工、受托 Agent 管理者、公司治理者和平台运营者可解释且不可越权的界面。

### 1.2 完成门槛

只有同时满足以下条件，才可标记本地目标完成：

1. 本文全部产品决策进入代码合同和验收矩阵；
2. 注册、登录、创建公司、接受邀请、加入第二家公司、切换公司和退出公司的主/异常路径均可恢复且幂等；
3. 每家公司恰有一个可恢复的所有者，不再通过“首位加入者”猜测管理员；
4. `agent_admin` 不再被当作高于普通成员的公司级管理角色，管理权以 Agent 对象授权为准；
5. 平台运营权不再自动等于公司治理权，不默认读取成员私人助手或业务内容；
6. 前端根据服务端有效能力和产品面渲染，菜单隐藏之外还有服务端拒绝；
7. 旧用户、旧角色、旧邀请码、旧 Agent 权限、旧路由和旧会话完成无破坏迁移；
8. 后端全量、前端全量、类型检查、构建、Ruff、迁移 smoke、权限负向和浏览器身份矩阵全部通过；
9. changed-files 清理、复测、独立代码审查和架构审查通过；
10. 结论明确限定为本地验证，不冒充已部署或生产验证。

## 2. 产品决策

### D-01：公司管理员也是员工

`org_admin` 与 `org_owner` 默认仍进入员工工作台，保留工作台、自己的私人助手、数字员工和 Group。管理能力通过独立的“公司管理”入口提供，不把管理员强制送入仪表盘。

### D-02：平台运营面与公司工作面分离

平台运营者默认进入平台控制台。平台运营权只负责租户生命周期、平台注册门禁、套餐/Credits、Provider/模型路由、平台健康、全局审计与发布证据，不自动拥有任何公司的业务管理权。

需要协助公司时，必须通过显式支持会话进入：选择公司、填写原因、确认范围、设置过期时间、产生审计事件。支持会话不得授予私人助手、私人 Workspace 或私人消息访问权。

### D-03：公司所有者是独立角色

公司创建者成为唯一 `org_owner`。所有者拥有公司管理员能力，并额外负责：

- 转移所有权；
- 任免公司管理员；
- 公司级账单责任与商业主体信息；
- SSO/域名所有权的最终确认；
- 停用或永久删除公司。

普通 `org_admin` 不得永久删除公司、转移所有权或任命另一个所有者。

### D-04：Agent 管理是对象能力，不是公司层级

“Agent 受托管理员”是产品称谓，不是比 `member` 更高的公司角色。有效管理权来自：

- Agent 创建者；
- 明确的 `AgentPermission(scope_type=user, access_level=manage)`；
- 公司管理员对非私人、公司治理范围内 Agent 的管理策略。

兼容期可以保留数据库中的 `agent_admin` 值，但任何界面和 API 都不得只凭该值授予管理权；最终应迁移为普通成员加对象授权。

### D-05：公司创建是账户级权益

`company.create` 属于全局账户/平台注册策略，不属于当前公司角色。企业优先默认策略下：

- 普通公司员工不默认看到“创建新公司”；
- 有 `company.create` 权益的自然人才看到创建入口；
- 加入其他公司依赖待处理邀请，不依赖当前公司是否允许；
- 当前公司的管理员无权禁止一个自然人在个人账户层创建另一家公司，但平台套餐/风控可以限制。

### D-06：平台注册凭证与公司邀请完全分离

- `RegistrationGrant`：只决定是否允许创建全局账号，不绑定公司角色；
- `OrganizationInvitation`：绑定公司、目标身份、目标角色、有效期和状态；
- `OrganizationJoinLink`：可选的企业域/批量加入能力，只能授予预设的低风险角色，不能创建所有者；
- SSO JIT：按已验证域名和企业策略创建明确角色的成员关系。

### D-07：私人助手内容始终 owner-only

公司管理员和平台运营者可以管理私人助手的套餐政策、允许的工具、审计元数据和安全事件，但默认不能读取其会话内容、记忆、Workspace 或附件。任何合规取证都必须是单独的、有法律/企业政策依据的流程，不纳入普通管理员权限。

## 3. 目标角色与有效能力

| 主体 | 生命周期 | 默认产品面 | 核心能力 | 明确禁止 |
|---|---|---|---|---|
| `member` | 每公司成员周期 | `work` | 任务、自己的助理、获授权 Agent、Group、自己的结果与用量 | 公司治理、平台配置、他人私人内容 |
| Agent 受托管理者 | 对象授权周期 | `work` | 在被授予的 Agent 上配置、查看管理范围内运行/审批、撤回低风险设置 | 企业管理、未授权 Agent、公司成员与账单 |
| `org_admin` | 每公司成员周期 | `work + company_admin` | 成员邀请、公司级 Agent 治理、政策、企业集成、审计、订阅操作 | 所有权转移、永久删除、平台 Provider、私人内容 |
| `org_owner` | 公司所有权周期 | `work + company_admin` | `org_admin` 全部能力，加所有权、管理员任免、账单主体、删除 | 平台 Provider、成员私人内容 |
| `platform_operator` | 全局运营周期 | `platform_admin` | 注册门禁、租户生命周期、套餐/Credits、Provider/路由、平台健康和发布 | 默认公司业务审批、默认进入私人工作区 |

### 3.1 有效能力命名

服务端至少按以下命名空间计算并下发能力：

- `work.use`
- `company.create`
- `company.view`
- `company.members.view`
- `company.members.invite`
- `company.members.manage`
- `company.admins.manage`
- `company.settings.manage`
- `company.audit.view`
- `company.billing.view`
- `company.billing.manage`
- `company.ownership.transfer`
- `company.delete`
- `agent.use`
- `agent.create.private`
- `agent.create.company`
- `agent.manage.assigned`
- `agent.manage.company`
- `platform.tenants.manage`
- `platform.registration.manage`
- `platform.billing.manage`
- `platform.providers.manage`
- `platform.support_session.create`

对象级 Agent `use/manage` 仍由单个 Agent 响应的 `access_level` 或 `capabilities` 表达，不能把所有 Agent ID 塞进 `/me`。

### 3.2 身份响应合同

登录和切换公司后的身份响应应包含：

- 当前 `identity_id`；
- 当前 `membership_id` 与 `tenant_id`；
- `membership_role`；
- `global_roles`；
- `effective_capabilities`；
- `available_surfaces`；
- 待处理邀请数量；
- 当前支持会话摘要（若存在）。

前端不得再在多个页面独立推导平台、公司和对象权限。

## 4. 产品面与导航

### 4.1 员工工作面 `work`

所有公司成员默认进入：

```text
工作
  工作台
  协作群组

团队
  我的助理 · <名称>
  数字员工 · <可见数量>

经营
  公司概览（按权限裁剪）
  目标与复盘
  团队知识

账户
  我的账户
  我的用量
  公司切换/待处理邀请
```

普通员工不显示 Provider、模型路由、公司邀请码、成员管理、账单主体和平台配置。

### 4.2 Agent 受托管理体验

不增加完整公司管理导航，只在数字员工中心和 Agent 详情中提供：

- “我管理的员工”筛选；
- 可管理节点标识；
- 对被授权 Agent 的设置、审批、Tool/Skill/Trigger/Channel 与可见性操作；
- 未授权 Agent 保持 `use` 或只读；
- 授权被撤销后，正在编辑的页面立即降级为只读并解释原因。

### 4.3 公司管理面 `company_admin`

只有拥有对应能力的 `org_admin/org_owner` 显示“公司管理”，进入后使用独立二级导航：

```text
公司管理
  概览
  成员与邀请
  Agent 员工治理
  权限与审批
  企业知识与集成
  套餐、账单与用量
  审计日志
  公司设置
  所有权与删除（仅 owner）
```

公司管理面不复制 Agent 消息界面；点击具体 Agent 仍进入现有详情/消息页面。

### 4.4 平台运营面 `platform_admin`

平台运营者默认看到独立外壳：

```text
平台运营
  租户与生命周期
  注册授权
  套餐与 Credits
  Provider 账号池
  模型与媒体路由
  平台健康与生产问题
  全局审计与发布证据
```

平台控制台不显示“我的助理”“数字员工”“协作群组”等公司业务入口。进入公司必须先创建支持会话或切换到一个真实、明确授权的公司成员关系。

## 5. 产品流程 F01–F16

### F01 注册全局账号

**参与者**：未登录访客、平台注册策略。

**前置条件**：密码注册服务可用；若开启注册门禁，具有有效 `RegistrationGrant`。

**主路径**：

1. 用户提交邮箱、密码、显示名称；
2. 服务端验证邮箱唯一性、密码策略、注册授权与速率限制；
3. 创建 `Identity` 和无公司上下文的账户锚点；
4. 发送并完成邮箱验证；
5. 登录后读取待处理公司邀请与 `company.create` 权益；
6. 有邀请进入 F04；有创建权益进入 F02；两者都有时先展示明确选择；两者都无时进入“等待邀请/申请加入”。

**异常与恢复**：邮件不可用时 fail closed；重复提交不重复创建 Identity；已有邮箱进入登录/找回密码；注册链接中的公司邀请不被当作平台注册码消费两次。

**验收**：匿名、已存在邮箱、无 SMTP、非法/过期注册授权、重复点击、验证链接重放。

### F02 创建新公司

**参与者**：已验证 Identity，具备 `company.create`。

**主路径**：

1. 填写公司名称、地区/时区和必要条款；
2. 显示“你将成为公司所有者”；
3. 原子创建 Tenant、免费/默认订阅、`org_owner` 成员关系和审计事件；
4. 颁发绑定新 membership 的访问令牌；
5. 进入 F07 公司初始化。

**规则**：当前是否是另一家公司的 `member/admin` 不影响账户级创建权益；同一幂等键只能创建一次；公司名称重复不等于同一公司；创建失败不得留下无 owner 的 Tenant。

**验收**：无权益 403、并发双击、订阅创建失败回滚、创建后唯一 owner、第二家公司创建与切换。

### F03 公司管理员发出邀请

**参与者**：`org_admin/org_owner`。

**主路径**：

1. 在“成员与邀请”输入目标邮箱；
2. 选择允许的目标角色：默认 `member`，可选“Agent 受托管理候选”；
3. 只有 owner 可直接邀请/任命 `org_admin`；任何邀请都不能创建 `org_owner`；
4. 设置有效期，生成一次性 invitation token；
5. 写入邀请状态和审计事件，发送邮件；
6. 页面显示 pending/accepted/revoked/expired。

**规则**：邀请绑定 canonical email、tenant、role、inviter、expiry；同一邮箱的重复 pending 邀请合并或显式替换；禁用成员不能通过旧邀请自行恢复。

**验收**：普通成员 403、Agent 管理者 403、跨租户、伪造角色、撤销后重放、过期、邮件发送失败重试。

### F04 新用户通过公司邀请注册并加入

1. 用户打开邀请链接；
2. 未登录时先完成 F01，但邀请码只作为 pending organization invitation 保存；
3. 登录身份邮箱必须与邀请目标匹配，或按企业已确认的域策略处理；
4. 用户查看公司名称、邀请人和目标角色，显式接受；
5. 原子创建 membership、消费邀请并记录 accepted_at；
6. 进入 F08 成员 Onboarding。

**禁止**：不得因公司没有管理员而自动升级角色；不得允许其他邮箱抢占邀请；不得在注册阶段和加入阶段各计数一次。

### F05 已有账号接受另一家公司邀请

1. 登录后在账户菜单看到待处理邀请；
2. 查看目标公司和角色后接受或拒绝；
3. 接受时创建新的 membership，不覆盖当前 membership；
4. 成功后询问“立即切换”或“留在当前公司”；
5. 切换后清空 tenant-scoped query/cache 并进入新公司 F08。

**验收**：已有 membership、重复接受、邀请已撤销、目标公司停用、当前页面有未保存内容、跨公司缓存不泄漏。

### F06 SSO/JIT 或企业域加入

- SSO provider 必须绑定 tenant；
- JIT role 来自企业策略，默认只能是 `member`；
- 首位 SSO 用户不能自动成为管理员或 owner；
- owner/admin 必须在启用 JIT 前完成显式配置；
- 域名匹配不等于自动加入，除非企业策略明确开启并经过域所有权验证。

### F07 公司初始化

**仅 owner/admin 可见的公司步骤**：公司名称确认、时区/地区、公司规模、默认成员策略、是否允许成员创建私有 Agent、默认审批策略、SSO/企业域稍后设置入口。

可以跳过非必要项；跳过后使用安全默认值。Provider、模型、Skill、Tool 不进入公司首次初始化。

完成后 owner 进入自己的成员 Onboarding F08；初始化失败可恢复，不重复创建 Tenant 或 owner。

### F08 成员 Onboarding 与私人助手

每个 `(tenant, user)` 独立执行：

1. 确认在该公司的显示名称、职位、时区/工作时间；
2. 解释私人助手与公司 Agent 员工的区别；
3. 可设置私人助手名称、响应风格和主动程度，也可以跳过定制并采用安全默认值；
4. 无论是否定制，都幂等创建/恢复唯一私人助手槽位；
5. 明确私人助手 owner-only、公司管理员默认不可读；
6. 进入 `/work`，提供首个任务引导。

公司名、Provider、模型、Skill、Tool、员工拓扑不在个人 Onboarding 中配置。

### F09 登录、公司选择与切换

- 一个 membership：直接进入该公司 `/work`；
- 多个 membership：恢复上次明确选择，失效时显示公司选择器；
- 只有平台运营权且无公司 membership：进入平台控制台；
- 同时有平台运营权和公司 membership：首次明确选择产品面，后续记忆选择，但账户菜单可切换；
- 切换公司必须取得新 membership-scoped token、清空缓存、关闭旧 tenant WebSocket，并重新建立浏览器会话。

禁止使用 `localStorage tenant_id` 作为授权事实；服务端 token/membership 才是权威。

### F10 普通员工日常工作

1. 默认进入工作台；
2. 使用自己的助理、获授权数字员工或 Group；
3. 数字员工拓扑只展示当前 membership 可见的长期员工；
4. 可以打开 Agent 消息界面和可访问 Workspace；
5. 只有 `use` 时不能看到设置和管理审批；
6. “公司概览/目标/知识”的内容按公司政策裁剪；
7. “我的用量”只解释个人消费和权益，不显示公司账单主体或 Provider Key。

### F11 招聘/创建 Agent 员工

**普通员工**：若公司政策允许，可创建额外的 private Agent；不能创建 custom 或 company-wide Agent。需要指定成员共享或公司可见时，由公司管理员创建或调整。

**公司管理员/owner**：从数字员工中心进入员工市场或高级自定义创建，确认职责、交付边界、负责人、可见范围和套餐影响后创建 company/custom Agent。

**结果路径**：

- “仅创建”回到员工名册并高亮；
- “创建并开始对话”进入现有 Agent 消息界面；
- 新员工作为真实节点进入拓扑，不用“添加节点”伪装数据对象；
- 创建失败不留下不可管理的半成品；超配额显示可操作原因。

### F12 Agent 管理权委派与撤销

1. company admin/owner 在 Agent 治理或 Agent 权限页选择成员；
2. 授予 `use` 或 `manage`，说明 manage 的具体能力；
3. 受托成员在数字员工中心看到“我管理的员工”；
4. 管理动作全部重新检查对象授权；
5. 撤销后立刻失去写权限，未提交表单不得继续保存；
6. 授权、变更、撤销均进入审计。

私人助手不能委派或转让给公司管理员；其中包含个人上下文，离开公司前只能由本人显式删除。普通 Agent 的创建者可以转交所有权；公司 owner 可以在离职/失联处置中强制转交非 private Agent，所有路径都必须校验目标为本公司活跃成员并写审计。

### F13 公司成员与管理员治理

`org_admin` 可以邀请/停用普通成员、分配 Agent 管理权和查看公司审计；只有 owner 可以任免 `org_admin`。

停用成员时必须：

- 立即撤销该 membership token/session；
- 先返回服务端责任预检并要求管理员确认；安全事件不得因未完成交接而阻止立即停用；
- 保留审计、任务责任和 Artifact 来源；
- 以计数和公司可见对象处理其 Agent、审批、待办与交付责任；private Agent 的名称、ID、任务标题、审批动作和交付细节不得暴露给管理员；
- 保留其私人助手数据但不可由管理员读取，直到按保留政策删除；
- 已有对象授权在停用期间休眠；只有显式恢复 membership 后才重新生效；
- 不影响同一 Identity 在其他公司的 membership。

### F14 所有权转移、退出和公司删除

**所有权转移**：owner 选择已验证、活跃的本公司成员；目标确认；原子交换角色；撤销旧高风险会话；写双向审计。

**owner 退出**：必须先转移所有权，不能让公司无 owner。

**管理员/成员主动退出**：服务端预检其 Agent 所有权、未完成任务、待处理审批、交付物、受托管理授权、个人凭证和所有权转移。仍拥有未删除 Agent 时硬阻断，先由本人转交普通 Agent 或删除私人助手/其他 Agent；其余责任需显式确认。退出只停用当前 membership，同时撤销成员级 Agent grant、使个人凭证失效、保留历史任务/Artifact/审计，并原子切换到其他有效 membership；没有可用 membership 时退出登录。

**永久删除公司**：仅 owner；再次认证；输入公司名称；显示影响清单和恢复政策；先进入可恢复停用期，再由后台流程永久删除。平台运营者不能绕过 owner 流程直接删除正常公司，安全/合规例外必须使用单独审计流程。

### F15 平台运营与支持会话

平台运营者可以创建/停用 Tenant、管理 RegistrationGrant、套餐与 Provider，但不能直接使用租户 Agent。

支持会话流程：

1. 选择公司和工单/原因；
2. 选择最小能力范围；
3. 设置最长有效时间；
4. 界面持续显示支持模式横幅；
5. 所有读取/写入带 support_session_id；
6. 到期、手动结束或切换公司立即失效；
7. 私人助手与私人内容始终排除。

### F16 套餐、用量和账单

- 普通成员：查看自己的用量、剩余额度和业务化限制说明；
- org_admin：查看公司聚合用量、套餐权益和升级入口；
- owner/显式 billing capability：管理订单、发票、支付主体和续费；
- platform operator：管理套餐目录、Credits 规则和跨租户异常；
- Provider 成本、密钥和路由只在平台面展示。

## 6. 权限计算与服务端规则

### 6.1 授权顺序

每个请求按以下顺序验证：

1. Identity 活跃且认证版本有效；
2. 当前 membership 存在、活跃且属于 token tenant；
3. 目标 Tenant 活跃；
4. 目标 product surface 允许；
5. company/global capability 允许；
6. 对象级 grant 允许；
7. 资源 tenant 与 membership tenant 一致；
8. autonomy/approval/plan/Credits 等现有门禁允许。

任何更高层身份都不能跳过 tenant 和对象检查。

### 6.2 前端守卫

- 路由守卫按 `available_surfaces/effective_capabilities` 判断；
- 页面按钮按能力和对象 access level 判断；
- 服务端返回 401 时重新认证，403 时解释权限，409 时刷新角色/邀请状态；
- 导航隐藏不是安全措施；直接访问深链仍由 API 拒绝；
- 权限在会话中变化时，Query cache、WebSocket 和编辑状态必须同步收敛。

## 7. 数据与迁移策略

### 7.1 新增/重构对象

- 增加公司所有者表达，并保证每个有效 Tenant 恰有一个 owner；
- 将平台运营权固定在全局 Identity/全局授权，不依赖租户 User.role；
- 将 Agent 管理权固定在 AgentPermission；
- 新建独立 `RegistrationGrant` 与 `OrganizationInvitation`；
- 可选增加 `OrganizationJoinLink` 和 `PlatformSupportSession`；
- 为邀请增加 `target_email/role/status/expires_at/accepted_by/accepted_at/revoked_at`；
- 身份接口增加 capabilities/surfaces。

### 7.2 兼容迁移

1. 迁移前只读审计每个 Tenant 的活跃管理员、创建时间和异常状态；
2. 对有且仅有一个明确初始管理员的 Tenant，生成 owner 候选；
3. 多管理员或无管理员 Tenant 不静默猜测，进入 `owner_resolution_required` 清单；本地 fixture 可显式确认，生产迁移必须另获授权；
4. `platform_admin` 用户转换为全局平台运营权，已有公司业务权限不能凭平台身份自动保留，需明确 membership；
5. `agent_admin` 保留现有 AgentPermission，membership 降为 member 后权限不扩大也不丢失；
6. 旧 tenant invitation code 只允许在受控兼容期兑换为默认 member 邀请，不再授予 admin；
7. 旧平台注册码迁移为 RegistrationGrant；
8. 所有迁移支持 PostgreSQL fresh upgrade、历史升级、downgrade/upgrade 或明确不可逆说明；
9. 不改写 Agent ID、会话、Workspace、Task、Artifact 和深链。

## 8. 验收矩阵

| ID | 场景 | 正向断言 | 必做负向断言 |
|---|---|---|---|
| IAM-01 | 注册账号 | Identity 唯一、邮箱验证、无隐式公司角色 | 注册码不能授予公司管理员 |
| IAM-02 | 创建公司 | 原子创建 tenant + 唯一 owner | 无 `company.create` 403、并发不重复 |
| IAM-03 | 发出邀请 | 邮箱/tenant/role/expiry 固定 | member/Agent 管理者不能邀请 |
| IAM-04 | 新用户接受邀请 | 一次创建明确 membership | 其他邮箱、过期、撤销、重放拒绝 |
| IAM-05 | 已有用户加入第二家公司 | membership 并存、显式切换 | 不覆盖旧 membership、不泄漏缓存 |
| IAM-06 | SSO/JIT | 按策略创建 member | 首位 SSO 用户不升级管理员 |
| IAM-07 | 公司初始化 | 安全默认、可恢复 | 不重复 tenant/owner |
| IAM-08 | 私人助手 | 每 membership 一个、owner-only | 公司/平台管理员读取拒绝 |
| IAM-09 | Agent use/manage | 对象级权限准确 | 未授权 Agent 设置/API 403 |
| IAM-10 | 公司管理面 | admin/owner 正向可达 | member/Agent 管理者路由和 API 拒绝 |
| IAM-11 | 所有权 | 仅 owner 转移/删除 | org_admin/platform operator 不能越权 |
| IAM-12 | 平台运营面 | tenantless operator 可达 | 公司角色不能进入 Provider/路由 |
| IAM-13 | 支持会话 | 范围、时限、审计生效 | 私人内容、过期会话、跨 tenant 拒绝 |
| IAM-14 | 权限热变更 | 撤权后页面转只读 | 旧 token/旧缓存不能继续写 |
| IAM-15 | 成员停用/退出 | 当前 membership 失效、其他公司保留 | 责任对象不丢失、无孤儿 owner |
| IAM-16 | 旧数据迁移 | 角色、邀请和 Agent grant 无损 | 不能自动把不确定管理员变 owner |

每个场景同时覆盖：tenant IDOR、幂等、审计、错误码、敏感信息、键盘、窄视口、刷新/回退、release identity 和测试数据清理。

## 9. 实施目标与顺序

### G1 产品合同与差距清单

- 更新角色、入口、导航、状态机和浏览器验收文档；
- 把本文决策映射到当前模型/API/页面和测试；
- 输出无歧义的数据迁移与兼容决策。

**门禁**：产品角色、所有权、邀请和平台支持访问仍有歧义时不改数据库。

### G2 后端身份与成员领域

- 实现 owner、global platform operator、membership capability resolver；
- 拆分 RegistrationGrant/OrganizationInvitation；
- 移除 first-joiner admin 推断；
- 实现创建、邀请、接受、切换、停用、转移和删除规则；
- 增加迁移、审计和定向安全测试。

### G3 服务端能力与对象授权

- `/me`/身份响应下发有效能力和产品面；
- 统一 company/platform route dependencies；
- Agent 管理继续以对象 `access_level=manage` 为权威；
- 清理角色层级与重复判断；
- 覆盖跨租户、私人助手和权限热变更。

### G4 前端产品面与流程

- 登录后目标选择、待处理邀请、创建/加入/切换公司；
- 员工工作面、公司管理面、平台运营面分离；
- 普通成员、Agent 受托管理、admin、owner 的导航和动作差异；
- 公司管理二级导航与所有权高风险操作；
- 保留 Agent 消息界面和 `/employees` 网络/名册实现。

### G5 Onboarding 与员工中心整合

- 公司初始化与个人 Onboarding 分层；
- 私人助手 owner-only 和幂等恢复；
- 数字员工中心按权限展示 use/manage/create；
- 添加员工、委派管理、撤销和回流路径闭环。

### G6 迁移与全角色验证

- PostgreSQL fresh/historical migration smoke；
- 后端/前端全量回归、Ruff、build、合同检查；
- 五类身份、两 Tenant、桌面/移动真实浏览器矩阵；
- 负向 API、缓存/WebSocket 切租户、权限热变更和测试数据清理；
- changed-files 清理、复测、独立代码与架构审查。

## 10. 风险与停止条件

- 无法确定现有 Tenant owner 时，停止自动迁移并输出待确认清单；
- 发现平台运营权依赖租户 `User.role` 的隐式业务路径时，先补兼容层和测试再移除；
- 发现邀请码仍被注册与加入双重消费时，停止前端改造先修事务合同；
- 发现撤权后旧 token/WebSocket 仍可写时，不进入浏览器完成声明；
- 任一普通成员、受托 Agent 管理者或平台运营者能读取他人私人助手内容时，本目标阻断；
- 全量测试通过但业务流未跑，不标记 `local_browser_verified`；
- 当前工作树有他人改动，所有实现必须适配并保留，不得重置或覆盖。

## 11. 完成报告必须包含

- 实际落地的产品决策和与本文的差异；
- 变更模型、迁移、API、页面和测试；
- 五类身份的正向/负向证据；
- fresh/historical migration 证据；
- 自动测试、构建、浏览器和独立审查结果；
- 本地、候选、部署、生产和业务流证明的严格状态；
- 未完成的外部授权与剩余风险。

## 12. 当前实现映射与 G6 实跑结论

| 决策/流程 | 当前本地落地证据 | G6 实跑结论或保留缺口 | 验收 |
|---|---|---|---|
| D-01 管理员也是员工 | `/auth/me` 下发 `available_surfaces/effective_capabilities`；`Layout.tsx` 保留工作面并链接独立 `CompanyAdmin` 外壳 | 五身份 desktop/390px 导航、深链正负向通过；四种移动产品面无横向溢出 | IAM-10 |
| D-02 平台与公司分面 | `Identity.is_platform_admin` 映射全局角色；`PlatformOperations` 使用独立外壳；公司依赖不接受平台权替代；`PlatformSupportSession` 独立建模 | tenantless operator、支持会话范围/过期/结束/跨 Tenant 与私人内容负向通过 | IAM-12/13 |
| D-03 公司 owner | `Tenant.owner_user_id`、`org_owner` 枚举与唯一约束迁移；目标确认式 ownership transfer；删除仅 owner 且进入 30 天可恢复停用期 | 所有权双向转移和删除/恢复 UI 通过；到期物理删除需单独的数据生命周期授权，未执行 | IAM-02/11/15/16 |
| D-04 Agent 对象管理 | `AgentPermission use/manage`、统一 access resolver 与对象写栅栏；`agent_admin` 只保留迁移兼容 | 委派/撤销/回流和打开设置页后的热撤权通过；旧 GET/PATCH 均 `403`，页面 fail-closed | IAM-09/14/16 |
| D-05 account company.create | `IdentityCapabilityGrant(company.create)`；`/tenants/self-create` 行锁、幂等键、唯一 owner 与 membership-scoped token | Alpha/Beta 两家公司创建、无权益、幂等键和并发负向通过 | IAM-02 |
| D-06 凭证与邀请分离 | 独立 `RegistrationGrant`、`OrganizationInvitation`、`OrganizationJoinLink`；邮箱/角色/有效期/消费状态服务端固定 | 新/已有身份接受、撤销、过期、错邮箱、重放和两入口并存通过；外部 IdP 往返不可用，JIT 固定 member 由自动化证明 | IAM-01/03/04/05/06/16 |
| D-07 私人助手 owner-only | `(tenant,user)` Onboarding 槽位 + `access_mode=private`；管理停用预检只返回计数并脱敏 private 对象与工作细节 | owner、admin、平台运营者和 Agent 受托者读取他人私人助手均拒绝；支持摘要不泄露私有字段 | IAM-08/13 |
| F07/F08 初始化 | 服务端以 Tenant 创建 provenance 决定 `entry_mode`；公司/成员/助理/开工四步可恢复；跳过只跳定制并创建安全默认助理 | 新建公司与受邀加入的完整恢复/刷新/幂等路径通过 | IAM-07/08 |
| F09 公司切换 | `commitSameOriginTenantSwitch` 校验 token tenant 后才落本地状态并清 Query cache；Direct/Group WebSocket 持续重验 membership、tenant、Agent grant 与 auth version | 两 Tenant 切换与主动退出后的 Alpha 旧 token `401`、Beta 新 token `200` 通过；WebSocket 热撤权由全量自动化覆盖 | IAM-05/12/14 |
| F11/F12 员工创建与委派 | 普通成员仅按公司政策创建 private；admin 创建 custom/company；`/employees` 提供 available/managed/governance；Agent 详情支持创建者交接与 owner 强制转交非 private Agent | 数字员工三视图、创建入口、授权、撤销、回流、未授权 API 与 private 负向通过 | IAM-09/10/14 |
| F13/F14 停用与退出 | 管理停用和本人退出均有服务端 preflight；停用可立即切断且脱敏，主动退出阻断孤儿 Agent；退出撤销对象 grant/凭证并返回已验证 fallback token | 管理停用/恢复和主动退出 UI、审计、旧 token、责任阻断及其他公司保留通过 | IAM-11/15 |
| F14 公司删除 | owner 二次认证、公司名确认、30 天停用与 restore；不立即物理删除 | 删除/恢复浏览器路径通过；后台到期物理删除未获生产/数据授权，保持未执行 | IAM-11/15 |

G6 已完成 PostgreSQL fresh/historical/downgrade/re-upgrade smoke、后端 `4465 passed`、前端 Node `118 passed`、Vitest `207 passed`（38 files）、Ruff、compileall、生产构建、能力/创作合同、`git diff --check`、IAM-01–16 浏览器矩阵、固定 ID QA 清理和两路独立终审。code-reviewer 结论为 `APPROVE`，architect 结论为 `CLEAR`；整体结论为 `local_browser_verified`。当前 dirty worktree 仍不是 immutable candidate、部署或生产证据，外部 IdP 往返与 30 天到期物理删除仍未验证。

## 13. G7 身份交付与安全合同（2026-08-16）

本节是 G8–G12 的实现合同。它补齐 G1–G6 已明确但尚未实现的邮件交付、MFA、SSO 本地往返、
到期清理、注册兼容和候选固化，不改变 Identity、membership、Agent 对象权限和产品面分离的既有结论。

### 13.1 状态与证据边界

- `queued`、`smtp_accepted`、`recipient_delivered` 和 `recipient_read` 是不同事实。当前 SMTP 协议只能证明
  本地投递任务已排队或目标 SMTP 服务器接受信封；没有 DSN/Provider webhook 时不得显示“已送达”或“已读”。
- `local_idp_emulated` 只证明受支持适配器的本地协议往返、state、JIT 和租户边界；不等于真实企业 IdP 已配置。
- `local_browser_verified`、`immutable_local_candidate`、`deployed` 和 `production_verified` 继续分离。
- 本轮只允许本地实现、测试与本地 candidate commit；不推送、不部署、不改生产、不写入真实凭证、不调用付费服务。

### 13.2 新增产品决策

#### D-17 系统邮件是可审计交付，不是后台日志副作用

- 注册验证、密码重置和公司邀请统一写入 `OutboundEmailDelivery`，业务事务只承诺“已安全受理”。
- 投递状态固定为 `queued → sending → smtp_accepted`，失败进入 `retry_wait`，超过上限进入
  `permanent_failed`；缺少平台邮件配置进入 `blocked_configuration`，不得静默跳过后仍提示成功。
- 每次尝试记录时间、稳定错误码、重试时间和脱敏回执。不得记录 SMTP 密码、完整 token、邮件正文或完整异常堆栈。
- 为支持重试，模板上下文使用认证加密存储；日志、API 和管理 UI 只显示脱敏收件地址和投递元数据。
- worker 以数据库锁领取任务并支持幂等重入；交互请求可触发一次即时后台投递，定时 worker 负责恢复和重试。

#### D-18 公司邀请默认 mail-first，人工链接是显式高风险降级

- 公司管理面默认只返回邀请 ID、目标邮箱、角色、到期时间和投递状态，不返回原始 token。
- “重新发送”必须旋转邀请 token、使旧链接立即失效、创建新的投递记录并写审计；不能因为重试而产生第二个 membership。
- 只有 owner/admin 通过二次认证后才能显式生成一次“人工复制链接”；该动作同样旋转 token、标记
  `manual_link_issued` 并审计。页面必须解释此链接含登录凭证能力，不能常驻或再次查询。
- 旧 enterprise 邮箱邀请入口和 identity-governance 入口收敛到同一服务，不再出现一条发邮件、一条回显 token 的分叉。

#### D-19 MFA 属于全局 Identity，高权限默认强制

- TOTP 密钥以认证加密 envelope 保存；恢复码只保存带域分离的 HMAC，明文只在生成时显示一次。
- `platform_operator`、`org_owner`、`org_admin` 必须启用 MFA 后才能获得相应管理 surface/capability；普通
  member 可选启用。Agent 对象管理权不能绕过公司高权限 MFA。
- 已启用 MFA 的密码登录先返回短时、单次、限次的 challenge；校验成功后才签发 access token。TOTP
  time-step 防重放，恢复码单次消费，失败受速率限制。
- 密码重置不自动关闭 MFA。启用、关闭、恢复码重置和受控管理员重置都递增 `auth_version`，旧 token/WebSocket
  立即失效并写审计。公司 owner/admin 只可在本人已完成 MFA、再次校验密码且目标是本公司普通成员时发起恢复；
  目标 Identity 只要属于多个活跃 Tenant、拥有平台权或任一管理员 membership，就必须由平台运营者处理。
- 账户设置保留现有个人资料/密码能力，并增加独立“账户安全”区域；登录页提供 challenge 和恢复码模式。

#### D-20 企业 SSO 与公共 Social Signup 分开决策

- 租户 SSO 继续只支持登记在案的企业适配器；G10 使用测试专用的本地 IdP 模拟器验证现有
  Google Workspace/OIDC 协议适配，不新增或宣传通用 `oauth2` Provider 类型。
- JIT 首次加入只能生成 `member`，不得成为首位管理员或 owner；state、browser nonce 和授权码均单次使用。
- Google/GitHub 公共 OAuth 只允许已绑定 Identity 登录。未知邮箱继续返回受控拒绝，公共 Social Signup
  endpoint 保持 `410`；未来若开放必须另立账号创建、同意、风控、合并与恢复合同。
- 真实外部 IdP 凭证、管理员配置和跨域往返仍是外部门禁，不能由本地模拟证据替代。

#### D-21 30 天后清理的是公司数据，不是跨公司 Identity

- owner 申请删除后仍保留 30 天恢复期。到期后由 `TenantDeletionJob` 执行 dry-run、legal hold 检查、
  加锁分批清理、对象存储清理和最终复核；每一步可幂等重入。
- 清理范围包含目标 Tenant 的 membership、Agent、会话、Workspace、Task、Run、Artifact、凭证、邀请、
  订阅/用量业务数据及含客户内容的审计 payload。全局 Identity 若仍属于其他 Tenant，绝不删除。
- 为合规与防重放保留最小墓碑：原 tenant UUID、不可逆名称摘要、申请/到期/完成时间、原因、计数摘要和
  receipt hash；不保留公司名称、邮箱、消息、文件、token、密钥或业务正文。
- legal hold 默认阻断 purge；解除 hold 是单独的高风险审计动作。dry-run 发现未知外键、文件失败或计数漂移时
  fail closed，不把 Tenant 标为已清理。
- 本轮只对隔离本地 fixture 执行真实 purge；生产数据清理需要单独授权、备份和发布门禁。

#### D-22 `/auth/register/init` 是唯一新客户端入口

- Web 前端切换到 `/auth/register/init`。`/auth/register` 只保留显式兼容路由、等价安全校验、弃用响应头和测试。
- 兼容路由至少保留一个已发布周期；只有生产调用遥测归零且发布计划获批后才能移除。本地候选不得凭猜测删除。

#### D-23 candidate 与外部发布门禁

- G12 在同一工作树完成全量测试、迁移、双租户多身份浏览器矩阵、SMTP capture、SSO emulator、MFA、purge
  fixture 和两路独立终审后，才允许创建本地 immutable commit 并记录 SHA。
- commit 只证明 `immutable_local_candidate`。真实 SMTP 服务、真实 IdP、远程发布、生产迁移和生产业务流分别
  需要授权与独立 evidence receipt；任何一项缺失都必须在报告中保持未验证。

### 13.3 新状态机

```text
OutboundEmailDelivery:
queued -> sending -> smtp_accepted
                  -> retry_wait -> sending
                  -> permanent_failed
queued/retry_wait -> blocked_configuration -> queued (配置修复并显式重试)

Identity MFA:
disabled -> enrollment_pending -> enabled
enabled -> recovery_codes_rotated -> enabled
enabled -> disabled (二次认证)
enabled -> administrative_reset_required -> disabled (受控公司或平台恢复流程)

Tenant deletion:
scheduled -> restored
scheduled -> eligible -> dry_run_passed -> purging -> purged
eligible/dry_run_passed/purging -> held | failed
held/failed -> dry_run_passed (问题解决并显式恢复)
```

### 13.4 当前到目标的实现映射

| 领域 | 当前事实 | G8–G12 目标落点 |
|---|---|---|
| 邮件发送 | `system_email_service.py` 直接 SMTP，未配置时仅日志跳过；无持久状态 | `OutboundEmailDelivery` 模型/迁移、加密模板上下文、dispatcher/worker、状态/重试 API 与本地 SMTP capture |
| 公司邀请 | `enterprise.py` 会发邮件；`identity_governance.py` 返回原始 token；`CompanyAdmin.tsx` 展示 token | 统一 invitation service；mail-first 创建、状态、旋转重发、二次认证人工链接；管理 UI 不默认回显 token |
| 验证/重置 | endpoint 存在但只 mock 邮件函数 | 统一 outbox；防枚举响应；本地 SMTP 正文、链接、单次/过期/重放测试 |
| MFA | G9 已实现 Identity 加密 TOTP、HMAC recovery、数据库 challenge、角色强制策略、账户安全 UI、审计与热失效 | G12 在最终候选上复跑双 Tenant 多身份与 desktop/390px 浏览器矩阵 |
| 企业 SSO | G10 已完成 Google Workspace/OIDC 适配器的 loopback IdP 往返、opaque state、同浏览器绑定、nonce、PKCE、JWKS/RS256、JIT member 与错租户/state/code 重放拒绝 | G12 在最终候选复跑 desktop/390px；真实企业 IdP 继续以 `provider_verified=false` 作为外部门禁 |
| Social OAuth | G10 已锁定 Google/GitHub existing-identity sign-in-only；signup `410` 与不创建数据由 HTTP/前端回归证明 | G12 复跑全量合同；未来开放 signup 必须新立产品与安全合同 |
| 删除 | G11 已实现 30 天停用/恢复、job/tombstone/hold/dry-run/受控 worker | 隔离 PostgreSQL 32 项真实清理和跨公司 Identity 保留通过；G12 复跑全量门禁，生产 purge 继续禁用 |
| 注册入口 | Web 已使用 `/auth/register/init` | 兼容 `/auth/register` 等价委托并带弃用/Sunset 合同；G12 复跑全量与浏览器注册路径 |
| 候选 | 当前 dirty worktree、无新 SHA | 全门禁通过后本地 commit + SHA-bound evidence；外部/生产继续保持 gate |

### 13.5 追加验收矩阵

| ID | 场景 | 正向断言 | 必做负向断言 |
|---|---|---|---|
| IAM-17 | 公司邀请投递 | mail-first、可查状态、旋转重发、SMTP capture 收到正确链接 | API/UI 不泄露 token；旧链接、重放、跨租户、非管理员重发拒绝 |
| IAM-18 | 验证/重置邮件 | 防枚举响应、队列恢复、链接单次且过期 | 未配置不伪报已发；日志/回执不含 token/密码/正文 |
| IAM-19 | MFA 注册与登录 | TOTP 确认、单次 challenge、恢复码登录 | 错码、重放、过期、暴力尝试、跨 Identity challenge 拒绝 |
| IAM-20 | MFA 权限与恢复 | 高权限未启用时 surface/API fail-closed；变更后旧会话失效 | org admin 不能重置他人 Identity；密码重置不能绕过 MFA |
| IAM-21 | SSO 本地往返 | 支持适配器、state/nonce、JIT member、tenant 绑定 | 错 tenant、state/code 重放、未知 provider、首位管理员提升拒绝 |
| IAM-22 | Social Signup 策略 | 已绑定 OAuth 登录保持可用 | 未绑定账号不创建 Identity；signup endpoint 继续 `410` |
| IAM-23 | 到期清理 | dry-run、hold、批次重入、内容/文件清理、墓碑 receipt | 未到期、hold、未知依赖、其他 Tenant/Identity 数据绝不删除 |
| IAM-24 | 注册与候选 | Web 走 `/register/init`；兼容路由等价；证据绑定本地 SHA | dirty/失败门禁不能冻结；无发布授权不能宣称 deployed/production |

### 13.6 G8–G12 顺序与停止条件

1. G8 先统一邮件交付与邀请；没有持久状态、token 仍默认回显或 SMTP capture 未通过时不进入完成声明。
2. G9 实现 Identity MFA；高权限 API 仍可绕过、旧 token 未失效或恢复码可重放时阻断。
3. G10 完成支持适配器的本地 IdP 往返并锁死 Social Signup；不得为测试便利开放通用 OAuth2 产品入口。
4. G11 实现 purge orchestration；只对隔离 fixture 实删。dry-run 漂移、legal hold 或跨租户影响任一存在时阻断。
5. G12 切换注册入口并完成全量验收、清理和独立终审；所有门禁通过后才固化本地 candidate SHA。

### 13.7 G11 本地实现与验证记录

- 物理清理执行器只有在 `ALLOW_LOCAL_TENANT_PURGE=true`、开发/测试环境、loopback PostgreSQL、
  `clawith_g11_purge_*` 数据库和 `g11-purge-*` Tenant 同时成立时才允许删除；公共 API 和浏览器均无 execute。
- 动态外键图、无主键/未知共享依赖、跨 Tenant 行、运行中 Agent/Run、待发邮件、文件存在性和最终计数任一
  不确定即 fail closed。partial schema migration 也会拒绝，不会猜测续建。
- 一次性 PostgreSQL smoke 通过 32 项断言，并在结束时销毁数据库与临时存储；桌面平台运营 UI 完成
  dry-run、operations hold、release，5 个 Identity/User 与 3 个 Tenant 的浏览器夹具精确清零。
- 定向后端 `105 passed`，前端 Node `125 passed`、Vitest `207 passed`、生产 build、Ruff 与 compileall 通过。
  这些证据只支持 `isolated_postgres_purge_verified`，不支持生产执行、部署或 production verified。

### 13.8 G12 本地候选收口记录

- Web 唯一新入口为 `/auth/register/init`；`/auth/register` 只做等价委托，并返回 deprecation、sunset 与 successor
  link。注册账号不会自动消费组织角色；新建公司、接受邀请、切换 membership 分别由账户 capability、组织凭证和
  新 membership-scoped token 驱动。
- desktop 与 390×844 均完成 owner、admin、member、second-owner、platform-operator 五身份双 Tenant 矩阵。
  owner/admin/member/platform 的 surface 与直接路由负向均符合后端 `available_surfaces`，没有以 localStorage 作为授权事实。
- 后端全量 `4496 passed`（13 个依赖弃用 warning）；前端 Node `125 passed`、Vitest `207 passed`（38 files）和
  production build；Ruff、compileall、`git diff --check`、30 templates/17 skills/141 tools capability contract、
  creative contract `115 passed` 均通过。
- loopback SMTP smoke、MFA HTTP/PostgreSQL `35` 项断言与 `19` 条审计、OIDC emulator `46` 项断言、隔离 purge
  `32` 项断言、PostgreSQL fresh/historical/downgrade/re-upgrade migration 全链均通过并清理 fixture。
- code reviewer 为 `APPROVE`。architect 首轮发现 `auth.py` tenant-switch redirect 的语法 blocker；修复后 API 编译、
  Uvicorn 启动、认证/权限专项 `100 passed` 及上述全量门禁重新通过，复审为 `CLEAR`。
- 该文档随候选 commit 一并固化，exact candidate SHA 由提交后的 Git、`/api/version` 与前端 footer 记录。本轮只形成
  `immutable_local_candidate`；外部 SMTP、真实企业 IdP、生产 purge、推送、部署和 production browser flow 均未验证。
