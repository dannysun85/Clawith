# Clawith 企业订阅(Subscription)功能实现设计

> **目标**:将云端 `cloud.clawith.ai/enterprise#subscription` 的企业订阅功能落地到本地(多租户 SaaS)。
> **现状**:云端已有(Stripe + 订阅 + 积分包 + 权益 + 跨租户管理);本地无,需从零新建,复用现有 `quota_guard` 强制层与用量记录。
> **方法**:逐模块设计,地基先行,后续模块按序追加。本文档为本地实现的依据。

---

## 0. 背景与目标

- 云端 `cloud.clawith.ai` 是同一套 Clawith 代码的 SaaS 部署,其 `enterprise#subscription` 已有完整订阅能力。
- 本地开源版**无**订阅主体(前后端均无 Plan/Subscription/Credit/Payment),只有基于 `Tenant.default_*` 的静态配额。
- 本地部署形态:**多租户 SaaS**(需跨租户管理、支付、发票)。
- 本设计的核心原则:**最小侵入、向后兼容**——复用现有 `quota_guard` 强制层与配额字段,订阅权益作为新的配额来源覆盖之,无订阅时回退现有行为。

---

## 1. 云端功能拆解(逆向 `/biz/` API)

| 模块 | 云端 API | 能力 |
|---|---|---|
| 套餐 Plans | `/biz/plans`、`/biz/plans/bundles`、`/biz/plans/with-discount`、`/biz/plans/my-entitlements` | 套餐列表/打包/折扣/我的权益 |
| 订阅 Subscription | `/biz/subscriptions`、`/biz/billing/subscriptions/bulk`、`/biz/billing/toggle-auto-renew` | 当前订阅/跨租户批量/自动续费 |
| 支付 Checkout | `/biz/billing/checkout/subscribe`、`/biz/billing/checkout/topup`、`/biz/billing/checkout/{id}/status` | 订阅下单/积分充值下单/轮询状态 |
| Stripe | `/biz/billing/portal`、`/biz/billing/setup-intents`、`/biz/billing/setup-checkout-session` | 客户门户/保存卡/预存支付方式 |
| 积分 Credits | `/biz/billing/credit-transactions`、`/biz/billing/usage` | 积分流水/用量 |
| 资料 | `/biz/billing/profile`、`/biz/billing/config`、`/biz/billing/transactions` | 发票资料/前端配置/交易记录 |
| 活动 | `/biz/events/vibe-company/*` | 兑换码/奖励 |

**核心模式**:Stripe 支付的 SaaS 订阅 = 订阅(subscription) + 积分包(credit pack/topup)双轨,由权益(entitlements)驱动功能限制;支持平台管理员跨租户批量管理。

---

## 2. 本地现状盘点

### ✅ 可复用(不要重造)

| 资产 | 位置 | 复用方式 |
|---|---|---|
| 配额强制层 | `services/quota_guard.py`(4 个 `check_*` + `increment_*`) | 改造为从权益派生 |
| 配额强制点 | `api/agents.py:28`(建 agent)、`api/websocket.py:488,600`(消息+LLM) | **不动**,只改数据来源 |
| 用量记录 | `DailyTokenUsage`(`models/activity_log.py:35`)、`llm_call_ledger` 表 | 计费/统计基础 |
| 配额字段 | `Tenant.default_*`、`User.quota_*`、`Agent.max_llm_calls_per_day` | 权益落地目标/回退值 |
| 配额管理 API | `api/enterprise.py:509,534` `/tenant-quotas` GET/PATCH | 保留,订阅覆盖之 |
| 前端 tab 框架 | `pages/EnterpriseSettings.tsx` VALID_TABS+hash+条件渲染 | 加一个 subscription tab |
| 多租户字段 | `Tenant.country_region`(默认 "001") | 支付渠道路由依据 |

### ❌ 缺失(需新建)

Plan / Subscription / Credit / Entitlement / Payment / BillingProfile 的 model、api、service、schema、migration、前端页面——**全无**。

---

## 3. 地基:数据模型 + 权益驱动配额

### 3.1 新增数据模型(7 张表,全带 `tenant_id`,`plans` 除外)

#### `plans`(全局套餐定义,无 tenant_id)
```
id                    UUID PK
code                  str unique        # free / pro / enterprise,程序引用
name                  str               # 展示名
tier                  int               # 等级,排序
period                str               # monthly / yearly / permanent
price_cents           int               # 分,避免浮点
currency              str               # CNY / USD
# 关键配额提列(高频查询,quota_guard 直接读)
max_agents            int
max_llm_calls_per_day int
message_limit         int
message_period        str               # daily / monthly / permanent
max_triggers          int
credits_per_period    int               # 订阅附赠积分
features              JSON              # 额外特性,灵活扩展不改表
stripe_price_id       str?              # 接 Stripe 时
is_active             bool
sort_order            int
created_at / updated_at
```

#### `subscriptions`(租户订阅)
```
id                    UUID PK
tenant_id             UUID FK index
plan_id               UUID FK
status                str               # active / trialing / canceled / expired / past_due
period_start          datetime
period_end            datetime          # 权益失效边界
auto_renew            bool
seats                 int               # SaaS 按席位
stripe_sub_id         str?
cancel_at_period_end  bool
created_at / updated_at
# partial unique index: tenant_id WHERE status IN ('active','trialing') —— 一租户一有效订阅
```

#### `credit_balances`(积分余额,单行/租户)
```
tenant_id   UUID PK FK
balance     int
reserved    int                 # 预占(进行中扣减)
updated_at
```

#### `credit_transactions`(积分流水)
```
id            UUID PK
tenant_id     UUID FK index
delta         int               # +充值 / -消耗
balance_after int               # 快照,审计
reason        str               # subscribe / topup / consume / refund / adjust
ref_type      str?              # agent / message / order
ref_id        UUID?
created_at
```

#### `billing_profiles`(发票资料,单行/租户)
```
tenant_id    UUID PK FK
company_name / tax_id / address / city / country / email / phone
stripe_customer_id  str?       # Stripe 客户 id
created_at / updated_at
```

#### `payment_orders`(支付订单)
```
id                  UUID PK
tenant_id           UUID FK index
type                str         # subscribe / topup
plan_id             UUID?       # subscribe 时
credits             int?        # topup 时
amount_cents        int
currency            str
provider            str         # stripe / alipay / wechat / manual
provider_session_id str?        # checkout session id
provider_payment_id str?
status              str         # pending / paid / failed / canceled
created_at / paid_at
```

