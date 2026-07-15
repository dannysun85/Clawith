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
3. 后端全量 pytest、前端测试与 `npm run build`、部署契约测试、Ruff 变更文件
   检查和 `git diff --check` 全部通过。
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

该脚本负责本地发布检查、不可变发布包、远程蓝绿部署、Nginx 校验、版本/commit
身份校验、单 worker 交接和失败回滚。不得使用 `ALLOW_DIRTY=1` 发布正式版本。

如获准执行真实账号 smoke，使用临时、最小权限的环境变量并设置
`RUN_REMOTE_SMOKE=1`；临时凭据文件必须由脚本清理，报告只记录通过/失败和 trace
ID，不记录凭据。

## 5. 发布后验收

- 公开 `/api/version` 的 version、commit 和 release ID 与候选一致。
- 后端、worker、前端健康；全局只有一个活动 worker；Nginx 配置有效。
- 用真实浏览器验证登录、Agent 聊天、模型档位保持、工具开关、文件/媒体加载、
  Credits 扣减与失败退款；`tests pass` 不能替代业务流证明。
- 检查生产问题监控、错误率、队列积压、Credits ledger drift 和媒体任务超时。
- Code 未获独立授权时，验证平台开关为 false、tenant 白名单为空、历史 Agent
  Code 分配已由迁移关闭。

## 6. 停止与回滚条件

出现版本身份不一致、迁移失败、多 worker、Credits 漂移、新增跨租户访问、密钥
泄露、Code 绕过授权、媒体资产不可访问或关键业务 smoke 失败时立即停止交接并
回滚。回滚不得自动恢复历史 Code 分配或自动化触发器。
