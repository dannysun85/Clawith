# 导航与页面归属事实基线

- 状态：`navigation-v2-validated + four-p0-scope-frozen`
- 日期：2026-08-18
- 目的：给每个一级入口一个唯一职责，清除“功能都有，但用户不知道从哪里开始”的问题

## 1. 当前路由事实

`frontend/src/App.tsx` 已注册 `/work` 与 `/employees`，有租户上下文的根路径默认进入 `/work`；`/dashboard`、`/plaza`、`/agents/*`、`/groups/*`、`/quality-reviews/*`、`/enterprise`、`/okr`、`/account/subscription` 等旧路由继续保留。

`Layout.tsx` 当前按四个业务目录呈现：`工作`（工作台、协作群组）、`团队`（我的助理、默认折叠的历史助理兼容入口、一个数字员工中心入口）、`经营`（公司概览、目标与复盘、团队知识），以及仅公司管理员可见的 `管理`（企业管理）。左侧栏是导航而不是员工数据库，不再按公司人数枚举完整员工列表；`历史助理` 不是一级功能，也不是可创建的新角色。公司切换位于业务目录上方；账户菜单承载账户设置、套餐与用量、平台运营和 SaaS 能力治理，后两项只对对应平台角色显示。

本次仅调整导航层级和命名，不移动权威页面：`目标与复盘` 仍使用 `/okr`，`团队知识` 仍使用 `/plaza`，`企业管理` 仍使用 `/enterprise`。`/plaza` 中现有员工市场暂作兼容入口，后续再作为独立产品流程归入数字员工中心；本切片不复制或改写招聘逻辑。

## 2. 页面逐项审查

| 页面/入口 | 当前真实职责 | 重叠/错位 | 目标归属 | 处理决定 |
|---|---|---|---|---|
| Workbench | 任务捕获、工作说明确认、执行者/能力预检、跨 Runtime 工作索引 | 新入口，不能演变为第二套 Runtime | 默认任务入口 | `/` 默认进入；只创建/读取真实 Task、Run、Deliverable 与 Artifact |
| Dashboard | viewer-scoped Agent/Work 摘要；公司资源聚合仅对 `company.analytics.view` | 普通 member 当前仍可从 token API/topology 重建公司资源 | 按权限裁剪的运营概览 | 保留 `/dashboard`；member 只看个人/可见对象，admin/owner 才看公司 token/cache；深链 `/employees` |
| OKR | 目标、KR、看板、日报和管理员设置链接；进度可引用已完成工作证据 | 当前列表/报告/Agent Tool 缺对象级 viewer policy | 经营 / 目标与复盘 | 保留 `/okr`；company Objective 对成员公开，个人/Agent/日报按本人、对象 grant、admin/owner 裁剪 |
| Plaza | 团队/我的 Experience；当前页面仍可进入员工市场 | 经验与招聘共享入口但不共享生命周期 | 经营 / 团队知识 | 保留 `/plaza` 和现有 Talent Market 兼容流程；不混合 Experience 与 Agent 模板数据模型，后续再迁移市场入口 |
| Groups | 群组树、会话、消息、成员、Workspace；可接收工作台 Group Task | 不应被当成组织架构或无责任人的黑盒编排 | 可见多人协作 | 保留；每项工作固定 Task、owner Agent、session、参与者与 correlation |
| Digital Employees | `/employees` 的协作网络、完整可见名册、筛选和添加入口 | 不能把完整名册继续塞进侧栏或公司概览 | 长期角色管理 | 桌面默认协作网络，移动默认员工名册；对话和有权限的设置继续深链既有 Agent 页面 |
| Agents | `/agents/:id/chat|directory|settings` | Agent 详情仍承载较多专业设置 | 单个员工的执行与配置现场 | 旧 Agent 深链、权限与设置保持，不再承担公司级员工索引 |
| Enterprise | info/users/invites/org/tools/skills/approvals/audit/okr/subscription 等 | 租户治理与平台路由曾混在一起；页面过宽 | 公司治理控制面 | 保留管理员入口；Provider/model 永远跳 SaaS Admin |
| Subscription | `/account/subscription` 当前混合个人用量、公司汇总、流水和订单 | 普通 member 可读取 tenant 财务与其他消费主体 | member“我的用量” + admin“公司套餐与用量” + owner“账单管理” | 共享账本但使用不同服务端投影；admin 只有 billing.view，owner 才有 billing.manage |
| Deliverable | chat 中发起、结果卡、右侧详情、独立 reviewer 路由，并由工作台跨员工聚合 | 必须保持结果属于产出者，不能再次挤入 composer | 正式产物生命周期 | 不做孤立文件库；Agent/Group 时间线报告，右侧详情检查与交付 |
| Workspace | Agent/Group 侧栏文件、预览、编辑和版本 | 用户易误认为正式交付或公司网盘 | 工作现场 | 保留现有所有权；Artifact 指向 Workspace 文件；不替代 Deliverable |