**Tenant 表不加冗余字段**——当前套餐通过 `subscriptions` 查(partial unique index 保证性能)。

### 3.2 权益驱动配额改造(核心)

新增 `services/entitlements.py`:
```python
async def get_tenant_entitlements(tenant_id) -> Entitlements | None:
    """当前有效订阅的权益;无订阅/过期返回 None"""
    sub = await get_active_subscription(tenant_id)
    if not sub or sub.status not in ("active", "trialing"):
        return None
    if sub.period_end and sub.period_end < datetime.now(timezone.utc):
        return None
    plan = await get_plan(sub.plan_id)
    return Entitlements(
        max_agents=plan.max_agents,
        max_llm_calls_per_day=plan.max_llm_calls_per_day,
        message_limit=plan.message_limit,
        max_triggers=plan.max_triggers,
    )
```

`services/quota_guard.py` 每个检查**只换 limit 来源**(以 LLM 配额为例):
```python
async def check_agent_llm_quota(agent_id):
    agent = await _get_agent(agent_id)
    ent = await get_tenant_entitlements(agent.tenant_id)
    # 有订阅→权益值;无订阅→现有 agent 字段(向后兼容)
    limit = ent.max_llm_calls_per_day if ent else agent.max_llm_calls_per_day
    if agent.llm_calls_today >= limit:
        raise QuotaExceeded(..., quota_type="agent_llm")
```
其余 3 个 check(`conversation_quota`、`agent_creation_quota`、`enforce_heartbeat_floor`)同理。

### 3.3 侵入点与兼容性

| 项 | 改动 |
|---|---|
| `quota_guard.py` | 4 个 `check_*` 各加 1 行 `get_tenant_entitlements` + limit 来源切换 |
| `agents.py:28` / `websocket.py:488,600` 强制点 | **不改** |
| 现有 `tenant.default_*` / `agent.max_llm_calls_per_day` | **保留**,作为无订阅回退值 |
| 现有 `/tenant-quotas` API | **保留**,订阅覆盖之 |

**向后兼容**:无订阅 → `ent=None` → 用现有字段值,行为完全不变。升级后现有租户默认无订阅,平滑过渡;管理员给某租户分配套餐后,该租户切换到权益驱动。

### 3.4 迁移策略

- 新建一个 `free` 套餐,字段值 = 现有 `Tenant` 默认值(max_agents=2, llm_calls=1000, message_limit=50...),作为"无订阅"的等价物。
- **不强制**现有租户绑定订阅——升级后默认走回退路径。
- 平台管理员可手动给租户分配套餐(阶段 0 无支付时的入口)。

### 3.5 多租户考量

- 除 `plans` 外所有表带 `tenant_id`,查询过滤。
- 跨租户:平台管理员走 `/api/billing/subscriptions/bulk` 查全部租户订阅(模块三)。
- 平台/组织管理员豁免配额——`quota_guard` 现有逻辑已对 `platform_admin`/`org_admin` 豁免,**无需改**。

### 3.6 订阅状态机与权益生效

状态:`active` / `trialing` / `canceled` / `expired` / `past_due`

**状态转换**:
```
无订阅 → trialing/active(订阅)
trialing → expired(试用结束未付费)/ active(付费)
active → active(续费成功)/ past_due(续费失败)/ canceled(用户取消)
canceled → expired(周期末)/ active(重新订阅)
past_due → active(补付)/ expired(重试期满)
expired → active(重新订阅)
```

**权益生效决策(已定)**:

| 场景 | 策略 |
|---|---|
| 升级 pro→enterprise | 立即生效 + 补差价(proration);无支付阶段0由管理员改 plan 立即生效 |
| 降级 enterprise→pro | 周期末生效(当前周期继续享高 plan,下周期降级) |
| 取消 | 周期末失效(`cancel_at_period_end=true`) |
| 过期未续费(expired) | **立即降级 free**(无 grace period,防白嫖) |
| 续费失败(past_due) | 跟随 Stripe 重试窗(3-5 天),期间权益保留;全失败 → expired → 降级 |
| 试用结束 | 降级 free |
| 权益失效后超额 agents | 停止(`status=stopped`,不删数据)→ 续费恢复 |

> **expired vs past_due 区分**:`expired`(主动不续费/取消到期)立即降级无宽限,防白嫖;`past_due`(支付失败,非用户主动)保留 Stripe 重试期,避免用户因支付系统问题(卡过期/余额不足)被误降级。

**entitlements 状态判定**:
```python
async def get_tenant_entitlements(tenant_id) -> Entitlements | None:
    sub = await get_active_subscription(tenant_id)
    if not sub:
        return None  # 无订阅 → 回退 tenant 默认值(free 等价)
    now = datetime.now(timezone.utc)
    if sub.status in ("active", "trialing", "canceled") and now < sub.period_end:
        return entitlements_from_plan(sub.plan)  # canceled 周期末前仍有效
    if sub.status == "past_due" and now < sub.period_end + STRIPE_RETRY_WINDOW:
        return entitlements_from_plan(sub.plan)  # Stripe 重试期保留
    return None  # expired / 超期 → 立即降级回退
```

**兜底定时任务**(防 webhook 丢失,复用 `services/trigger_daemon.py`,每日):
- `active/trialing` 且 `period_end < now` → 标 `expired` + 停止超额 agents
- `canceled` 且 `period_end < now` → `expired`
- `past_due` 且超 Stripe 重试窗 → `expired`
- webhook 主路径 + 定时兜底 = 状态最终一致

**超额 agent 处理**:降级/失效时,租户 agents 数 > 新 plan `max_agents` 的部分 → `status=stopped`(不删数据),续费后恢复 `active`。停止的 agent 不再调用 LLM(`quota_guard` 拒绝),但数据/配置保留。

### 3.7 配额计量(准确性)

**决策**:

| 项 | 策略 |
|---|---|
| 重置时区 | 租户时区(`tenant.timezone`),按本地日期重置 |
| 并发竞态 | 原子 UPDATE + RETURNING,合并 check+increment |
| 计费单位 | 次数配额 + tier 加权(premium=5, standard=1, basic=1) |
| 配额归属 | tenant 级共享(所有 agent 共享 plan 配额) |

