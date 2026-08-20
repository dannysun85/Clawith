# 生产切流操作规程

本规程把 2026-08-19/20 事故收成**失败即停**红线。完整发布门禁仍以
`.agents/workflows/deploy-production.md` 与 `scripts/deploy-astra-production.sh`
为准；本文件不重复那些步骤。

适用范围：`opc.reeftotem.ai`，应用目录 `/opt/astra-poc`。本规程不授予部署权限。

## 必须

- 同一时刻只有一名切流主责。其他代理只读，直到主责声明释放。
- 上传包、迁移、切 Nginx 之前：应用网络上回答 `postgres` 与 `redis` 的运行容器必须各恰好一个。
- 共享库/缓存属于 compose 项目 `astra-poc`（例如 `astra-poc-postgres-1`），不是槽位 `astra-poc-app-a` / `astra-poc-app-b`。
- 槽位 `compose up` 必须带 `--no-deps`，且不得启动 postgres/redis。
- DNS / `docker inspect` 解析为 0 个或 ≥2 个地址时立即停止；正式脚本会拒绝继续。
- 独立 code review 与 architecture review；存在 `REQUEST CHANGES` 或 `BLOCKED` 时禁止打标签或切流。
- 生产写操作必须在**当前任务**中取得明确授权。
- Smoke 使用已批准的内部 `SMOKE_TENANT_*` 租户。
- 模型或会话切换后，先重读现状再动手。

## 禁止

- 把合并/停止/启动数据库当作普通版本发布的步骤。数据面修复是单独的已授权变更。
- 在对方仍在要求确认时先改生产。
- 把未发布产品能力（CEO、creative v2、Code）混进稳定性热修，除非书面写明「本列车包含 X，开关保持关闭」。
- 脏工作区，或 HEAD 含未评审 CEO 能力时发版。
- 用平台管理员邮箱顶替 smoke 租户。
- 多代理同时写同一数据面；禁止靠心跳/围观/噪声「帮忙」。

## 1. 主责与模型切换

1. 口头或书面声明切流主责（人名 + 当前会话）。未声明则不得写生产。
2. 其他代理只做只读核对，不 ssh 写、不 docker start/stop、不迁移、不切 Nginx。
3. 模型或会话一切换，主责在任何写操作前必须重读：
   - 公开 `/api/version`（version / commit / release id）
   - `current` symlink 目标
   - 活动槽位（active-slot / active-state）
   - 应用网络上 `postgres` 与 `redis` 的容器与解析地址
   - `cutover-state`
4. 不得凭上一会话、上一模型或聊天摘要继续。现状与记忆冲突时以现状为准，并视为事故信号。

## 2. 确认协议

1. 其他团队要求「确认两次再执行」时：主责回复之前，他们不得改生产。
2. 若他们已经改了（停库、删容器、`pg_restore`、切 Nginx、改 symlink）：按事故处理，不把他们的方案当成计划，不补做他们要求的合并/停库。
3. 普通发版只切换应用槽位。共享数据面保持 `astra-poc` 项目中的那一个 postgres/redis。

## 3. 数据面唯一（硬门禁）

在包上传、迁移、维护窗、Nginx 切换之前，只读确认：

1. 生产应用网络上，带网络别名 `postgres` 的运行容器恰好 1 个；`redis` 同理。
2. 从 live backend 所在网络看，`postgres` / `redis` 各自只解析到 1 个 IP。
3. 该容器的 compose 项目必须是 `astra-poc`，service 必须是 `postgres` / `redis`。
4. 槽位项目里不得再跑一个会抢 DNS 名的 postgres/redis。`up` 漏掉 `--no-deps` 会因 `depends_on` 再拉起一个库——那就是 2026-08-19 的双写。
5. 结果为 0 或 2：失败即停。禁止靠「先停旧库、并到新槽位库」来消除歧义。
6. 数据面异常只能走独立授权的修复变更，不能夹在版本列车里做。

## 4. 列车范围与评审

1. 先写明本列车包含什么、不包含什么。
2. 稳定性热修默认只含运行时/渠道修复。CEO、creative v2、Code 默认不在列车内，开关保持关闭。普通发布仍会强制 `CODE_EXECUTION_ENABLED=false`。
3. 若同一 commit 含这些能力：必须有显式声明「本列车包含 X，flags 保持关闭」。没有声明则不得切流。
4. Alembic 必须只有一个 head；迁移 smoke 必须对着当前 head，不得仍期望旧 head。
5. 新增文件的 Ruff 违规（含 F841）阻断发布。
6. 独立 code + architecture review 未通过，或仍为 `REQUEST CHANGES`，禁止打发布标签。

## 5. 授权与 smoke

1. 工作区必须干净；未提交或未评审的 CEO/产品代码不得随列车上船。
2. 上传包、迁移、改 `.env`、切 Nginx、启停容器都需要当前任务的明确写授权。只读检查不构成写授权。
3. `SMOKE_TENANT_ID` 必须是已批准的内部验证租户。禁止拿平台管理员邮箱当租户用户，禁止误用客户租户。

## 6. 主责有序步骤

1. 重读 version / current / 槽位 / postgres·redis DNS / cutover-state。
2. 确认自己是唯一主责；其他代理只读。
3. 确认列车范围与 flags；确认独立评审无 `REQUEST CHANGES`。
4. 跑正式脚本的只读数据面预检（0 或 2 个地址会失败）。
5. 取得当前任务的生产写授权后，才执行 `bash scripts/deploy-astra-production.sh`。
6. 按 `deploy-production.md` 做发布后验收。
7. 声明释放主责之前，其他人不得接手写操作。
