# Astra — SKILL.md（项目开发唯一事实来源）

本文件是 Astra 项目的**单一权威说明**。所有 AI 编码代理（Claude Code、Codex CLI、Cursor、Copilot 等）必须先读本文件。根目录的 `CLAUDE.md` 与 `AGENTS.md` 仅为跳转文件，不含任何项目知识——修改项目规则/架构/命令一律改这里。

> 个人全局规则（Karpathy 4 条 + Mnimiy 8 条）位于 `~/.claude/CLAUDE.md`，同样生效，本文件不重复。

---

## 0. 行为约定

- **完成提示音**：每个任务结束后在本机播放短促提示音，macOS 用 `afplay /System/Library/Sounds/Glass.aiff`（或其他短促系统音），让用户即使在其他窗口也能注意到。用户明确说"不要声音"时跳过。
- **不重复造文档**：本文件是唯一项目知识源。`CLAUDE.md` / `AGENTS.md` 仅做跳转，禁止向它们添加实质性内容。需要分专题细化时，在 `.agents/rules/` 或 `.agents/workflows/` 下新增文件，并在本文件末尾"专题文件索引"登记。

---

## 1. 项目概览

Astra 是一个开源**多智能体协作平台**——"数字员工"系统。基于上游 OpenClaw 开源单 Agent runtime 构建多租户团队协作层（类比 Kubernetes 与 OpenShift 的关系），后续将深度整合本地 OpenClaw 与 Hermes 服务能力。AI Agent 具备：

- **持久身份**：`soul.md`（人设）+ `memory.md`（长期记忆）
- **自主感知**：cron / interval / webhook / on_message 触发器（Aware Engine）
- **互相通信**：A2A（Agent-to-Agent）协议
- **全渠道接入**：飞书、钉钉、企业微信、Slack、Discord、Microsoft Teams、Jira/Confluence

---

## 2. 环境与命令（必须严格遵守）

### 后端（Python / FastAPI）—— Python 3.12 硬性规则

> 防止污染 anaconda base、防止 Python 版本漂移，这几条是用血的教训换来的。

- 后端虚拟环境固定为 `backend/.venv`（Python 3.12，uv 管理）。
- `backend/.python-version` = `3.12`，`backend/Dockerfile` 用 `python:3.12-slim`，`pyproject.toml` 中 `requires-python = ">=3.12"`、Ruff `target-version = "py312"`——本地 dev 与部署统一 3.12。
- **严禁直接使用系统 / anaconda python**。`uv pip install` 默认可能落到活动解释器，安装类命令必须显式 `--python .venv/bin/python`，或先 `source .venv/bin/activate`。
- 跑命令前先 `cd backend`。用 `.venv/bin/<cmd>` 前缀最稳（无需激活）。

```bash
cd backend

# 首次创建 venv（已存在则跳过）
uv venv --python 3.12 .venv

# 安装依赖（必须显式指定 .venv）
uv pip install -e ".[dev]" --python .venv/bin/python

# 跑命令：用 .venv/bin 前缀
.venv/bin/pytest
.venv/bin/pytest tests/test_auth.py -v
.venv/bin/ruff check .
.venv/bin/ruff format .

# 或激活后直接用
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
deactivate

# 数据库迁移
.venv/bin/alembic upgrade head
.venv/bin/alembic revision --autogenerate -m "description"
```

