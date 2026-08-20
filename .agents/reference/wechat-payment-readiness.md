# 微信支付生产就绪合同

本文只描述当前代码事实和上线门禁。它不代表已配置真实商户、不代表已创建真实订单，
也不代表生产已验证。

## 1. 产品状态

`GET /api/subscription/config` 返回不含密钥的支付状态：

| `status` | 产品语义 | 能否创建订单 | UI 行为 |
|---|---|---:|---|
| `manual` | 人工订单，由平台管理员线下处理 | 是 | 明示不会生成在线付款或微信二维码 |
| `ready` | 当前 Provider 的必要配置通过本地检查 | 是 | 微信模式才显示 Native 扫码支付 |
| `misconfigured` | 已选择 Provider，但配置不完整或格式错误 | 否 | 禁用购买并显示 `next_action` |
| `unsupported` | Provider 名称不受支持 | 否 | 禁用购买并要求平台管理员修正 |

`checkout_enabled` 是能否提交订单的主门；`native_payment_enabled` 是能否展示在线支付
流程的独立门。前端不得从 `provider=wechat` 自行推断通道已经可用。

人工订单与微信订单不是自动降级关系：

- `BILLING_PROVIDER=manual` 才创建人工 pending 订单；
- `BILLING_PROVIDER=wechat` 但 readiness 不通过时，API 在写 `payment_orders` 前返回
  `503 BILLING_PROVIDER_NOT_READY`；
- 微信 Provider 调用失败也不得静默改成人工订单；用户可重试原语义，平台可诊断配置。

## 2. 微信配置边界

微信 Native checkout 必须同时具备：

- 商户身份：`WECHAT_PAY_APPID`、`WECHAT_PAY_MCHID`；
- 请求签名：`WECHAT_PAY_SERIAL_NO` 与 RSA 商户私钥；
- 资源解密：32-byte `WECHAT_PAY_API_V3_KEY`；
- HTTP 验签：微信平台公钥或证书及其 serial / `PUB_KEY_ID_*`；
- 公网 HTTPS 支付/回调 URL；
- 回调重放时间窗，默认 300 秒。

私钥、公钥正文和 APIv3 key 不通过 `/subscription/config` 或 readiness CLI 输出。
`python -m app.scripts.check_billing_readiness` 只输出状态、缺失配置键名和安全问题码；
发布门需要在线支付时使用 `--require-native-ready`。

## 3. 下单与回调状态机

1. 公司计费管理员从市场页提交订阅或 Credits 加餐包。
2. API 先检查 readiness 和支付域名，再创建本地 order。
3. 微信 Provider 创建 Native order，`out_trade_no=PaymentOrder.id.hex`，并在 `attach`
   写入租户绑定；前端只渲染 Provider 返回的 `code_url`。
4. 回调必须包含 `Wechatpay-Signature`、`Wechatpay-Timestamp`、`Wechatpay-Nonce`、
   `Wechatpay-Serial`。服务端先验证 RSA SHA-256 签名、serial 和时间窗，再做 AES-GCM
   解密。
5. 服务端回查 Provider 订单；回调体只在回查失败且自身已验签/解密时作为恢复证据。
6. 入账前必须同时匹配 order ID、provider session、MCHID、APPID、`NATIVE`、CNY
   金额和 tenant attach。
7. `PaymentOrder` 行锁是资金幂等栅栏；`billing_webhook_events(provider,event_id)` 和
   order-scoped Credit ledger 唯一约束提供第二层 exactly-once 保护。
8. 非人工 Provider 订单不能由 SaaS 管理员手工 `mark-paid`。

## 4. 关闭、退款与恢复

- `time_expire` 只声明有效期，不作为本地已关单证据；对过期 pending 微信订单，
  reconciliation 先回查，再调用微信 close API，成功后才把本地状态改为 `canceled`。
- 丢失回调时，状态轮询和 reconciliation 复用同一 Provider 查询及订单绑定校验。
- Provider 返回 `REFUND` 时，订单改为 `refunded`；最新生效订阅被取消，order 对应的
  grant 只从尚未消费、且未被 reservation 占用的余额中扣回。
- `refund_clawback` 使用 order reference 幂等；重复退款事件不会重复扣 Credits。
- 已消费 Credits 不制造负余额，正在执行任务的 reservation 不被退款流程抢走。

## 5. 验证层级

本地可执行证据：

```bash
cd backend
PYTHONPATH=. .venv/bin/python -m app.scripts.check_billing_readiness
PYTHONPATH=. .venv/bin/python ../scripts/wechat-payment-postgres-smoke.py
```

第二条使用临时 RSA/AES 密钥、stubbed Provider 响应和真实 PostgreSQL 事务，覆盖并发
重复回调、伪造签名、金额/tenant/order 错配、过期关单和退款回收。它不会访问微信，
不会创建真实微信订单，也不会扣费。

以下仍是独立的外部门，不能由本地测试替代：

- 真实商户号、AppID、APIv3 key、商户证书和微信平台公钥已由授权人员配置；
- 支付域名、回调域名、证书、反向代理 header 保留和网络出口已验证；
- 经明确授权创建最小金额真实订单，验证扫码、回调、轮询、对账、退款和关单；
- 真实验证完成后，才能把 `provider_verified` 或 `production_verified` 标为通过。
