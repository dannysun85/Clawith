# 导航与页面归属事实基线

- 状态：`active-design-baseline`
- 日期：2026-07-31
- 目的：给每个一级入口一个唯一职责，清除“功能都有，但用户不知道从哪里开始”的问题

## 1. 当前路由事实

`frontend/src/App.tsx` 当前没有 `/work`：根路径进入 `/dashboard`；主要租户路由包括 `/dashboard`、`/plaza`、`/agents/*`、`/groups/*`、`/quality-reviews/*`、`/enterprise`、`/okr`、`/account/subscription`。

`Layout.tsx` 当前一级导航显示 Dashboard、OKR、Plaza、Groups；其下是一整块“智能体”列表，私人助手和数字员工混在其中。企业设置、套餐和 SaaS 管理位于账户菜单。

## 2. 页面逐项审查

| 页面/入口 | 当前真实职责 | 重叠/错位 | 目标归属 | 处理决定 |
|---|---|---|---|---|
| Dashboard | Agent 状态、任务摘要、Token、全局活动、新增 Agent | 像员工监控台，也像首页 | 公司运营概览 | 保留；不再作为默认任务入口；招聘入口降级为辅助动作 |
| OKR | 目标、KR、看板、日报和管理员设置链接 | 与 Dashboard 的绩效概览重叠 | 目标与结果管理 | 保留一级组织入口；只消费已批准工作证据，不执行任务 |
| Plaza | 团队/我的经验，draft/published/retired | 名称像社交广场；员工发现另在弹窗 | 发现中心：经验库 + 员工市场 | 保留 `/plaza`；增加明确分区，不混合经验与招聘数据模型 |
| Groups | 群组树、会话、消息、成员、Workspace | 容易被当成万能 Agent 团队或通讯录 | 可见多人协作 | 保留；工作仍需要责任主体和 Task/Run |
| Agents | 左侧列表 + chat/directory/settings，详情内部 Tab 很多 | 私人助手混入；执行、文件、配置和招聘入口集中 | 数字员工花名册与员工执行现场 | 拆分“我的助理”导航；长期员工保留；高级配置渐进显示 |
| Enterprise | info/users/invites/org/tools/skills/approvals/audit/okr/subscription 等 | 租户治理与平台路由曾混在一起；页面过宽 | 公司治理控制面 | 保留管理员入口；Provider/model 永远跳 SaaS Admin |
| Subscription | `/account/subscription` 用量/流水/订单；Enterprise 有购买/计划 | 两处都叫套餐 | 成员可见“套餐与用量” + 管理员“购买与配置” | 共享数据源；前者读用量与账单，后者管理订阅；互相深链 |
| Deliverable | chat 中发起、结果卡、右侧详情、独立 reviewer 路由 | 缺少跨员工索引；曾错误挤入 composer | 正式产物生命周期 | 不做孤立文件库；由工作台聚合，Agent/Group 时间线报告，右侧详情交付 |
| Workspace | Agent/Group 侧栏文件、预览、编辑和版本 | 用户易误认为正式交付或公司网盘 | 工作现场 | 保留现有所有权；Artifact 指向 Workspace 文件；不替代 Deliverable |

## 3. 目标导航

### 3.1 普通成员

```text
工作台
我的助理 · <自定义名称>
Groups

数字员工
  搜索员工
  最近使用

组织
  仪表盘
  OKR
  广场

账户
  套餐与用量
  设置
```

### 3.2 公司管理员增加

```text
企业设置
  公司与成员
  数字员工治理
  工作模板
  Skills 与 Tools
  审批与审计
  套餐管理
```

### 3.3 平台管理员增加

```text
SaaS 管理
  套餐/Credits
  模型路由
  媒体路由
  Provider 账号池
  租户与注册码
  生产问题与发布证据
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

## 6. 路由兼容策略

- 新增 `/work`，第一阶段 `/` 通过 feature flag 选择 `/dashboard` 或 `/work`。
- 保留 `/agents/:id/chat|directory|settings`、`/groups/*`、`/quality-reviews/:id`、`/okr`、`/plaza`、`/enterprise`、`/account/subscription`。
- 旧链接永不因导航改名失效；新页面只产生到旧权威页面的深链。
- Onboarding 先变更落点，不改变已有助手 Agent ID。
- 狭窄屏幕上工作台、审批和 Deliverable 详情必须可用；复杂管理员页面仍可声明 desktop-first。

## 7. 当前最严重的定位问题

1. 产品以“已有页面集合”组织，而不是以用户任务生命周期组织。
2. 默认首页偏公司/员工监控，普通成员没有清晰的首次任务入口。
3. 私人助手既是注册关系又被展示为普通员工，破坏了私人协调与公司资源边界。
4. Plaza 的旧名字、Experience 新语义和 Talent Market 分散入口造成发现逻辑割裂。
5. Workspace、聊天附件和 Deliverable 都能展示文件，但正式产物权威没有在导航层明确。
6. 企业设置、套餐详情、SaaS 后台虽然权限已分层，用户仍缺少清晰的“谁管理什么”解释。

## 8. 完成标准

- 每个一级页面只有一个主要职责，所有次要动作深链到权威页面。
- 普通成员、公司管理员、平台管理员看到不同且可解释的导航。
- 默认路径是“开始工作”，而不是“先管理 Agent”。
- 新导航上线时所有现有深链、浏览器刷新、回退和权限守卫通过回归测试。