## 3. 当前导航

### 3.1 普通成员

```text
工作
  工作台
  协作群组

团队
  我的助理 · <自定义名称>
  历史助理（仅旧数据存在时默认折叠）
  数字员工 · <可见数量>

经营
  公司概览
  目标与复盘
  团队知识

账户
  我的用量
  设置
```

### 3.2 公司管理员增加

```text
管理
  企业管理

账户
  公司套餐与用量（billing.view）
  账单管理（仅 billing.manage）
```

### 3.3 平台管理员增加

```text
平台运营
  租户与注册码
  生产问题与发布证据

SaaS 能力治理
  套餐/Credits
  模型路由
  媒体路由
  Provider 账号池
```

平台管理员没有租户时直接进入 SaaS 控制台，不经过租户 Onboarding，也不伪装成租户成员。

## 4. 页面所有权规则

### 工作台

- 所有用户的默认入口；拥有意图捕获、工作类型、执行者提议和跨运行时工作索引。
- 不拥有 Agent 设置、Provider 配置或文件内容。

### 仪表盘

- member 只展示自己的工作、行动、产出与 viewer-scoped Agent；不请求或重建公司 token/cache/Credits。
- org_admin/org_owner 通过 `company.analytics.view` 增加公司级健康、工作量、风险、目标和资源聚合。
- 点击数据必须进入权威对象页面，不在 Dashboard 内复制管理流程。

### OKR

- 拥有 Objective、KR、报告和 OKR 政策。
- company Objective 对当前 tenant 成员可读；用户目标/日报只对本人和 admin/owner，Agent 对象按 `use/manage` grant 投影；管理版公司复盘仅 admin/owner。
- 可以引用 Delivery/Artifact 作为进展证据；不能从 Agent 文本自动把 KR 标为完成。

### 广场

- 经验库拥有 Experience 生命周期；员工市场拥有模板/角色发现。
- 招聘成功后进入数字员工配置；发布经验不会创建 Agent。

### Groups

- 拥有群成员、群会话、群公告/记忆和 Group Workspace。
- 不拥有公司成员目录，也不复制 Agent 的私有 Workspace。

### 数字员工与我的助理

- `我的助理` 是固定关系入口；`数字员工` 是长期角色花名册。
- `历史助理` 只承接旧版本内容回访，不计入员工花名册，也不能从员工市场新增。
- 左侧只保留一个带数量的 `数字员工` 入口；完整员工名册、搜索、筛选与协作网络统一由 `/employees` 承载。
- `添加员工` 是数字员工中心页头和空状态中的主动作，不作为拓扑图里的伪节点，也不在公司概览复制一套流程。
- 默认从员工市场按岗位模板添加；高级用户可以自定义。创建前必须确认职责、交付边界和可见范围，普通成员不能创建全公司可用员工。
- `仅创建` 返回员工名册并高亮新员工；`创建并开始对话` 进入既有 Agent 消息界面。只有拥有管理权的用户看到设置动作。
- Agent chat 是执行和沟通现场；Settings 仅对有管理权的人显示。

### 企业设置、套餐、SaaS 管理

- 企业设置只管理租户策略与资源。
- “我的用量”只向 member 解释个人可归因用量与 entitlements，不显示公司余额、流水、订单、支付主体或 Provider Key。
- org_admin 的 `company.billing.view` 只提供公司套餐、Seats 与聚合用量；org_owner 的 `company.billing.manage` 才提供订单、账单资料、支付与续费。
- SaaS 管理拥有 Provider、模型、路由、账号池和全局计费规则。

## 5. Deliverable 与 Workspace 的展示规则

1. 用户提交前：composer 可以出现紧凑的工作说明草稿卡。
2. 提交后：Task/Deliverable 进入聊天时间线，由负责的 Agent/Group 报告进展。
3. Artifact 产生后：右侧 Workspace 自动定位到实际文件，但不抢走用户锁定的文件。
4. 需要检查/审批时：时间线只显示摘要和“查看交付详情”；完整操作在右侧详情或 reviewer 页面。
5. 完成交付后：结果仍属于产出者的消息；输入框只用于下一条输入。
6. 工作台显示跨 Agent 的摘要和状态，不复制整个交付面板。

## 6. 路由兼容状态

- `/work` 已新增，租户根路径固定进入 `/work`；`/employees` 负责数字员工中心；`/dashboard` 继续作为公司概览深链存在。
- 保留 `/agents/:id/chat|directory|settings`、`/groups/*`、`/quality-reviews/:id`、`/okr`、`/plaza`、`/enterprise`、`/account/subscription`。
- 旧链接永不因导航改名失效；新页面只产生到旧权威页面的深链。
- Onboarding 已变更落点到 `/work`，没有改变已有助手 Agent ID。
- 历史助理沿用原 `/agents/:id/*` 深链；分类只改变展示和员工统计，不迁移会话或 Workspace。
- 狭窄屏幕上工作台、审批和 Deliverable 详情必须可用；复杂管理员页面仍可声明 desktop-first。

