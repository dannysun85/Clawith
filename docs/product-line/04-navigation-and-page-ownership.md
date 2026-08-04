# 导航与页面归属事实基线

- 状态：`next-slice-worktree-implemented`
- 日期：2026-08-03
- 目的：给每个一级入口一个唯一职责，清除“功能都有，但用户不知道从哪里开始”的问题

## 1. 当前路由事实

`frontend/src/App.tsx` 已注册 `/work`，有租户上下文的根路径默认进入 `/work`；`/dashboard`、`/plaza`、`/agents/*`、`/groups/*`、`/quality-reviews/*`、`/enterprise`、`/okr`、`/account/subscription` 等旧路由继续保留。

`Layout.tsx` 当前按三层呈现：`工作`（工作台、协作群组）、`协作角色`（我的助理、按需出现的历史助理、Agent 员工）和 `组织`（公司概览、OKR、发现中心、管理员可见企业设置）。`历史助理` 是兼容旧数据的迁移分组，不是可创建的新角色。账户菜单承载账户设置、套餐与用量、平台运营和 SaaS 能力治理；后两项只对对应平台角色显示。

## 2. 页面逐项审查

| 页面/入口 | 当前真实职责 | 重叠/错位 | 目标归属 | 处理决定 |
|---|---|---|---|---|
| Workbench | 任务捕获、工作说明确认、执行者/能力预检、跨 Runtime 工作索引 | 新入口，不能演变为第二套 Runtime | 默认任务入口 | `/` 默认进入；只创建/读取真实 Task、Run、Deliverable 与 Artifact |
| Dashboard | 长期 Agent 状态、服务端 Work 摘要、Token、全局活动、新增 Agent | 仍有部分员工管理辅助动作 | 公司运营概览 | 保留 `/dashboard`；不再作为默认任务入口；私人助手不计入员工统计 |
| OKR | 目标、KR、看板、日报和管理员设置链接；进度可引用已完成工作证据 | 不能从 Agent 自述自动完成 KR | 目标与结果证据 | 保留一级组织入口；仅接受完成 Task 或带批准 Artifact 的成功 Deliverable |
| Plaza | 团队/我的 Experience；发现中心可进入员工市场 | 经验与招聘共享入口但不共享生命周期 | 发现中心：经验库 + 员工市场 | 保留 `/plaza` 和 Talent Market；不混合 Experience 与 Agent 模板数据模型 |
| Groups | 群组树、会话、消息、成员、Workspace；可接收工作台 Group Task | 不应被当成组织架构或无责任人的黑盒编排 | 可见多人协作 | 保留；每项工作固定 Task、owner Agent、session、参与者与 correlation |
| Agents | 分离后的我的助理、历史助理、Agent 员工列表及 chat/directory/settings | Agent 详情仍承载较多专业设置 | 长期角色与执行现场 | 当前 companion、迁移历史和长期员工分别展示；旧 Agent 深链、权限与设置保持 |
| Enterprise | info/users/invites/org/tools/skills/approvals/audit/okr/subscription 等 | 租户治理与平台路由曾混在一起；页面过宽 | 公司治理控制面 | 保留管理员入口；Provider/model 永远跳 SaaS Admin |
| Subscription | `/account/subscription` 用量/流水/订单；Enterprise 有购买/计划 | 两处都叫套餐 | 成员可见“套餐与用量” + 管理员“购买与配置” | 共享数据源；前者读用量与账单，后者管理订阅；互相深链 |
| Deliverable | chat 中发起、结果卡、右侧详情、独立 reviewer 路由，并由工作台跨员工聚合 | 必须保持结果属于产出者，不能再次挤入 composer | 正式产物生命周期 | 不做孤立文件库；Agent/Group 时间线报告，右侧详情检查与交付 |
| Workspace | Agent/Group 侧栏文件、预览、编辑和版本 | 用户易误认为正式交付或公司网盘 | 工作现场 | 保留现有所有权；Artifact 指向 Workspace 文件；不替代 Deliverable |

## 3. 当前导航

### 3.1 普通成员

```text
工作
  工作台
  协作群组

协作角色
  我的助理 · <自定义名称>
  历史助理（仅旧数据存在时显示）
  Agent 员工

组织
  公司概览
  OKR
  发现中心

账户
  套餐与用量
  设置
```

### 3.2 公司管理员增加

```text
组织
  企业设置

账户
  套餐与用量
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

- 只展示公司级健康和运营摘要：员工活跃、工作量、风险、目标进展、Credits 趋势。
- 点击数据必须进入权威对象页面，不在 Dashboard 内复制管理流程。

### OKR

- 拥有 Objective、KR、报告和 OKR 政策。
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
- Agent chat 是执行和沟通现场；Settings 仅对有管理权的人显示。

### 企业设置、套餐、SaaS 管理

- 企业设置只管理租户策略与资源。
- 套餐与用量向成员解释权益、Credits 和订单，不暴露 Provider Key。
- SaaS 管理拥有 Provider、模型、路由、账号池和全局计费规则。

## 5. Deliverable 与 Workspace 的展示规则

1. 用户提交前：composer 可以出现紧凑的工作说明草稿卡。
2. 提交后：Task/Deliverable 进入聊天时间线，由负责的 Agent/Group 报告进展。
3. Artifact 产生后：右侧 Workspace 自动定位到实际文件，但不抢走用户锁定的文件。
4. 需要检查/审批时：时间线只显示摘要和“查看交付详情”；完整操作在右侧详情或 reviewer 页面。
5. 完成交付后：结果仍属于产出者的消息；输入框只用于下一条输入。
6. 工作台显示跨 Agent 的摘要和状态，不复制整个交付面板。

## 6. 路由兼容状态

- `/work` 已新增，租户根路径固定进入 `/work`；`/dashboard` 继续作为公司概览深链存在。
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