**新增 `tenant_usage` 表**(tenant 级用量计数,替代 `agent.llm_calls_today`):
```
tenant_id       UUID PK
period_date     date PK        # 租户本地日期
llm_calls_used  int            # 今日加权消耗
llm_calls_limit int            # 今日上限(从 plan 派生)
messages_used   int
messages_limit  int
tokens_used     int            # token 累计(统计)
updated_at
# PK (tenant_id, period_date) —— 日期变更自动新行 = 天然重置,无需显式 reset
```

**`quota_guard` 改造(原子 UPDATE,合并 check+increment)**:
```python
TIER_WEIGHTS = {"premium": 5, "standard": 1, "basic": 1}

async def consume_llm_quota(tenant_id, model_tier, db):
    weight = TIER_WEIGHTS.get(model_tier, 1)
    ent = await get_tenant_entitlements(tenant_id)
    limit = ent.max_llm_calls_per_day if ent else tenant.default_max_llm_calls_per_day
    today = today_in_tenant_tz(tenant)
    # 原子 upsert + 条件扣减(仅当未超额)
    result = await db.execute(text("""
        INSERT INTO tenant_usage (tenant_id, period_date, llm_calls_used)
        VALUES (:tid, :d, :w)
        ON CONFLICT (tenant_id, period_date) DO UPDATE
        SET llm_calls_used = tenant_usage.llm_calls_used + :w
        WHERE tenant_usage.llm_calls_used + :w <= :limit
        RETURNING llm_calls_used
    """), {"tid": tenant_id, "d": today, "w": weight, "limit": limit})
    if result.scalar_one_or_none() is None:
        raise QuotaExceeded(..., quota_type="tenant_llm")
```

**tier 加权**:调用按 `model.tier` 权重消耗(premium 1 次=5 配额,standard=1)。权重可配置(系统设置或 plan 字段)。

**租户时区重置**:
```python
def today_in_tenant_tz(tenant) -> date:
    tz = zoneinfo(tenant.timezone or "UTC")
    return datetime.now(timezone.utc).astimezone(tz).date()
```

**A2A 归属**:同 tenant 共享无歧义;跨 tenant A2A 算被调用方(执行 LLM 的 agent 的 tenant)。

**entitlements 联动**:`ent.max_llm_calls_per_day` 是 tenant 级总额,`quota_guard` 扣 `tenant_usage`(非 `agent.llm_calls_today`)。

**向后兼容**:
- 无订阅:limit = `tenant.default_max_llm_calls_per_day`,扣 `tenant_usage`
- 现有 `agent.llm_calls_today` / `agent.max_llm_calls_per_day` 保留(回退/过渡),新调用走 `tenant_usage`
- 消息配额(`check_conversation_quota`)同理改造为 tenant 级原子计数

### 3.8 租户运营与防滥用(已定)

| 项 | 决策 |
|---|---|
| 新租户初始 | 注册时自动创建 `subscription`(plan=free, status=active, period=permanent);所有租户统一有订阅记录,`entitlements` 走 plan |
| 免费防滥用 | free plan 低配额(如 50 次/天)+ Redis 每分钟限频 + 邮箱验证码 |
| 自带 key 计费 | 租户级模型(自带 key)调用仍计 `llm_calls` 次数(防滥用),但不扣积分(无平台成本) |

**新租户注册流程**:
1. 创建 Tenant
2. 创建 `subscription`(plan=free, status=active, period_end=permanent)
3. 初始化 `credit_balances`(balance=0)
4. `Tenant.default_*` 保留作为无订阅回退(向后兼容)

**防滥用三层**:
- 配额层:free plan `max_llm_calls_per_day=50`(低)
- 限频层:Redis 计数器,每分钟 N 次/tenant(本地有 Redis 7+)
- 注册层:邮箱验证 + 验证码(防批量注册)

**自带 key 计费规则**:
- 租户级模型(`tenant_id=X`):走租户 `api_key`,不耗平台池
- 仍计 `tenant_usage.llm_calls_used`(防滥用,按 tier 加权)
- **不扣** `credit_balances`(无平台 token 成本)
- 平台级模型(`tenant_id=null`):走平台池,正常计次数 + 扣积分(若启用积分制)

---

## 4. 模块一:支付集成(多租户 SaaS 商业化核心)

### 4.1 渠道选型

| 渠道 | 适用 | 优势 | 劣势 |
|---|---|---|---|
| **Stripe** | 海外 | subscription/checkout/portal/webhook 原语成熟,云端已用 | 中国大陆不可用 |
| **支付宝/微信** | 国内 | 覆盖国内用户 | 无 subscription 原语,需自建续费;需企业资质 |
| **混合** | 全球 | 按 `tenant.country_region` 路由 | 两套实现,复杂度高 |

本地 `Tenant` 已有 `country_region` 字段(默认 "001")→ 可据此路由支付渠道,与云端一致。

### 4.2 Stripe 方案(对应云端 `/biz/billing/*`)

**订阅流程**:
1. 前端选套餐 → `POST /api/billing/checkout/subscribe {plan_id}`
2. 后端创建 Stripe Checkout Session(`mode=subscription`,line_items=[plan.stripe_price_id])→ 返回 `{session_url, order_id}`
3. 前端跳转 Stripe 托管支付页
4. 支付完成 → Stripe `webhook` → `POST /api/billing/webhook` → 创建/激活 `Subscription`(status=active,period_end 从 Stripe)
5. 前端轮询 `GET /api/billing/checkout/{order_id}/status` 或跳转 `billing/success`

**积分充值**:`mode=payment`,amount = 积分包价格,webhook 到账后写 `credit_transactions`(+delta)。

**自动续费**:Stripe 订阅天然支持;`/api/billing/toggle-auto-renew` 调 Stripe API 改 `cancel_at_period_end`。

**Customer Portal**:`/api/billing/portal` 创建 Stripe Portal Session,用户自助管理卡片/发票/取消。

**Setup Intent**:保存支付方式用于无 checkout 的扣款(`/api/billing/setup-intents`)。

### 4.3 国内支付方案(支付宝/微信)

> **已实现(微信支付 Native)**: `services/billing_provider.py::WeChatBillingProvider`,`BILLING_PROVIDER=wechat` 启用;下单走 V3 Native 返回 `code_url`,前端扫码弹窗 + `/subscription/checkout/{order_id}/status` 轮询;回调 `/api/subscription/billing/webhook/wechat` 用 APIv3 密钥 AEAD 解密验真,并回查订单接口确认终态。微信仅支持 CNY,USD 定价按 `BILLING_USD_CNY_RATE`(默认 7.2)换算下单与展示。续费仍为"到期手动续"(无委托代扣)。