### 前端（React / TypeScript / Vite）

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
npm run build        # 类型检查 + 构建
npm run preview      # 预览生产构建
```

### 全栈（Docker Compose）

```bash
bash setup.sh        # 一键初始化（创建 .env、PostgreSQL、安装依赖）
bash restart.sh      # 启动所有服务 → http://localhost:3008
```

开发服务器部署（192.168.106.163，端口 3009）流程见 `.agents/workflows/deploy-dev.md`（待补）。

---

## 3. 仓库结构

### Monorepo 布局

```
Clawith/
├── backend/          # Python 3.12 FastAPI 应用
├── frontend/         # React 19 + TypeScript + Vite 应用
├── helm/             # Kubernetes Helm charts
├── scripts/          # 运维/构建脚本
├── deploy/           # 部署相关配置
├── docs/             # 文档
├── .agents/          # 本项目 AI 代理规则与工作流（本文件配套）
├── SKILL.md          # ← 你正在读的文件，唯一事实来源
├── CLAUDE.md / AGENTS.md  # 跳转文件，不含项目知识
├── docker-compose.yml
├── setup.sh / restart.sh
└── agent_template/   # Agent 工作区模板（soul.md / memory.md 等）
```

### 后端结构（`backend/app/`）

| 目录 | 用途 |
|------|------|
| `api/` | FastAPI 路由（按领域拆分） |
| `services/` | 业务逻辑（含 `llm/` 子目录：LLM 抽象层） |
| `models/` | SQLAlchemy 2.0 async ORM 实体 |
| `schemas/` | Pydantic 请求/响应模型 |
| `dao/` | 数据访问对象（ContextVar 事务层） |
| `core/` | 认证、事件、中间件、日志 |
| `alembic/` | 数据库迁移 |

**关键文件**：

- `api/websocket.py` — 核心 LLM 工具调用循环（最多 50 轮：LLM → Tool → 上下文重组 → 重复），LLM 流式输出
- `api/gateway.py` — OpenClaw 边缘节点协议（poll/report/send，用于本地 Agent）
- `services/agent_tools.py` — 所有文件类工具（`read_file`、`write_file`、`send_message_to_agent` 等）
- `services/agent_context.py` — 从 `soul.md`、系统提示、`memory.md` 组装 LLM 上下文
- `services/trigger_daemon.py` — Aware Engine 后台调度（cron/interval/poll/on_message 触发器）

### 前端结构（`frontend/src/`）

| 目录 | 用途 |
|------|------|
| `pages/` | 页面组件 |
| `components/` | 可复用 UI 组件 |
| `stores/` | Zustand 全局状态（auth、permissions、i18n） |
| `services/` | Axios API 客户端 |
| `hooks/` | 自定义 React Hooks |
| `i18n/` | 国际化资源（en.json / zh.json） |
| `types/` | TypeScript 类型定义 |
| `constants/` | 常量 |
| `utils/` | 工具函数 |
| `styles/` | 样式 |

**关键文件**：

- `pages/AgentDetailPage.tsx` 及子目录 — Agent 聊天 UI、设置、触发器、关系
- `pages/EnterpriseSettings.tsx` 及子目录 — 企业配置、渠道、认证提供商
- `App.tsx` — 主路由（含受保护路由）

---

## 4. 核心数据模型

- **Agent** — 数字员工实体（原生或 OpenClaw 边缘节点）
- **Participant** — 多方通信路由锚点（决定聊天气泡左右渲染）
- **ChatSession / ChatMessage** — 完整审计轨迹，包含 tool_call 快照
- **AgentTrigger** — Aware Engine 调度（cron、interval、poll、webhook、on_message）
- **AgentAgentRelationship** — A2A 严格访问控制（Agent 之间必须有显式关系才能通信）
- **Tenant / OrgDepartment / OrgMember** — 多租户隔离（所有实体带 `tenant_id`）

### 多租户模式

所有数据库实体包含 `tenant_id`，所有查询必须按 tenant 过滤。`OrgMember` 表把外部渠道用户（飞书/钉钉/企微）映射到内部用户。

### WebSocket 工具调用循环

`api/websocket.py` 中核心 LLM 执行最多 50 轮：

- 每轮：调用 LLM → 解析 tool calls → 执行工具 → 重组上下文 → 重复
- 回合数到 80% 时发出资源警告
- 高风险工具（`write_file`、`delete_file`）有硬性参数校验

### Agent 工作区

每个 Agent 在 `agent_template/` 下有私有文件工作区，其中 `soul.md`（人设）和 `memory.md`（长期记忆）通过 `services/agent_context.py` 注入到每次 LLM 上下文。

---

## 5. 技术栈

- **后端**：Python 3.12、FastAPI、SQLAlchemy 2.0（async）、PostgreSQL 15+ / SQLite（dev）、Redis 7+
- **前端**：React 19、TypeScript、Vite 6、Zustand 5、TanStack Query 5、React Router 7、i18next
- **LLM**：`services/llm/` 统一抽象，支持 OpenAI、Anthropic Claude、DeepSeek 等
- **集成**：飞书/Lark、钉钉、企业微信、Slack、Discord、Jira/Confluence、Microsoft Teams
- **Lint**：Ruff（line-length 120，target py312），前端 TypeScript strict mode
- **测试**：pytest + pytest-asyncio（`asyncio_mode = "auto"`）

---

## 6. 代码规范

- **Python 导入**：尽量放在文件头部。除非为避免循环依赖，否则禁止在函数/方法内部做 inline import。
- **惯例高于品味**：遵循现有风格，不顺手重构。REST 项目里不突然引入 GraphQL，类组件项目里不突然引入 Hooks。
- **简洁至上**：不为假想未来做过度抽象，不写投机性功能。
- **大声报错**：跳过即报错，不确定即声明，部分失败必须暴露。

---

## 7. 专题文件索引

随项目演进，在 `.agents/` 下按主题拆分细化文件。新建后在此登记，避免散落漂移。
开始任务前必须读取并遵守所有与任务范围相关的已登记专题文件；它们是本文件的强制扩展，不是可选背景资料。

**规则（`.agents/rules/`）——行为指令"必须/禁止"**

- `.agents/rules/capability-and-agent-governance.md` — 新能力、Skill、Tool、Provider 与 Agent 员工的强制边界和上线门禁

**工作流（`.agents/workflows/`）——步骤化操作指南**

- `.agents/workflows/deploy-production.md` — 生产部署固定流程（`opc.reeftotem.ai` / `/opt/astra-poc`）
- `.agents/workflows/add-product-capability.md` — 新增或升级产品能力、Skill、Tool、Provider 与 Agent 员工的统一流程
- `.agents/workflows/creative-deliverables-implementation-rollout.md` — 图片、视频、PPT 的完整落地、兼容迁移、灰度、验收和回滚方案

**参考（`.agents/reference/`）——纯事实性知识，按需拆分自本文件**

- `.agents/reference/creative-deliverables-capability.md` — 当前图片、视频、PPT 实现基线、质量方案、调用链与降级矩阵
- `.agents/reference/agency-agents-zh-provenance.md` — 首批外部 Agent 角色的固定版本、筛选映射、MIT 来源和 Astra 重写边界

> 规则：本文件总长度控制在 250 行以内。超过时优先把最详细的部分拆到 `.agents/reference/`，并在上面登记链接。
