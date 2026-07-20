# 生产部署固定流程

适用目标：`opc.reeftotem.ai`，应用目录 `/opt/astra-poc`。本流程是生产变更
门禁，不代表任何代理已获得部署权限。

## 1. 授权边界

- 日志、容器状态、数据库一致性和当前版本检查可以在用户明确要求检查生产
  环境时只读执行。
- 上传包、执行迁移、修改 `.env`、切换 Nginx、重启容器、回滚和启用产品
  能力都属于生产写操作，执行前必须在当前任务中取得明确授权。
- “发布新版本”和“启用 Code”是两个独立授权动作。普通版本部署会主动重写
  `CODE_EXECUTION_ENABLED=false`，并清空 tenant、tool、provider 和 endpoint
  allowlist；不会继承上一版本 `.env` 中的 Code 开启状态。
- 不在终端输出、报告、日志或审批消息中展示 API Key、JWT、数据库密码或完整
  工具配置。

## 2. 本地发布门禁

1. 确认当前分支、完整 commit、`backend/VERSION` 和发布说明一致，工作区干净。
2. Alembic 必须只有一个 head，并完成 PostgreSQL 升级、降级、再升级 smoke。
3. 锁定 Python `dev` extra 后运行后端全量 pytest，前端先 `npm ci` 再运行测试与
   `npm run build`；部署契约测试、Ruff Git
   基线差异检查（不得新增 violation）和 `git diff --check` 全部通过。历史存量
   Ruff 告警不作为本次伪失败，但新增文件或新增问题必须阻断发布。
4. 至少完成一次独立 code review 和 architecture review；存在 `REQUEST CHANGES`
   或 `BLOCKED` 时禁止打发布标签或部署。
5. 本地 Git 保存候选提交和证据；本项目不要求把代码上传 GitHub。

## 3. Code 独立授权清单（当前阻断）

v1.10.12 只发布 Code 的安全关闭态，不包含生产启用。当前禁止通过手工修改
`.env` 绕过普通部署的强制关闭。后续申请启用前，除明确记录以下全部内容外：

- 精确 tenant UUID；不接受 `*` 或全租户授权。
- 精确 Agent UUID 及业务负责人。
- 精确 Code tool 名、外部隔离 provider 和自定义 endpoint；不接受 `*`。
- 供应商依赖锁定、真实容器 import/contract smoke、资源上限和健康检查证据。
- 网络是否开启；默认关闭。需要联网时由平台管理员单独批准。
- 审批级别；首次灰度必须保持 `CODE_EXECUTION_REQUIRE_APPROVAL=true`。
- 灰度时间窗、最大并发、Credits 上限、监控人和回滚负责人。

还必须先完成并复审：审批人可看到实际 code/command 而非密文；外部执行具备
持久化 claimed/terminal 状态且崩溃歧义不会自动重放；provider 有硬超时、输出
上限和可验证 egress；Code 有按 tenant 的并发/额度、Credits 预留结算，以及独立
成功率、超时、拒绝、费用和告警指标。在这些条件闭环前，Code 激活状态为
`BLOCKED`。

生产禁止 `subprocess`、`docker` 本地 Code 后端，禁止
`SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING=true`。隔离失败必须
fail-closed。未来通过独立流程启用时，顺序固定为：精确 provider/endpoint/tool
白名单 → tenant 白名单 → Agent 工具分配 → 平台开关；关闭顺序相反，并优先关闭
平台开关。普通版本部署不是该独立启用流程。

生产 API/worker 容器不得挂载 `/var/run/docker.sock`，不得启用 `privileged`、
`SYS_ADMIN`、`seccomp=unconfined` 或 `apparmor=unconfined`。如未来恢复 OpenClaw
容器型 Agent，必须先设计独立、最小权限且有鉴权的生命周期 sidecar，不能把宿主
Docker 控制权重新交给通用 API/worker。

## 4. 生产预检与部署

在获得生产写授权后，先只读确认当前 release、活动蓝绿槽位、单 worker、数据库
备份和磁盘空间。随后从已审查且干净的本地提交运行：

```bash
bash scripts/deploy-astra-production.sh
```

该脚本负责不可跳过的本地全量检查、由已审查 commit 直接生成的不可变发布包、
包内 commit 与远端 SHA-256 双重校验、远程蓝绿部署、Nginx 校验、版本/commit
身份校验、单 worker 交接和失败回滚。脏工作区会直接失败，不存在
`ALLOW_DIRTY` 或 `RUN_LOCAL_CHECKS=0` 的正式发布旁路。