Stripe 无国内可用,国内需自建订阅原语:
- **订阅 = 首次支付 + 记录 `period_end` + 定时任务(cron)到期前发起续费扣款**。
  - 复用现有 `services/trigger_daemon.py`(Aware Engine 调度器)做续费提醒 + 自动扣款。
  - 或用支付宝"周期扣款"/微信"委托代扣"(需签约,门槛高,企业资质)。
- **简化路径**:国内先做"按次购买/积分包"模式,订阅用管理员手动续期 + 到期提醒(webhook/邮件)。
- 推荐:国内租户走"积分制为主、订阅为辅",海外租户走"Stripe 订阅"——与云端订阅+积分双轨一致。

### 4.4 webhook 安全与幂等

- **签名验证**:Stripe 用 `Stripe-Signature` header + endpoint secret;国内用平台公钥验签。
- **幂等**:用支付平台 event id 去重,新增 `webhook_events` 表(`event_id` unique, `processed_at`)。
- **关键 event(Stripe)**:`checkout.session.completed`、`customer.subscription.updated`、`customer.subscription.deleted`、`invoice.paid`、`invoice.payment_failed`。
- **顺序不保证**:webhook 可能乱序/重复——以**调用支付平台 API 查最新状态**为准,非依赖事件顺序。

### 4.5 数据模型补充(支付相关)

- `billing_profiles.stripe_customer_id`:per-tenant Stripe 客户。
- 新增 `webhook_events(id, provider, event_id unique, event_type, raw json, processed_at)`:幂等去重。

### 4.6 API(支付集成)

```
POST /api/billing/checkout/subscribe     {plan_id} → {session_url, order_id}
POST /api/billing/checkout/topup         {credit_pack_id} → {session_url, order_id}
GET  /api/billing/checkout/{order_id}/status → {status, subscription?}
POST /api/billing/webhook                Stripe/国内支付回调(无认证,验签)
POST /api/billing/portal                 → {portal_url}
POST /api/billing/setup-intents          保存卡 → {client_secret}
POST /api/billing/toggle-auto-renew      {auto_renew}
GET  /api/billing/config                 → {publishable_key, plans, credit_packs}(前端配置)
```

### 4.7 多租户与风险

- Stripe Customer per tenant(`billing_profiles.stripe_customer_id`)。
- 平台管理员跨租户:**只读**查订阅状态,不代操作支付(支付操作必须租户自己走 checkout)。
- 发票资料 per tenant。
- **风险/坑**:
  - webhook 乱序/重复 → 幂等 + 查最新状态。
  - 退款/争议 → 监听 `charge.refunded` / `charge.dispute.created`,同步状态。
  - 多货币 → 按 `country_region` 路由货币与渠道。
  - 测试 → Stripe test mode;国内支付沙箱。

### 4.8 资损与合规(已定)

| 项 | 决策 |
|---|---|
| 退款 | 立即终止订阅 + 降级 free;已消耗配额不退;未消耗附赠积分扣回 |
| 发票 | 手动导出开票清单,财务线下开具(初期);`billing_profile` 收集公司名/税号/邮箱/开票内容 |
| 对账 | 每日自动对账 job:下载支付平台对账单 + 比对 `payment_orders` + 差异告警 |
| 防欺诈 | 每租户/每信用卡仅一次试用(指纹去重);退款扣回兑换码积分;Stripe Radar(海外) |
| 多货币 | 按 `tenant.country_region` 路由,plan 多货币价格表(管理员配置) |

**退款流程**:
1. 用户申请 / 平台主动 / chargeback → 调支付平台退款 API(Stripe `/refunds` / 支付宝微信退款)
2. 订阅 status → `canceled`/`expired`(立即降级,按状态机)
3. 扣回未消耗附赠积分(`credit_transactions` 反向流水,reason=`refund_clawback`)
4. 已消耗配额/Token 不退(已用服务)

**对账 job**(每日,复用 `trigger_daemon`):
- 下载支付平台当日对账单 → 与 `payment_orders` 比对(金额/状态/订单号)
- 差异:订单缺失(webhook 丢失)→ 补单;金额不符 → 告警人工核实
- 输出对账报告 + 异常告警

**发票(手动)**:
- `billing_profile` 收集开票信息(支付前或支付后申请)
- 管理后台导出开票清单(周期内已支付订单 + 开票信息)
- 财务线下开具电子发票,回填发票号到 `payment_orders`

---

## 5. 模块二:积分制(credits)

云端模式为**订阅 + 积分包双轨**:订阅按周期发放积分,积分包可额外购买;积分是核心消耗单位,用完提示购买订阅或积分包。

### 5.1 积分与订阅配额的关系(关键决策)

两种额度并行,需先定模式:

| 模式 | 说明 | 侵入性 |
|---|---|---|
| **A 配额+积分补充** | 订阅配额(quota_guard 次数)用完 → 扣积分继续 | 低,积分是叠加层 |
| **B 纯积分消耗** | 每次 LLM 调用按 token 扣积分,订阅每月发放积分 | 高,替换现有 quota_guard 计数 |

云端文案 "You've run out of credits. Purchase a subscription or a credit pack" 指向**积分为核心**。但本地现有 `quota_guard` 是次数配额,改纯积分侵入大。

**推荐模式 A**:保留订阅配额(阶段 0 权益驱动),积分作为补充额度与独立计费项(如高级模型、额外 agent)。不破坏现有 `quota_guard`,积分叠加。

### 5.2 积分消耗规则

- **消耗点**:LLM 调用(token 换算)、agent 创建、消息发送
- **计费单位**:1 积分 = X token,或按模型成本差异化(本地 `llm_call_ledger` 已记录每次调用的 token 与模型 → 可据此算积分)
- 复用 `DailyTokenUsage`(日 token)与 `llm_call_ledger`(调用账本)作为计费数据源

### 5.3 积分发放

- **订阅激活**:发放 `plan.credits_per_period`(写 `credit_transactions`,reason=subscribe)
- **周期续费**:每周期发放(Stripe `invoice.paid` webhook 触发;国内定时任务触发)
- **积分包购买**:`topup` checkout 支付成功 → 发放

