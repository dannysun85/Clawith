# 生产发布验收身份生命周期

本流程只服务于 `scripts/deploy-astra-production.sh` 的强制真实账号 smoke。它不授予
生产写权限，也不允许把客户账号、`admin@reeftotem.ai`、MFA 账号或普通平台管理员
改造成自动化账号。任何创建、密码轮换和撤权都属于生产安全写操作，必须先在当前
任务取得明确授权。

## 1. 身份边界

一次获批发布使用三类身份：

- Release QA 公司所有者：必须已存在，且只属于一个名称以 `Release QA ` 开头、slug
  以 `release-qa-` 开头的内部租户；角色为 `org_owner`，注册来源为
  `release_smoke`，不是平台管理员且未启用 MFA。
- Release QA 普通成员：邮箱必须以 `@release-smoke.invalid` 结尾，只能属于上述
  租户，角色固定为 `member`，注册来源固定为 `release_smoke`。
- 临时平台操作员：邮箱必须以 `@release-smoke.invalid` 结尾，不能加入任何公司，
  只允许一个 tenantless `platform_admin` 用户，注册来源固定为 `release_smoke`。

`backend/scripts/manage_production_smoke_principals.py` 对以上边界 fail-closed。它不会
创建租户、修改客户身份、关闭 MFA、复用已有多租户平台管理员，也不会创建 Agent、
私人助理或业务数据。Release QA 所有者的私人助理如缺失，必须在发布身份获批后通过
真实注册/初始化 API 或浏览器流程创建，不得直接插数据库伪造。

## 2. 凭据合同

发布脚本继续使用以下七个值，三组邮箱和三组密码必须互不相同：

```text
SMOKE_TENANT_EMAIL
SMOKE_TENANT_PASSWORD
SMOKE_TENANT_ID
SMOKE_PLATFORM_ADMIN_EMAIL
SMOKE_PLATFORM_ADMIN_PASSWORD
SMOKE_MEMBER_EMAIL
SMOKE_MEMBER_PASSWORD
```

密码必须为 20 到 4096 个字符，并同时包含大小写字母、数字和特殊字符。凭据应来自
已批准的密码管理器或 owner-only 临时文件，不得写入仓库、聊天、shell history、
发布证据或命令参数。部署脚本在本地进程启动时立即捕获并取消导出，随后用标准输入
传输为远端 `/dev/shm/astra-deploy-smoke` 下的 `0600` JSON；完成或失败都会清理。

## 3. 显式启用参数

普通发布默认 `PREPARE_REMOTE_SMOKE_PRINCIPALS=0`，不会改变任何身份。仅在取得
本次生产安全授权后，发布主责才可同时设置：

```text
PREPARE_REMOTE_SMOKE_PRINCIPALS=1
SMOKE_PRINCIPAL_CONFIRM_TENANT_ID=<与 SMOKE_TENANT_ID 完全相同的规范 UUID>
SMOKE_PRINCIPAL_PROVISION_OPERATION_ID=<本次准备操作 UUID>
SMOKE_PRINCIPAL_DEACTIVATE_OPERATION_ID=<本次撤权操作 UUID>
```

两个 operation UUID 必须不同且非零。对同一组凭据和同一次中断重试，复用同一对
UUID；凭据发生变化时必须申请并使用新的一对 UUID。`RUN_REMOTE_SMOKE=0` 时禁止
启用本流程，break-glass 不能顺带创建验收身份。

取得全部授权和值后，仍只运行正式入口：

```bash
bash scripts/deploy-astra-production.sh
```

不得手工执行 SQL、远端 `docker compose up`、迁移或 Nginx 切流，也不得单独运行
manager 来绕开部署序列。

## 4. 正式执行顺序

部署脚本在完成本地全量门、包身份、生产只读预检、备份、迁移、候选后端/worker/
frontend 健康与告警 canary 后，按以下顺序执行：

1. 在隔离候选后端内做 Release QA 边界 inventory。
2. 以 PostgreSQL advisory transaction lock 和 operation UUID 执行 exactly-once
   provision；轮换所有者密码，准备普通成员，临时启用 tenantless 平台操作员。
3. 再次 inventory，要求三类身份均满足预期且可登录。
4. 对候选槽运行真实订阅 API 和浏览器 smoke，生成不含凭据的合并证据。
5. 在任何公开切流之前清空临时平台操作员密码、关闭 password login、撤销平台
   标志、禁用 identity/user 并递增 `auth_version` 撤销 token。
6. 再次 inventory，要求所有者和普通成员仍可用、临时平台操作员不可登录。
7. 删除远端凭据文件；只有上述步骤全部通过才允许切公开流量。

任何中断和 rollback 都会在删除凭据前再次尝试第 5、6 步。撤权无法验证时发布失败，
继续保持或重新建立维护态，并拒绝恢复公开流量；回滚结果标记为需要人工关注，不得
宣称安全闭环。

## 5. 证据与验收

每次发布的 owner-only 证据位于：

```text
/opt/astra-poc/backups/<release-id>/smoke-principals.inventory-before.json
/opt/astra-poc/backups/<release-id>/smoke-principals.provision.json
/opt/astra-poc/backups/<release-id>/smoke-principals.inventory-ready.json
/opt/astra-poc/backups/<release-id>/smoke-principals.deactivate-*.json
/opt/astra-poc/backups/<release-id>/smoke-principals.inventory-deactivated-*.json
/opt/astra-poc/backups/<release-id>/smoke-principals-state
```

数据库 `audit_logs` 同时保存 privacy-safe receipt：operation UUID、版本、作用域和
结果布尔值，不保存邮箱、密码、hash 或 token。发布完成后还必须独立确认公开
version/commit/release id、平台操作员不可登录、Release QA 租户归属、真实产品流程、
Credits exactly-once、日志与监控；这些证据不能由本地测试替代。
