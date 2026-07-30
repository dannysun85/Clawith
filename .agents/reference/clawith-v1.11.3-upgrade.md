# Clawith v1.11.3 → Astra v1.11.9 语义升级记录

## 1. 目标和边界

- 上游基准：官方 Clawith `v1.11.3`，commit
  `b20c708fe2e938f76cb4e7f38c20c9136e29707f`。
- Astra 起点：本地定制线 `v1.11.8`，升级后版本为 `v1.11.9`。
- 目标：吸收 v1.11.2–v1.11.3 的 Runtime、Group、错误协议、模型路由、逻辑删除和迁移修复，
  同时保持 Astra 图片、视频、PPT、SaaS 路由、Credits、审批、生产发布和历史数据合同。
- 本次只完成代码集成和本地验证，不代表已经部署或生产验证；生产发布仍必须单独授权并执行
  `.agents/workflows/deploy-production.md`。

## 2. 已吸收的上游能力

- Durable Runtime：命令恢复、checkpoint side effects、group planning、group `@` 状态、
  workspace typed tools、run compaction 和错误传播。
- 定时任务：daemon 只负责发现到期 occurrence，并以稳定 occurrence identity 注册到 Durable
  Runtime；保留 `USER_SCHEDULE_EXECUTION_ENABLED` operator kill switch。
- 聊天与模型：多模态消息转换、可用模型候选解析、逻辑删除后的 cache invalidation、SaaS
  tier/modality 路由和 compound cursor 历史分页。
- Group：创建时成员选择、共享 workspace 文件格式、公告缓存与 group workspace 工具。
- 资源和安全：Agent/LLM model 逻辑删除、L3 删除审批恢复原 Run、统一 HTTP/WebSocket
  可追踪错误合同、子进程取消回收和 workspace 参数前置校验。
- 文档：PPTX 分页读取不再对 `python-pptx` slide collection 使用不支持的切片操作。
- 数据库：合并上游与 Astra 两条 Alembic lineage，发布 head 为
  `merge_v1113_astra_heads`。

## 3. Astra 保留和适配项

- `finish` 继续作为受治理的控制工具，不按上游方案降级为无协议的自然结束。
- Agent 删除采用逻辑删除并保留历史，同时继续阻止存在 Credits、媒体任务、审批或抖音绑定的
  不安全删除。
- L3 Runtime 审批详情继续使用 Astra 的签名、加密和 fingerprint 合同；批准后恢复原始 Run，
  非 Runtime 审批继续走既有安全 worker。
- 图片、视频、PPT 继续使用 Astra 的 Deliverable、Artifact、质量检查、Credits 和 Provider
  failover；上游多模态能力只作为输入与 Runtime 基础设施增强。
- trace ID 始终归一化为内部 12 位十六进制值，不回显客户端提供的任意相关标识。
- Agent schedule 保留 fail-closed operator gate，但启用后统一进入 Durable Runtime，不再由
  scheduler 直接执行独立 LLM/tool loop。

## 4. 明确拒绝的上游内容

- 不采用上游 `.github/drone.yml` 和 `docker-compose.cd.yml` 中绑定特定服务器用户、端口和目录的
 自动部署方案；Astra 生产发布流程保持独立，且本次未触发部署。
- 不删除 Astra 项目文档、`.coaligneignore`、release workflow 或自有生产门禁。
- 不把官方版本号 `1.11.3` 覆盖到 Astra 已向前演进的版本线；产品版本递增为 `1.11.9`，
  上游基准单独记录。

## 5. 验证门禁

合并候选必须同时满足：

1. Alembic 只有一个 head，且同时包含上游和 Astra lineage。
2. 后端全量 pytest、Ruff undefined-name 检查通过。
3. 前端 Node contract tests、Vitest、TypeScript 和 Vite production build 通过。
4. 逻辑删除、L3 审批恢复、严格 Finish、SaaS 模型路由、schedule Runtime、HTTP/WebSocket
   error contract、PPTX 读取和 media/deliverable 回归通过。
5. 本地真实浏览器验证登录、Agent 聊天和可见升级入口；真实 Provider 付费调用只有获得单独成本
   授权后才执行。
6. 生产部署、生产数据迁移和生产业务流验证必须作为独立状态报告，不能由本地通过替代。