### 5.4 积分消耗流程(与 quota_guard 协同,模式 A)

新增 `services/credit_service.py`:
```python
async def charge_credits(tenant_id, amount, ref_type, ref_id):
    async with db_transaction:
        bal = await get_balance_for_update(tenant_id)  # SELECT ... FOR UPDATE
        if bal.balance < amount:
            raise InsufficientCredits
        bal.balance -= amount
        await log_transaction(tenant_id, -amount, bal.balance, "consume", ref_type, ref_id)
```
协同:配额内 → 走 `quota_guard`(不扣积分);配额用完 → 检查积分,有则扣积分放行,无则 `QuotaExceeded`。或简化:积分只用于独立计费项(高级模型等),配额用完即停。

### 5.5 兑换码(对应云端 `vibe-company/redeem-code`)

- `redeem_codes` 表:`code` unique, `credits`, `expires_at`, `max_uses`, `used_count`, `created_by`
- 兑换:`POST /api/billing/redeem {code}` → 校验有效期/次数 → 发放积分
- 云端 `vibe-company` 是营销活动(邀请奖励等),本地做简化版兑换码即可

### 5.6 数据模型补充(地基已含 `credit_balances`/`credit_transactions`)

- `credit_packs`(积分包商品):`id, name, credits, price_cents, currency, is_active, stripe_price_id`
- `redeem_codes`(兑换码):`id, code unique, credits, expires_at, max_uses, used_count, created_by`

### 5.7 API

```
GET  /api/billing/credits/balance          → {balance, reserved}
GET  /api/billing/credit-transactions       → [流水,分页]
POST /api/billing/redeem                    {code} → {credits_added, balance}
GET  /api/billing/credit-packs              → [可购积分包]
POST /api/billing/checkout/topup            {credit_pack_id} → {session_url}  (模块一已含)
```

### 5.8 多租户与风险

- 积分 per tenant(`credit_balances.tenant_id`)
- **并发扣减**:`SELECT FOR UPDATE` 行锁,防超扣
- **预占**(`reserved`):长操作(异步任务)先预占再结算
- **审计**:`credit_transactions.balance_after` 快照对账
- 风险:并发未锁致负数 → 强制行锁;退款 → 反向流水

---

## 6. 模块三:批量管理 + 平台后台(跨租户 SaaS)

### 6.1 平台管理员视角

- `platform_admin` 可查看**所有租户**的订阅状态、用量、积分、支付记录
- 跨租户批量:分配套餐、充值积分、延期、导出
- 对应云端 `/biz/billing/subscriptions/bulk?tenant_ids=`

### 6.2 权限模型(复用现有)

- `User.role` 已有 `platform_admin`/`org_admin`/普通;`quota_guard` 已对 `platform_admin` 豁免配额
- 平台管理端点用 `get_current_platform_admin` 依赖(类比现有 `get_current_admin`)
- 前端入口:现有 `pages/AdminCompanies.tsx`(bundle 中已存在)可扩展,或新建 `pages/admin/BillingAdmin.tsx`

### 6.3 bulk API

```
GET  /api/billing/subscriptions/bulk?tenant_ids=   → [各租户订阅摘要]
POST /api/billing/subscriptions/bulk/assign         {tenant_ids, plan_id} → 批量分配(无支付,管理员操作)
POST /api/billing/credits/bulk/grant                {tenant_ids, credits, reason} → 批量充值积分
GET  /api/billing/admin/tenants                     → [租户列表 + 订阅 + 用量汇总]
GET  /api/billing/admin/revenue                     → 收入统计(按周期/套餐)
```

### 6.4 平台后台 UI

- 租户列表表格:租户名、当前套餐、状态、到期、用量、积分余额、累计消费
- 批量操作:勾选 → 分配套餐/充值积分/延期(二次确认)
- 收入看板:MRR、ARR、套餐分布、流失

### 6.5 多租户数据隔离

- 平台管理员查询**跨租户**,绕过 `tenant_id` 过滤(专用 service + `platform_admin` 鉴权)
- 普通租户查询只看自己(现有 `tenant_id` 过滤不变)
- 写操作走 `audit_logs`(现有表)留痕

### 6.6 风险

- 误操作:批量分配/充值需二次确认 + 审计日志
- 数据量:租户多时 bulk 查询分页 + 索引(`tenant_id`、`status`)
- 权限泄露:`platform_admin` 端点严格鉴权

### 6.7 后台形态(已定)

**决策:方案 A 内嵌 admin**(阶段 0/1),阶段后期可拆独立后台。

- 扩展现有 `AdminCompanies.tsx` + 新增 `BillingAdmin` 页面,加到 `/admin/*` 路由组
- `platform_admin` role + `get_current_platform_admin` 依赖鉴权
- 复用现有 auth/组件/构建,单一前端项目

**`BillingAdmin` 页面**(收拢充值/订阅监管):
- 收入看板:MRR/ARR、套餐分布、流失
- 租户订阅列表:套餐/状态/到期/降级 agents
- 账号池健康:各模型 credential 用量/状态/成功率(7.10)
- 对账报告:每日,差异告警(4.8)
- 退款处理 + 开票清单导出(4.8)
- 积分/兑换码管理 + 流水审计
- 跨租户批量操作:分配套餐/充值积分(6.3)

**阶段后期**:若运营与产品团队分离、或安全合规要求独立部署,再拆独立后台项目(`admin.clawith.ai`)。

---

## 7. 模块四:模型资源管理 + 账号池 + plan 路由

> **架构(2026-07):两层并存。**
> 1. **账号池层**(平台统一管 API key):`llm_credentials` 表(provider 维度——一个 key 服务该 provider 下多 modality,如 MiniMax code plan 账号可调语音/图片/视频,每个账号独立配额),平台 admin 在专门路由页 `/account` 统一录入/轮换/监控,所有租户共用;`load_balancer.pick_credential(provider, modality)` 按 capabilities+healthy+额度筛+加权轮询;`caller.py` 经 `resolve_model_key` 取池内 key,失败 `mark_degraded`+换 key,池尽走模型级 failover;用量/健康实时监控。
> 2. **订阅层**(plan 管模型访问):plan 的 `allowed_modalities`/`allowed_tiers` 经 entitlements 在 `quota_guard.check_model_entitlement` 强制——租户只能用 plan 允许的 modality/tier;stopped agent 拦截。
>
> 平台模型(`tenant_id=null`)走账号池;租户级模型(`tenant_id=X`,自带 key)走 `llm_models.api_key_encrypted` 单 key。模型级 failover(主→fallback)不变。