真实账号 smoke 默认必跑（`RUN_REMOTE_SMOKE=1`），使用临时、最小权限的环境变量。
`SMOKE_TENANT_ID` 必须指向明确批准的内部验证租户；API 与浏览器证据都必须证明
最终 token 仍属于该租户，禁止依赖多租户账号的默认选择，也禁止误用客户租户；
临时凭据文件必须由脚本清理，报告只记录通过/失败和 trace ID，不记录凭据。只有
紧急恢复才允许 `RUN_REMOTE_SMOKE=0`，且必须同时提供带审批号、一次性随机
`approval_nonce`、审批人、原因、目标版本、完整 commit、签发时间和未过期 UTC 时间的
`REMOTE_SMOKE_BREAK_GLASS_ARTIFACT`。字段不得为空或重复，审批有效期最长 4 小时；
审批文件还必须显式包含 `bypassed_gates=subscription_api,subscription_browser`，避免把
跳过真实 API 与跳过浏览器业务流混成一个没有范围的口头授权；
`approval_nonce` 只有在包含完整原始审批、文件 SHA-256、nonce SHA-256、版本和完整
commit 的 root-owned 证据完成 fsync 并原子发布后才算消费；发布前中断可安全重试，
发布后失败必须取得新审批。该证据不能以口头说明、空文件、重放旧审批或同版本其他
提交的审批替代。

普通发布只验证、不自动安装主机防火墙。首次发布本合同前，平台管理员必须另行
授权并在生产主机执行：

```bash
DOCKER_NETWORK_NAME="$(
  grep -E '^DOCKER_NETWORK=' /opt/astra-poc/current/.env | tail -1 | \
    cut -d= -f2- | sed -E 's/^"//; s/"$//'
)"
test -n "$DOCKER_NETWORK_NAME"
sudo bash scripts/manage-production-mcp-egress-guard.sh install \
  "$DOCKER_NETWORK_NAME" deploy/security-contracts/mcp-egress-v1
sudo bash scripts/manage-production-mcp-egress-guard.sh verify \
  "$DOCKER_NETWORK_NAME" deploy/security-contracts/mcp-egress-v1
```

该操作安装 root-owned `DOCKER-USER` 出网规则和 systemd watchdog：应用网络保留
内部 PostgreSQL/Redis/服务通信以及现有产品所需的公网端口，私网、loopback、
link-local、metadata、benchmark 和保留地址全部拒绝。MCP 的 HTTPS、DNS 解析和
connected-peer 校验仍由应用层独立执行；主机层不把整个共享应用网络误收窄为
53/443。安装时必须从当前生产 `.env` 读取显式 `DOCKER_NETWORK`，且目标网络必须
已有应用容器；禁止使用脚本默认值或对空网络安装。规则修复先插入临时 REJECT
栅栏，完整链和唯一首条 jump 校验成功后才
移除，禁止瞬时 fail-open。普通部署在备份、维护窗口和迁移之前核对合同 SHA-256、
network subnet、规则顺序和 watchdog marker；缺失或漂移时停止发布。安装或修改
该规则属于独立的生产主机安全变更，必须先验证现有公网集成与内部服务均正常。

## 5. 发布后验收

- 公开 `/api/version` 的 version、commit 和 release ID 与候选一致。
- 后端、worker、前端健康；全局只有一个活动 worker；Nginx 配置有效。
- 用真实浏览器验证登录、Agent 聊天、模型档位保持、工具开关、文件/媒体加载、
  Credits 扣减与失败退款；`tests pass` 不能替代业务流证明。
- 检查生产问题监控、错误率、队列积压、Credits ledger drift 和媒体任务超时。
- Code 未获独立授权时，验证平台开关为 false、tenant 白名单为空、历史 Agent
  Code 分配已由迁移关闭。
- 候选切流前的 `verify_channel_secrets` 门禁必须确认所有当前渠道密钥均已封装，
  并可由候选版本使用生产 `SECRET_KEY` 完整认证和读取；门禁只输出行数，禁止输出
  密钥或数据库原值。
- 迁移不能擦除历史 WAL、旧物理页、快照或备份中的既有明文。切流后必须按渠道
  逐一轮换 bot token、签名/加密密钥和 verification token，验证收发后吊销旧值，
  并执行已审批的 WAL/备份保留策略。轮换和保留窗口完成前，历史密钥风险不得标记
  为完全闭环。

## 6. 停止与回滚条件

出现版本身份不一致、迁移失败、多 worker、Credits 漂移、新增跨租户访问、密钥
泄露、Code 绕过授权、媒体资产不可访问或关键业务 smoke 失败时立即停止交接并
回滚。回滚不得自动恢复历史 Code 分配或自动化触发器。