## 7. 当前剩余的定位与验证问题

1. 普通成员、`agent_admin` 和公司管理员入口矩阵已在提交前工作树实跑；仍需在新 immutable candidate SHA 上重启前后端并复验 release identity。
2. Agent 详情仍包含较多 Tab；需要通过用户任务而不是继续加一级栏目来渐进暴露高级设置。
3. 发现中心同时承载经验与员工入口，浏览器验证必须证明用户不会把“发布经验”和“招聘员工”当作同一动作。
4. Workspace、聊天附件和 Deliverable 已有文案与深链边界，但图片、视频、PPT 的真实右侧预览和正式交付仍需逐类核验。
5. 普通成员与 `agent_admin` 已无法进入企业设置、邀请、账户运营或 SaaS 管理入口；仍需在候选 SHA 上补跨租户 API IDOR 专项，并持续保证错误和日志不泄露 Provider Key。
6. OKR 证据链已实现，但需要验证审批 Artifact 更新、证据快照和旧进度路径不会互相覆盖。
7. 历史助理当前只提供无损回访入口；后续归档或转员工必须作为独立、可撤销并显示席位影响的产品流程设计。

## 8. 完成标准

- 每个一级页面只有一个主要职责，所有次要动作深链到权威页面。
- 普通成员、公司管理员、平台管理员看到不同且可解释的导航。
- 默认路径是“开始工作”，而不是“先管理 Agent”。
- 新导航上线时所有现有深链、浏览器刷新、回退和权限守卫通过回归测试。

## 9. 本地验收边界（2026-08-15）

- 已验证：数字员工单一侧栏入口、可见数量、网络/名册双视图、管理员与普通成员差异、添加员工、仅创建回流、继续对话、设置深链、移动端默认视图、加载失败重试。
- 已验证：完整员工只出现在 `/employees`，无协作边的员工仍进入名册；公司概览只保留摘要与深链，不复制完整拓扑和招聘流程。
- 已清理验收期间创建的临时用户、身份、员工、参与者和审计数据。
- 尚未声称：Git candidate、部署、生产环境、付费 Provider 或生产业务流验证。

## 10. 导航 V2 浏览器验收（2026-08-15）

- 管理员桌面端按 `工作 / 团队 / 经营 / 管理` 四组呈现；普通成员只显示前三组，页面和链接中均不存在 `/enterprise` 入口。
- `我的助理` 始终显示固定关系名称，并将 Agent 自定义名称作为第二行身份信息；旧助手存在时才显示默认折叠的 `历史助理` 兼容组，展开后继续使用原 `/agents/:id/chat` 深链。
- 已逐项点击验证 `/work`、`/employees`、`/okr`、`/plaza`、`/enterprise`；导航命名变化没有改变权威路由。
- 已在 `390×844` 视口验证移动端抽屉：普通成员目录完整、无横向溢出，进入 `团队知识` 后抽屉自动关闭；桌面折叠态宽度为 `68px`，助理第二行身份信息隐藏，展开后恢复。
- 浏览器验收录屏保存在本机 `~/.config/browser-harness/agent-workspace/recordings/clawith-navigation-v2-local`；管理员、普通成员和移动端截图保存在本地可视化目录。
- 验收使用的临时身份、租户、用户、两名 Agent、参与者、Onboarding 与自动生成的 OKR 设置均已按精确 ID 清理并复查为零；内置助手模板未删除。
- 以上结论仅对应当前本地工作树；未提交、未部署，也不代表生产环境或生产业务流已验证。

## 11. 角色化产品面 V3 约束

导航 V2 的员工工作面和 `/employees` 员工中心继续保留；后续权限重构只增加明确产品面，不把左侧栏重新变成员工数据库：

| 产品面 | 使用者 | 默认落点 | 导航规则 |
|---|---|---|---|
| 员工工作面 | 所有有效公司成员 | `/work` | 保留工作、团队、经营；内容按能力裁剪 |
| Agent 受托管理 | 拥有具体 Agent `manage` 的成员 | `/employees` 或 Agent 详情 | 只增加“我管理的员工”和对象内操作，不显示公司管理目录 |
| 公司管理面 | `org_admin/org_owner` | `/company-admin`（兼容期可承接 `/enterprise`） | 独立二级导航；owner-only 高风险页面单独守卫 |
| 平台运营面 | `platform_operator` | `/admin/platform` | 独立外壳；不显示我的助理、数字员工或 Group |

管理员默认仍使用员工工作面；“进入管理”是显式模式切换。平台运营者进入公司只能使用真实 membership 或有范围、时限和审计的支持会话。导航与路由必须消费服务端 `available_surfaces/effective_capabilities`，页面按钮再叠加对象级 `access_level`。