### 7.1 现状(本地)

- `llm_models` 表(`models/llm.py`):provider/model/api_key_encrypted/base_url/label/enabled/supports_vision/max_output_tokens...
- 14 种 provider(anthropic/openai/deepseek/qwen/zhipu/gemini/vllm/ollama/sglang...),protocol 分 anthropic/openai_compatible/gemini
- `services/llm/caller.py` + `utils.get_model_api_key(model)` 取**单 key**;有**模型级 failover**(主→fallback)
- 模型可平台级(tenant_id=null)或租户级
- 模型分类字段已加:`modality`/`modalities`/`tier`/`capabilities`(见 7.2)
- **账号池已恢复**:`llm_credentials` 表(provider 维度,见 7.3)+ `load_balancer.py`(见 7.5)+ 专门路由页 `/account`

### 7.2 模型分类(modality)

`llm_models` 加字段:
- `modality` str — text / vision / audio / music / video / multimodal
- `modalities` JSON? — `["text","vision"]`(多模态,补充 supports_vision)
- `tier` str — premium / standard / basic(plan 匹配用)
- `capabilities` JSON — `{stream, tool_call, max_input,...}`(灵活扩展)

`supports_vision` 保留(= `"vision" in modalities`,向后兼容)。`LlmTab` 按 modality 分组展示(文字/视觉/音乐/视频)。

### 7.3 订阅驱动模型访问(强制点)

**plan 绑定模型范围**(`plans` 表):
- `allowed_modalities` JSON — `["text","vision"]`(free 仅 text)
- `allowed_tiers` JSON — `["standard"]`(free 仅 standard)

**entitlements**(`services/entitlements.py`)返回 `allowed_modalities`/`allowed_tiers`(随订阅状态机生效,3.6)。

**强制**(`services/quota_guard.py::check_model_entitlement(agent_id, model)`):
- 取 `agent.tenant_id` → `get_tenant_entitlements` → 比对 `model.modality`/`model.tier`
- 不在 allowed 集 → 抛 `QuotaExceeded(quota_type="model_modality"|"model_tier")`
- 无 agent / 无 tenant / 无订阅 / allowed 集为空 → 不限制(向后兼容回退,3.3)

**收口点**(`services/llm/caller.py`,覆盖所有 LLM 入口):
- `call_llm`:catch → 返回 `⚠️` 串(覆盖 websocket 聊天 / feishu·gateway IM / A2A / trigger invoker)
- `_try_model`(`call_agent_llm_with_tools` 内):抛 `QuotaExceeded` → 外层 catch 终止(覆盖 scheduler / task_executor 后台);置于 inner `try` 之前,避免被 `except Exception` 吞成 `[Error]` 串误判 retryable
- 拒绝是终态,**不 failover**(`⚠️` 非 retryable,`call_llm_with_failover` 不会切 fallback)

### 7.4 边界与降级(已定)

| 场景 | 处理 |
|---|---|
| modality/tier 不匹配 | `check_model_entitlement` 抛 `QuotaExceeded` → caller 返回 `⚠️` 提示升级/联系管理员;**不自动降级** |
| 模型调用失败 | 无池内 key 概念;直接走 `caller.py` 模型级 failover(主→fallback),fallback 也失败则报错 |
| 模型下线 | `enabled=false` 时引用该模型的 agent 由管理员迁移到同 modality/tier 替代模型 |
| 模型成本差异 | `quota_guard` tier 加权(premium=5, standard=1, basic=1,3.7)已平衡 |

**调用层次**:
1. 订阅权益过滤:`check_model_entitlement`(modality/tier 是否在 plan 范围)→ 否则 `⚠️` 终止
2. 模型选择:agent 主模型
3. 模型级 failover:主失败(retryable)→ fallback 模型
4. 全失败 → 报错 + 降级提示

### 7.5 多租户

- **平台级模型**(`tenant_id=null`):所有租户共享,各租户受自己 plan 的 modality/tier 限制
- **租户级模型**(`tenant_id=X`):租户自带 key,仍受 plan 限制(后续若要作为高级特性豁免再议)

---

## 8. 分阶段实现路线

| 阶段 | 内容 | 工作量 | 价值 |
|---|---|---|---|
| **0 MVP(无支付)** | Plan+Subscription model、entitlements、quota_guard 改造、管理员手动分配套餐、前端 subscription tab | 1-2 周 | 80% 价值,向后兼容 |
| **1 积分制** | CreditBalance/Transaction、积分消耗、积分包/兑换码 | 1 周 | 按量计费 |
| **2 接支付** | Stripe(海外)/支付宝微信(国内)checkout+webhook+自动续费+发票 | 1-2 周 | 商业化 |
| **3 平台管理** | 跨租户批量订阅、admin 后台 | 1 周 | 多租户 SaaS |

## 9. 前端体验与故障恢复(已定)

**前端策略总则**:**功能流程对齐云端**(bundle 逆向的页面/路由/API),**视觉复用本地现有 `EnterpriseSettings` tab 模式与 UI 风格**(一致性,不完全复刻云端视觉——云端 SPA 需登录态,无法抓取渲染 UI)。

| 项 | 决策 |
|---|---|
| 配额超限 UX | 后端返回结构化错误码(`QUOTA_EXCEEDED` + `quota_type` + `action`),前端弹窗引导(次数超→升级套餐;积分完→买积分包;agent 数超→升级) |
| 支付中断恢复 | `billing/success` 页每 2s 轮询 order status,5 分钟超时取消,订单保留 24h;webhook 兜底 |
| 用量展示 | 按需刷新(进入 tab + 操作后)+ 轮询 30s |
| 降级提示 | 权益失效操作超限 → 提示"订阅已过期,升级恢复" + 跳订阅页;agent 停止 → 列表标"已停止(订阅降级)" |

**配额超限错误码**:
```json
{"error": "QUOTA_EXCEEDED", "quota_type": "tenant_llm", "action": "upgrade|buy_credits"}
```
前端按 `action` 引导:upgrade→套餐页;buy_credits→积分包页。

**支付轮询**:`billing/success` 页 `useQuery` refetchInterval=2s 轮询 `/api/billing/checkout/{order_id}/status`;5 分钟未 paid → 超时提示,订单保留 24h;webhook 激活后 status=paid → 跳成功。

**用量展示**:`SubscriptionTab` 进入时 + 操作后刷新,`useQuery` refetchInterval=30s;展示当前套餐、配额用量(已用/上限)、积分余额、订阅状态/到期。

---

## 10. 关键决策点

1. **支付渠道**:Stripe(海外)/ 支付宝微信(国内)/ 混合(按 country_region 路由,推荐)
2. **计费模式**:纯订阅套餐 / 订阅+积分包(云端模式,推荐)/ 纯积分按量
3. **对齐度**:完全对齐云端(含兑换码/活动/Stripe portal)/ 核心订阅+配额优先
4. **阶段 0 起步**:无支付、管理员分配套餐、权益驱动配额——20% 工作量拿 80% 价值,向后兼容

---

## 11. 第一阶段 MVP 产品验收标准

> **第一阶段 MVP 目标**:先把 SaaS 订阅、模型档位、额度、费用、用量的产品边界做正确,让用户买的是套餐和 Credits,选择的是 `Lite / Pro / Ultra` 档位;真实模型、provider、API key、费用规则、账号池、限流都由平台 SaaS 后台统一配置。
> **不再接受的旧形态**:租户企业设置里直接管理真实模型、provider、base_url、API key、模型池。

### 11.1 产品信息架构验收

第一阶段必须形成 4 个独立产品域,不能继续混在 `/enterprise#llm` 中:

| 产品域 | 入口 | 使用者 | 验收标准 |
|---|---|---|---|
| SaaS 后台配置 | `/admin/saas` 或等价平台后台入口 | `platform_admin` | 可配置套餐、额度包、计费规则、模型档位、模型路由、账号池、租户订阅、用量审计 |
| 企业订阅购买页 | `/enterprise#subscription` | `org_admin` | 对齐云端截图的套餐卡片、月付/年付切换、Boost 额度包、升级/当前套餐状态 |
| 套餐详情页 | `/subscription` 或 `/account/subscription` | `org_admin`/`member`(按权限) | 展示当前套餐、到期时间、Credits 用量、Seats 用量、消耗明细、订单历史、账单管理入口 |
| 运行时档位选择 | Chat/Agent composer | 普通用户 | 只展示 `Lite / Pro / Ultra`,不展示真实模型列表和 provider 配置 |

### 11.2 角色权限验收

| 角色 | 可以做 | 不可以做 |
|---|---|---|
| `platform_admin` | 管理 SaaS 套餐、价格、Credits、额度包、计费规则、模型档位、模型路由、账号池、限流、租户订阅、全局用量审计 | 不通过租户企业设置页伪装成租户配置模型 |
| `org_admin` | 查看/购买/升级本企业套餐,购买 Boost 额度包,查看本企业用量/订单,管理 seats/用户/邀请 | 不可配置真实模型、provider、API key、base_url、全局模型池 |
| `member` | 使用 Agent/Chat,选择允许的 `Lite / Pro / Ultra`,查看自己或被授权范围内的用量 | 不可购买套餐、管理账单、改模型路由、改账号池 |
| `system/agent` | 执行前做 entitlement/quota/credit 检查,执行后写 credit ledger | 不绕过后端直接相信前端传入的模型 id |

### 11.3 SaaS 后台配置验收

第一阶段必须有独立后台页面或平台后台 tab,至少包含以下配置面:

| 配置面 | MVP 字段 | 验收标准 |
|---|---|---|
| 套餐 Plans | code、name、价格、billing interval、Credits、Seats、features、allowed_tiers、allowed_modalities、是否发布、排序 | 管理员改配置后,企业订阅页展示同步变化;运行时权益检查使用同一份配置 |
| 额度包 Boost | Credits 数、价格、单位价格、适用套餐折扣、是否发布 | 企业订阅页能展示 10,000/50,000/200,000 Credits 等包;购买入口可先 mock 或 admin 分配 |
| 计费规则 | action、modality、tier、unit、credit_cost、是否启用 | `chat/image/video/tts/music/heartbeat` 等动作能落到统一 Credits 规则 |
| 模型档位 | `Lite`、`Pro`、`Ultra`、展示名、权重/成本倍率、允许套餐 | 前端选择档位,后端按档位做 entitlement 检查 |
| 模型路由 | tier、modality、provider、model、priority、fallback、enabled | 用户选择 `Ultra` 时由后端解析到真实模型;前端不可见真实模型 |
| 账号池 | provider、key/secret_ref、rate limit、健康状态、fallback 状态 | 全局账号池只在 SaaS 后台出现,不出现在企业设置页 |
| 租户订阅 | tenant、plan、status、period_start/end、credits、seats、manual adjustment | 平台管理员可给租户分配套餐/补额度/查看状态 |
| 用量审计 | tenant、user、agent、action、tier、credits、provider/model、时间 | 每次消耗可追溯,可按租户查询 |

### 11.4 企业订阅购买页验收

`/enterprise#subscription` 第一阶段按云端截图实现业务结构:

- 顶部企业设置 tab 中,`订阅` 应位于靠前位置;`模型池/llm` 不再作为租户可见 tab。
- 套餐卡片至少包含 Free、Starter、Pro、Scale 四档。
- 支持月付/年付切换,年付显示折扣。
- 每个套餐展示价格、Credits/月、Seats、核心权益、当前套餐/升级按钮。
- Pro 或配置中指定的套餐支持推荐标记。
- 页面下半部分展示 Boost 额度包。
- 当前套餐按钮不可重复购买;不可用套餐给出明确原因。
- 页面不出现真实模型名、API key、provider、base_url、模型 CRUD。

### 11.5 套餐详情页验收

用户菜单应提供“套餐详情”入口,不能把普通用户带到账号池管理。

MVP 页面至少包含:

- 当前套餐名称、计费周期、状态、到期时间。
- Credits 用量:已用/总额、进度条、剩余额度。
- Seats 用量:已用/总数。
- 操作入口:`账单管理`、`管理订阅`。未接支付时可进入占位状态,但必须说明当前由平台管理员分配。
- `消耗明细` tab:时间、消耗方、发起方、动作、积分。
- `订单历史` tab:订单号、类型、金额、Credits、状态、时间。

> 用户具体用量子界面的细节截图暂缺。第一阶段先按云端截图的摘要卡 + ledger table 完成;后续拿到截图后再补充字段和交互。

### 11.6 运行时档位选择验收

聊天/Agent composer 的模型选择必须改为用户可理解的档位:

| 前端显示 | 后端含义 | 验收标准 |
|---|---|---|
| `Lite` | 低成本/基础模型路由 | Free/低阶套餐默认可用 |
| `Pro` | 标准生产模型路由 | 按套餐权益开放 |
| `Ultra` | 高质量/高成本模型路由 | 仅允许有权益的套餐使用 |

验收要求:

- 前端只传 `tier`/`modality` 或等价业务参数,不传可被信任的真实 provider key。
- 后端用 SaaS 后台配置解析 `tier + modality -> provider/model/credential`。
- 套餐不允许的 tier 必须被后端拒绝,不能只靠前端隐藏。
- 拒绝结果返回结构化错误,前端引导升级或购买额度。
- 模型调用成功或失败都要保留可审计记录;成功消耗必须写入 Credits ledger。

### 11.7 后端强制与账本验收

第一阶段必须保证“配置展示”和“运行时强制”用同一套数据:

1. 请求进入模型/工具执行前,后端检查 tenant subscription、allowed_tiers、allowed_modalities、credit balance、rate limit。
2. 后端解析 `Lite / Pro / Ultra` 到真实模型路由,并选择可用 credential。
3. 执行完成后写入 `credit_transactions` 或等价 ledger,至少包含:
   - `tenant_id`
   - `user_id`/发起方
   - `agent_id`/消耗方
   - `action`
   - `modality`
   - `tier`
   - `provider`
   - `model`
   - `delta`
   - `balance_after`
   - `created_at`
4. Credits 不足时返回可识别错误,前端能引导到套餐页或额度包。
5. 账号池限流/失败应触发 fallback 或明确错误分类,不能被计为用户权限问题。

### 11.8 旧模型池移除验收

第一阶段 UI 层必须完成移除,数据层采用迁移/归档,不做破坏性删除:

- 企业设置不再出现 `模型池` tab。
- `/enterprise#llm` 老 hash 要兼容:
  - `platform_admin` 可跳转到 SaaS 后台模型路由/账号池配置。
  - 非平台管理员跳转到 `/enterprise#subscription` 或显示无权限。
- 原 `LlmTab` 的模型 CRUD 能力迁移到 SaaS 后台。
- 原 `AccountManagement` 账号池管理不能再作为普通用户菜单入口。
- 旧 DB 模型记录先保留,通过 migration/seed 转换为 SaaS 后台的模型路由配置;确认无运行引用后再归档删除。

### 11.9 第一阶段不做范围

为避免 MVP 失焦,以下内容不作为第一阶段验收阻塞项:

- Stripe/支付宝/微信真实支付闭环。
- 自动开票、退款、争议处理、每日对账。
- 复杂优惠券、兑换码、营销活动。
- 多币种价格矩阵。
- 用户具体用量子界面的精细视觉还原。
- 真实模型成本按 token 精细核算;第一阶段可先按 action/tier 固定 Credits 规则。

### 11.10 第一阶段完成定义

第一阶段 MVP 只有同时满足以下条件才算完成:

1. 企业管理员能在 `/enterprise#subscription` 完整看到套餐与 Boost 额度包,且页面不暴露模型配置。
2. 平台管理员能在 SaaS 后台配置套餐、额度包、计费规则、模型档位、模型路由、账号池。
3. 普通用户能在聊天/Agent composer 选择 `Lite / Pro / Ultra`,真实模型由后端解析。
4. 后端对套餐、tier、modality、Credits、账号池限流做统一强制。
5. 每次 Credits 消耗都能在套餐详情页的消耗明细中查到。
6. 用户菜单“套餐详情”进入的是订阅用量页面,不是账号池管理。
7. `/enterprise#llm` 不再作为租户侧模型配置入口。
8. 至少完成以下验证:
   - 前端 build 通过。
   - 订阅页、套餐详情页、SaaS 后台核心 tab 的桌面浏览器检查通过。
   - 后端 entitlement/quota/credit ledger 的单元或集成测试通过。
   - 一条从选择 tier、执行调用、扣 Credits、查看明细的端到端 smoke 通过。

### 11.11 注册码注册门禁验收脚本

注册码属于 SaaS 平台后台的基础准入能力,第一阶段需要用独立 smoke 固化,避免只靠手工点页面确认。

命令:

```bash
scripts/registration-code-smoke.sh
```

默认验收 API 业务链路:

1. 创建/复用本地 smoke `platform_admin`。
2. 登录后台。
3. 生成平台级注册码。
4. 打开 `invitation_code_enabled`。
5. 验证无注册码注册被拒绝。
6. 验证无效注册码注册被拒绝。
7. 验证有效注册码注册成功。
8. 验证 `max_uses=1` 的注册码不能重复使用。
9. 验证 `used_count=1`。
10. 自动恢复原 `invitation_code_enabled` 状态并停用测试注册码。

可选 UI 验收:

```bash
scripts/registration-code-smoke.sh --ui
```

`--ui` 会额外用 Playwright 打开 `/login`,切换到注册表单,确认注册码/邀请码输入框可见。开发组并行改 UI 时,优先看脚本输出的 `stage`;如果 API 链路已通过而 `stage` 只落在 `ui_registration_code_field_visible`,应归类为前端可见性回归或进行中改版,不能误判后端注册门禁失败。

常用参数:

- `CLAWITH_SMOKE_API_BASE=http://localhost:3008/api` 覆盖 API 地址。
- `CLAWITH_SMOKE_FRONTEND_URL=http://localhost:3008` 覆盖前端地址。
- `CLAWITH_SMOKE_ADMIN_EMAIL` / `CLAWITH_SMOKE_ADMIN_PASSWORD` 覆盖 smoke 管理员。
- `--no-admin-bootstrap` 不写本地 DB,只使用已存在管理员账号。
- `--keep-code` 保留测试注册码。
- `--leave-gate-enabled` 不恢复注册门禁开关,仅用于人工调试。

验收结论只能按层级表述:

- `status=passed`:本地产品 smoke 通过。
- `stage=service_reachable`:本地服务未启动或 API 地址不对。
- `stage=admin_login`:管理员账号/权限/登录链路问题。
- `stage=registration_gate_enable` 或之后的注册阶段:注册码门禁业务链路问题。
- `stage=ui_registration_code_field_visible`:前端注册表单可见性问题,需要结合当前 UI 开发状态判断。
