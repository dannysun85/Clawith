# 图片、视频与 PPT 能力基线和目标方案

## 文档性质

本文记录 2026-07-26 仓库实现基线和已决定的目标方案。它不表示目标能力已经上线；完成状态必须按 `.agents/rules/capability-and-agent-governance.md` 分级报告。

底座决策：Astra v1.11.9 已完成对上游 Clawith v1.11.3 的语义升级；图片、视频和 PPT 继续使用
Astra 自有 Deliverable、Credits、Approval、Provider 路由和质量门禁，不以覆盖式上游合并替代这些业务合同。

## 一、已核验的当前实现

### 共用产品入口

- `backend/app/services/deliverable_workflows.py` 已定义 `builtin.presentation.v1`、`builtin.poster.v1`、`builtin.video.v1`。
- 图片、视频、语音和音乐的 durable Tool 回执会在历史会话重新载入时恢复右侧 Workspace
  预览。恢复路径必须来自成功媒体 Tool 回执；当最终助手回执也包含路径时，两者必须匹配，
  不能仅凭助手文本创建预览。
- PPT、海报和视频的 `launch_policy` 均为 `agent_runtime`。三者都必须以最终 Artifact
  校验成功为生成闭环，以质量评审和创建者批准为交付闭环；不能把 storyboard、Provider
  拒绝、无产物的 Runtime 结束或仅生成候选文件视为已交付。
- 用户合同不包含 provider/model；运行时根据 tenant、tier、能力和健康状态路由。
- 请求、运行、批准、Credits 和 Workspace Artifact 应继续作为 durable truth。

### 图片

- 当前已核验的生产媒体执行以 MiniMax `image-01` 为主；本地统一路由已接入 Agent Plan 与
  MiniMax。两者都复用平台凭据池、entitlement、Credits、Agent Tool 开关、资产保存和品牌安全处理，
  但本地接入不能替代生产 Provider 配置验证。
- 当前主要短板不是单一因素：
  - 编排层仍接近一次 prompt、单候选、首个结果；
  - 缺少系统化 prompt compiler、候选比较、质量评分、选择回执和基于失败项的修订；
  - `image-01` 在多参考、复杂商品编辑、成套一致性等任务上也存在模型能力上限。
- `brand-safe-media` 已正确规定：精确文案使用 deterministic overlay，真实商品/logo 使用冻结资产层；静态 packshot 不得冒充商品本体参与运动。

结论：先量化 `MiniMax optimized - current`，再用同题 A/B 判断火山候选 Provider 的真实增量；不能只看文档或主观印象直接替换。

### 视频

- 当前已有 MiniMax Hailuo 文生视频/图生视频相关 Tool、异步任务检查、Credits 和文件验证路径。
- 当前交付物工作流已开放正式 launch，但只有 Provider 接受、媒体任务完成且最终 MP4 Artifact
  通过校验时才算生成成功；缺失 MP4 必须 fail closed。
- 主要缺口是 storyboard compiler、多参考/关键帧一致性、逐镜头状态、质量评分、剪辑包装和镜头级重做，不只是替换模型。

### 语音和音乐

- 语音与音乐沿用同一 durable media task、Credits、Provider failover、Workspace Artifact
  和浏览器播放合同。
- `commercial-voiceover` 是 provider-neutral 商用配音 Skill：约束精确脚本、发音、声音身份、
  持久化音频、真人听审和视频混音。它不授予 Tool、Provider、Credits 或审批权限。
- `generate_speech_minimax` 保留兼容期内部名称，但 Tool 对 Agent 和用户展示为
  `Generate Speech`。未指定 `voice_id` 时按 Agent Plan → MiniMax 自动路由；显式声音 ID
  属于 Provider 命名空间，只允许在对应 Provider 内执行，禁止 fallback 时静默换声。
- 音乐 Tool 可声明 5–180 秒的精确 `duration_seconds`。Provider 返回更长的完整曲目时，
  在 durable storage 和 Credits settlement 前执行确定性裁切并重新校验；Provider 输出
  短于请求时 fail closed，不得把实际长音频或短音频声称为请求时长。

### PPT

- 已有 `convert_html_to_pptx`、`convert_html_to_pdf` 和 `builtin.presentation.v1`；正式合同要求同时生成结构有效的 PPTX 和匹配 PDF。
- 当前已生成 server-owned `PresentationBrief`、`DeckOutline`、adaptive-v1 `SlideSpec`、
  主题/版式下限、页数/素材/重复版式/溢出等结构门禁，并同时交付 PPTX/PDF。事实引用、字体替换、
  PPTX/PDF 像素级一致性、按页局部修订和真实多人视觉评审仍未形成完整商用闭环。
- 图片/视频 Provider 只应提供 PPT 中的装饰或场景视觉，不负责事实正确性、叙事结构、图表数据和可编辑性。

## 二、谁可以调用

### 用户层

tenant 内有权使用对应 Agent/Group 的成员都可以提出图片、视频或 PPT 任务。用户不直接调用 Provider Tool，也不选择模型。

### Agent/Expert 层

- 一次性任务由具备对应 Skill 的 task-scoped expert 或现有 Agent 执行。
- 内容创作、抖音运营、增长等持久员工可以通过 AgentTemplate 获得 `brand-safe-media` 及最小媒体 Tool grant。
- 只有需要长期记忆、触发器、渠道身份和持续责任时才增加持久 Agent 员工。

### Runtime 层

真正执行必须同时通过：

`tenant → Agent active/relationship → Skill resolution → Tool enabled/visible/granted → entitlement/tier → provider capability/credential health → Credits → autonomy/approval → durable state/idempotency`

当前代码事实：

- 图片、语音、视频属于 product-wide Agent capabilities，但仍受 entitlement、tier、Credits、媒体合同和 Agent 显式禁用控制。
- 音乐、安装 Skill、导入 MCP、发布、代码执行等能力需要显式 grant。
- Tool 的 builtin/admin/tenant/agent ownership 和 Agent assignment 决定可见性。
- global/tenant Skill 可以通过默认、显式选择或 AgentTemplate folder 解析，tenant override 优先。
- AgentTemplate 的 `default_skills` 和 `default_tools` 分开调和；Skill 指令不会授予 Tool。

## 三、目标质量流水线

### 图片/海报

1. `CreativeBrief`：用途、渠道、受众、比例、风格、精确文案、品牌资产、允许重绘范围、禁止项。
2. `ReferencePack`：商品、人物、风格、Logo 及 hash；区分 exact asset 与 creative reference。
3. 选择合同：brand-safe、product-in-motion 或 creative。
4. `PromptCompiler`：把 brief 编译为 Provider-specific prompt，不直接透传用户一句话。
5. `CandidatePolicy`：Lite 1、Pro 2、Ultra 3 个候选；数量最终以成本实验校准。
6. 生成无文字主体/背景。
7. deterministic composition：精确 copy、logo、商品层、safe area、裁切和导出。
8. 自动 QA：可解码、尺寸/比例、OCR、主体相似度、伪影、安全、文案完整性。
9. `SelectionReceipt`：保存候选、评分、选择理由和成本。
10. composition/final 批准；修改只重做失败层或候选。

图片“更好”的衡量不是只看美观，还包括 brief 遵循、主体/品牌一致、文案正确、构图适配渠道、首轮可用率和单位可用产物成本。

### 视频

1. `VideoBrief`：渠道、受众、比例、总时长、故事、商品/人物约束、字幕、声音和 CTA。
2. 先生成 script、storyboard 和逐镜头 `ShotSpec`，在付费生成前批准。
3. 建立 reference/keyframe pack；品牌精确任务优先批准的首帧/尾帧/多视图资产。
4. 每个镜头独立提交 durable async job，保存 provider task/receipt。
5. 自动 QA：文件可解码、时长、分辨率、黑帧/坏帧、主体一致性、标签可读性和安全。
6. 只重做失败镜头；通过镜头不重复付费。
7. 合成、转场、字幕、配音/音乐、响度、封面和 CTA 是独立确定性阶段。
8. 输出 MP4、storyboard、字幕/脚本和 source manifest；final 批准后归档。

高成本视频默认单候选；只有未达到阈值或用户明确要求时才局部重做。不能用多 Provider 并发“碰碰运气”替代质量编排。

### PPT

1. `PresentationBrief`：目标、受众、场景、页数、语言、风格、必须覆盖内容、品牌主题、可编辑性。
2. Source inventory：解析上传文件和数据；每个事实、引文、图表保存 source reference，缺失证据的内容标为假设。
3. `DeckOutline`：先确定一句话主张、故事线和页级目的，outline 批准后才制作。
4. `SlideSpec`：每页定义 slide type、headline、supporting points、data、source、visual intent、speaker notes。
5. Theme/Layout Engine：标题、章节、图文、对比、流程、数据、案例、结尾等受控版式；统一网格、字号、留白、颜色、图标和品牌 token。
6. 结构化内容优先：图表、表格、流程、关键数字使用可编辑 shapes/data；生成图片只用于无事实负担的插画、背景或氛围视觉。
7. HTML/structured schema 渲染为 PPTX/PDF，默认可编辑；复杂视觉页栅格化必须显式标记并获得确认。
8. 自动 QA：页数、overflow、最小字号、对齐、对比度、字体替换、图片分辨率与每页素材覆盖、整页栅格化/局部图片对象比例、引用完整性、PPTX 结构和 PPTX/PDF 一致性。
9. 人工/视觉 review：叙事、信息密度、视觉节奏、重复版式、数据可读性。
10. 支持按页自然语言修订并保存 Artifact revision；只重做被修改的页。

PPT 精美的核心是“叙事结构 + 版式系统 + 可编辑数据视觉 + 合适素材 + QA”，不是让图片模型生成一组带文字的整页截图。

## 四、没有图片/视频生成能力时

| 请求 | 仍然产出 | 不得声称 |
|---|---|---|
| 图片/海报 | 完整 brief、构图线框、prompt pack、素材清单、精确文案与品牌层；有现成素材时可做确定性裁切/排版/导出 | 不得把占位背景或提示词称为生成成图 |
| 视频 | script、storyboard、shot list、首尾帧规范、字幕、旁白、音乐建议、asset list、edit decision list | 不得把分镜或空壳 MP4 称为生成视频 |
| PPT | 使用 shapes、typography、charts、tables 和已授权 workspace assets 继续制作；没有合适图片时采用干净的信息设计或明确占位 | 不得用破图、虚构图片、无来源事实或不可解释的整页截图凑页 |

统一处理：

1. preflight 返回 `degraded` 或 `unavailable` 及用户可操作原因。
2. 保存 brief 和已完成中间产物为 blocked/resumable request。
3. 若等价 Provider 在提交前健康可用，可自动路由；质量/成本/合同有实质变化则重新确认。
4. 若 Provider 可能已经接受，状态进入 `reconciling`，不得重复提交或扣费。
5. 能力恢复后从最小未完成阶段继续。

## 五、火山 Agent Plan 在本方案中的位置

火山账号是候选 Provider 资源，不是新的产品入口，也不等于已经具备生产能力。接入前必须分别验证：

- API Key、模型白名单、endpoint、并发/速率、AFP/额度、错误和耗尽语义；
- 图片/视频真实 API 调用、异步 task、下载 URL 和服务器网络；
- 与 MiniMax current、MiniMax optimized 的真实客户同题 A/B；
- tenant、凭据池、Credits、幂等、恢复、内容安全和可观测性；
- 内部 allowlist 浏览器业务流与有效 Artifact。

建议角色：

- PPT 独立建设，不绑定任何媒体 Provider。
- 火山图片/视频先作为 shadow A/B 和 Pro/Ultra 候选。
- MiniMax 在观察期保留为可控 fallback。
- 未达到评测门槛前不切默认路由，不因已购买套餐而倒推产品选择。

### 官方 Skill 的采用边界（2026-07-26 已核验）

已从 `https://skills.volces.com/skills/volcengine/agentplan` 隔离下载并审查：

- `byted-ark-seedream-skill` v3.0.0，lock hash
  `4a150ace8b7d8ffa28e7fab87ec0398e5dff72221a032ee41a3013a617329798`；
- `byted-ark-seedance-skill` v4.0.0，lock hash
  `cc4b905b8fbec7cc7c9fe94f16c94353a986001df03bebfcba38871b7c86b82d`。

采用的是经过复核的协议和能力矩阵，不直接执行下载脚本。官方脚本会从 chat/env/OpenClaw/Hermes
读取或保存 API Key、写用户目录、保存本地偏好并用本地文件/cron 管理任务，这些行为不适用于多租户 SaaS。
Astra 适配必须替换为：

- SaaS credential vault 和 tenant/provider capability；
- `MediaGenerationTask`、Credits、幂等和 durable recovery；
- tenant workspace/storage 和不可变 Artifact；
- AgentTemplate 的最小 Skill/Tool grant；
- server-owned provider/model routing 和审计回执。

已识别并修复的关键协议差异：

- Agent-facing 产品合同继续使用 `doubao-seedance-2.0` 等公开模型名；官方 Skill v4.0.0 在提交前将
  公开名映射为版本化 Provider ID。Astra 已把该映射收进 server-owned adapter，不向 Agent 暴露，
  也不再把低套餐返回的 `UnsupportedModel` 错判为“版本化 ID 无效”；
- 当前 adapter 支持 `doubao-seedance-1.5-pro`、`doubao-seedance-2.0`、
  `doubao-seedance-2.0-fast` 和 `doubao-seedance-2.0-mini`。按照 2026-07-24 的运营复核策略，
  Medium 显式路由到 `2.0-mini`，Large/Max 的商用质量默认使用标准 `2.0`，fast 仅在速度/成本策略明确时使用；
- `doubao-seedance-1.5-pro` 已进入退役兼容期，只允许已被 Provider 接受且已固定模型的旧任务继续对账，
  不是新任务或长期商用依赖；
- 2.0 系列的实际可用性仍必须以账号套餐和 Provider receipt 为准；本地 `plan_tier` 是管理员声明，不能替代
  火山控制台的真实权益。`UnsupportedModel` 必须进入 credential/entitlement 诊断并安全降级，不能靠猜模型名重试；
- Seedream 连贯组图不仅需要开 `sequential_image_generation`，prompt 还必须明确张数、逐张内容和一致性约束；
- Seedance 根据参考图/视频/音频、首尾帧、联网、draft/flex、分辨率、时长和速度需求做能力路由，
  不是从 prompt 关键词猜模型；
- 图片支持 1–14 张参考图和 `reference_strength`；视频首帧/尾帧必须使用明确的 `role`；
- 整图模糊不是默认质量优化。只有检测到 Provider 伪文字且用户接受恢复处理时才可启用，
  并必须在 receipt 中记录 `background_sanitized=true`。

#### Skill、API 与 Astra Tool 的完整边界

| 能力 | 官方 Skill/API | Astra 当前 Agent Tool | 决策 |
| --- | --- | --- | --- |
| Seedream 单图、单参考、2K/3K/4K | 支持 | 支持，按 SaaS tier server-route | 当前正式使用 |
| Seedream 1–15 连贯组图、最多 14 参考图、`reference_strength` | 支持 | adapter 已理解；Agent Tool 仍是单 Artifact/单创意参考 | 等 multi-artifact Credits、恢复、选择和交付合同后再开放 |
| Seedream web search、stream、prompt optimize、水印开关 | 支持 | 不作为 Agent 参数 | 保持 server-owned，交付固定无水印 |
| Seedance 文生、首帧、首尾帧、生成音频 | 1.5/2.0 均支持 | 支持 | 当前正式协议 |
| Seedance 1.5 Pro | 4–12 秒；480/720/1080p；固定六种比例；支持 draft/flex；不支持联网、多图/视频/音频参考、编辑/延长 | 仅已接受旧任务的兼容对账；Agent Tool 不为新任务暴露该模型 | 兼容接入，不作为新任务路由 |
| Seedance 2.0 标准 | 最长 15 秒；最高 4K；支持多模态参考、联网、编辑/延长；不支持 draft/flex | Large/Max 默认模型；当前 Tool 只开放与 1.5 共同的稳定子集 | 后续按可恢复执行单元逐项扩展 |
| Seedance 2.0 Fast/Mini | 最长 15 秒、最高 720p；高级参考能力与 2.0 对齐 | Medium 默认使用 Mini；Fast 仅按管理员速度/成本策略路由 | 仍需目标账号真实权益和生成 receipt |

图片和视频 Skill 都必须定制，但定制位置不同：

- **Skill 层**：补商业 brief、真人/产品一致性、分镜、确定性文字/品牌层、实际 Artifact 质检和不可重试边界；
- **Tool/Provider 层**：保留凭据、套餐 entitlement、模型能力矩阵、Credits、幂等、异步 task、存储和 fallback；
- **不能复制的官方行为**：chat/env API Key 搜索、用户目录下载、偏好文件、本地 pending queue 和 cron；
- **不能只改 prompt 的能力**：多图参考、连贯组图、视频参考、edit/extend、draft/flex。没有 Tool schema、
  durable state、Credits 和 Artifact 合同，Skill 文案不得声称可用。

本轮已把 `require_audio=false` 从 Agent Tool 一致传到 Agent Plan 请求，避免静音/旁白任务被 Provider
隐式开启音频；同时按公开模型名先校验 1.5 Pro 能力，再映射到官方 Skill v4.0.0 的版本化 Provider ID，
避免映射后通过字符串判断遗漏 1.5 限制。

### 本地 Provider 实证（2026-07-26）

以下只代表当前本地密钥和本地运行库，不代表生产已配置或已验证：

- Agent Plan 文字网关已用真实调用验证三档模型：
  `doubao-seed-2.0-mini`、`doubao-seed-2.1-turbo`、
  `doubao-seed-evolving` 均能通过 Anthropic-compatible API 返回结构化 Tool Call；
- SaaS 文字路由已经落成 `Agent Plan primary -> MiniMax-M3 fallback`。当本地火山凭证暂时不声明
  `text` 能力时，同一无副作用请求在 Provider 请求前自动切换到 MiniMax 并返回成功结果；凭证能力随后恢复；
- Agent Plan TTS 已按官方 `doubao-seed-tts-2.0` HTTP 流协议真实返回 MP3。
  本地 Agent 完整业务流由火山文字模型规划并调用火山 TTS，`AgentRun`
  `465c2531-31d9-4aa9-a553-be0a3458cda2` 已 `delivered`，媒体任务
  `ad74f949-4277-4e22-adf8-6aeb11065f00` 已 `succeeded`，交付文件为
  `workspace/audio/agent_plan_tts_business_flow_v2_ad74f9494277.mp3`；
- 兼容期工具内部名仍是 `generate_speech_minimax`，但其产品语义已经是 provider-neutral managed route；
  Runtime receipt 必须显示实际 `provider` 和 `model`，不得按工具名推断供应商；
- Agent Plan 只读任务列表验证成功，说明密钥可访问 `/api/plan/v3`；
- `doubao-seedream-5.0-lite` 真实图片生成成功，图片能力可参与本地路由；
- 公开名和官方 Skill 版本化 ID 均已做最小 4 秒、480p 提交前探测：
  `doubao-seedance-1.5-pro`、`doubao-seedance-2.0`、`doubao-seedance-2.0-fast`、
  `doubao-seedance-2.0-mini` 全部返回 `UnsupportedModel`，没有创建 Provider task、没有生成费用；
- 当前运营复核策略显示：Small 无视频，Medium 的新任务目标为 Seedance 2.0 Mini，Large/Max 默认标准
  Seedance 2.0；1.5 Pro 仅保留旧任务兼容。当前 Key 的文字、Seedream、TTS 成功且所有视频模型拒绝，
  与 Small 权益完全一致，因此本地按 provider behavior 将账号纠正为 `plan_tier=small`、
  `capabilities=text/image/audio`，不再保留虚假的视频 capability 或无意义 model circuit；
- 2026-07-26 17:31 从正式“制作交付物 → 短视频”入口创建了 6 秒、9:16、真人使用产品、
  中文旁白的 ULTRA 请求 `6e50d404-a0f5-48f8-a4ef-a0a20eab32ca`。Runtime 正确创建了
  request-scoped storyboard，并由 provider-neutral Tool 依次检查火山和 MiniMax；火山三个
  Seedance 2.0 model circuit 均已打开，MiniMax video circuit 为 provider error `2056`，
  因此没有 Provider 接受、没有创建任务、没有扣 Credits、没有 MP4；
- 同一次浏览器流暴露的“无 MP4 仍显示可批准”已修复：presentation/video Runtime 完成后统一执行
  Artifact reconciliation。上述请求现在为 `failed/artifact_verification_failed`，
  `last_error_code=deliverable_artifact_missing`，不再出现批准入口；
- 视频执行 prompt 现在只允许调用一次 provider-neutral 生成 Tool，由 Tool 自己完成 Provider fallback；
  Provider 均不接受时禁止 Agent 自行连点重试、创建 Trigger 或把 storyboard 当成交付结果；
- 本地管理员原填写的 `plan=large` 与上述真实权益冲突，已纠正。Plan Key 本身的只读任务接口不返回
  套餐名称；若要获得“订单级 SKU/有效期”而不是行为级判断，仍需登录控制台或使用需要火山 AK/SK
  签名的 `GetPersonalPlan` 管理 API。再次点击通用鉴权验证不能扩大模型权益；
- 同题 MiniMax `MiniMax-Hailuo-2.3` 文字生成视频返回 10.125 秒、1366×768、无音轨，
  未满足 9:16 + 音频的广告硬门槛。这证明“有 fallback”不等于“fallback 能满足同一交付合同”。

配置一致性边界：

- 新建 Agent Plan 凭证默认声明 `text/image/audio`；Medium、Large、Max 可声明 `video`，Medium 新任务
  路由到 `2.0-mini`，且声明值仍必须经过 Provider 实证；
- 迁移只创建 provider model 和 SaaS route，不擅自扩大管理员已有凭证的 capabilities；
- 本地凭证已按真实 Provider 行为收敛为 `small + text/image/audio`；
- 生产仍需在发布变更窗口内显式配置/核验凭证 capability、套餐、额度和真实调用，当前不得标记
  `production_verified`。

### 受管 Seedream / Seedance Skill（2026-07-26 本地落地）

- 新增 `volcengine-seedream-commercial`：采用官方 Seedream v3.0.0 的触发、参考图、连贯一致性和
  提示词方法，但只允许调用 Astra 的 provider-neutral `generate_image_minimax`；
- 新增 `volcengine-seedance-commercial`：采用官方 Seedance v4.0.0 的模型能力路由意图、首尾帧、
  音频与异步任务规范，并补充真人广告的 timed shot plan 和实际 MP4 商用质检；
- 两个 Skill 都保留 source/version/lock hash/reference，不复制 API Key 检测、用户目录、本地 cron、
  pending queue 或直接 JavaScript 执行；
- `Douyin Operations Manager` 模板已获得这两个 Skill，同时保留原有
  `brand-safe-media` 和显式媒体 Tool grant；Skill 不授予 Tool；
- 当前“抖音运营经理”工作区已真实同步两个 Skill 的 `SKILL.md` 与 provenance reference；
- 当前 Small 账号下 Seedream Skill 可执行；Seedance Skill 会先服从运行时 capability，火山视频不入选，
  MiniMax 只有在健康可用且在 Provider 接受前才可自动兜底。
- 2026-07-26 18:45 已通过真实 Agent 会话执行 `volcengine-seedream-commercial`：
  `volcengine_agent_plan / doubao-seedream-5.0-lite` 成功交付
  `workspace/images/agent_plan_skill_real_person_ad_bd78482be7cb.png`。样张为 9:16 真人持杯场景，
  无模型文字、Logo、水印和整图模糊；人物、双手和杯体可读，但杯体被模型画成带把手的智能杯，
  说明 Skill 明显改善了商用构图和污染控制，仍不能替代真实产品参考图或品牌资产的一致性约束。
- 同轮视频验证暴露了两项本地缺口：UI 附件标签 `images/...` 未物化为规范
  `workspace/images/...` 路径，以及 Agent 把本地参数校验失败误当成可重试。当前已让媒体输入只接受并
  规范化受限的 workspace 媒体根目录，同时把“任何失败均计入单次 Tool invocation budget”写入
  Seedance Skill。重启同步后复验只运行 1 次 `generate_video_minimax`，返回
  `media_video_provider_unavailable`；数据库 20 分钟窗口内新增视频 Provider task 数为 0，
  Credits 消耗为 0，也没有 Trigger。

### 受管 Commercial Presentation Skill（2026-07-31 本地落地）

- 新增 provider-neutral `commercial-presentation`，覆盖 PPT 任务触发、自然语言 brief 推断、
  来源/事实合同、叙事结构、自适应版式、真实素材、PPTX/PDF 双格式、结构检查、人工评审和局部修订边界；
- Skill 明确不授予 Tool、Provider、Credits、tenant 或批准权限；正式客户交付只服从
  `builtin.presentation.v1` 的 server-owned brief、路径、输出合同和审批状态；
- `convert_html_to_pptx` 与 `convert_html_to_pdf` 继续作为 product-wide 默认 Tool，并已具备 Durable
  Runtime typed adapter；不为 PPT Skill 建立重复的角色 Tool grant；
- Skill 只分配给职责真实需要演示文稿的 `Content Creator`、`Douyin Operations Manager`、
  `Growth Hacker`、`Product Manager`、`Project Manager` 和 `Chief of Staff`，没有全局推送给
  `Private Assistant` 或所有未来 Agent；
- 本地 `Douyin Operations Manager` 实例
  `b4f0f5d8-4fb8-40d1-b10c-3fb7bdab6864` 已完成 registry、模板和 workspace 三层同步；
  这证明 `skill_ready` 与本地 Agent 授权，不等于 PPT 商用质量批准或生产发布。

### 受管 Commercial Voiceover Skill（2026-07-31 本地落地）

- 新增 provider-neutral `commercial-voiceover`，覆盖旁白、口播广告、讲解、无障碍音频和视频配音；
- Skill 固化精确脚本、发音、时长约束、声音身份、持久化音频回执、真人听审和视频混音边界；
- 未指定声音 ID 时保留 Agent Plan → MiniMax 自动路由；显式 `voice_id` 只在对应 Provider
  命名空间中执行，禁止自动兜底时静默换成另一种声音；
- Skill 按最小职责分配给 `Content Creator`、`Douyin Operations Manager` 和
  `TikTok Strategist`，没有全局推送给私人助手或所有 Agent；
- 兼容期内部 Tool 名仍为 `generate_speech_minimax`，但模型和用户可见的展示、说明已改为
  provider-neutral `Generate Speech`。

本地无新增 Provider 消耗的证据复核（2026-07-31）：

- `local_text_provider_flow_proven=true`：`Doubao Seed 2.0 Mini Lite`、`Doubao Seed 2.1 Turbo Pro`
  和 `Doubao Seed Evolving Ultra` 均存在真实 `delivered` Agent Run；当前三档文字路由均为
  `Agent Plan primary -> MiniMax fallback`，Tool Ledger 和 Credits 必须记录实际 Provider，不能由模型名或
  内部 Tool 名推断；
- `local_voice_provider_flow_proven=true`：当前抖音运营经理有 6 条成功语音媒体任务，其中
  `volcengine_agent_plan / doubao-seed-tts-2.0` 5 条、`minimax / speech-2.8-turbo` 1 条；所有任务均有
  非空版本化 `output_path`、可解码 MP3 和匹配的本地文件；
- `local_voice_credit_settlement_proven=true`：上述 6 条任务的 reservation 全部为 `finalized`，并分别存在
  一条匹配 Provider、model、modality 和 reservation ID 的负向 `consume` 交易，没有悬空 hold；
- `local_voice_preview_playback_proven=true`：历史 Agent 会话重新加载火山 TTS 文件后，右侧 Workspace
  `<audio>` 使用版本化下载 URL，`readyState=4`、`duration=2.256`、无媒体错误；同一文件通过 macOS
  `afplay` 完整听音。MiniMax 文件恢复原版本化引用后也在 Workspace 中显示，`readyState=4`、
  `duration=4.342156`、无媒体错误；
- 复核发现一条历史 MiniMax 语音产物在成功后被 Agent 通过 `move_file` 改名，导致
  `media_generation_tasks.output_path` 和 Tool Ledger 指向已不存在的旧路径。旧样本已通过保留改名副本并
  恢复相同 SHA-256 字节到原版本化路径完成无损修复；运行时现在禁止 Agent 修改、移动、替换或删除任何
  被媒体任务引用的版本化输出，并返回 `durable_media_output_immutable`，防止再次产生悬空回执；真人显式
  数据管理仍由独立产品流程负责；
- 本轮只读取既有 Agent Run、任务、Credits 和文件并执行本地播放/修复，没有发起新的 Provider 请求，
  没有新增模型 Credits，也没有修改生产配置。以上均为 `local business_flow_proven`，不替代生产发布后的
  路由、计费、存储、浏览器和真人听审验证。

### 同题豆包 Benchmark 结论（2026-07-26）

固定题目、结构化验收项和样本路径记录在
`tmp/creative-benchmark/2026-07-26-agent-plan/benchmark-plan.json`。该次结果不是“选一个总冠军”，
而是把各交付合同的真实差距拆开：

- 图片：豆包样本视觉信息更丰富，但带可见水印；其中一个版本还违反“不要出现手”的明确约束。
  Agent Plan Seedream 能真实生成无水印图片，但仍需在同一组任务中继续量化构图、人物/产品一致性、
  文字污染和指令遵循，不能因 Provider 可调用就宣布商用品质达标。
- 人物广告视频：豆包 Seedance 2.0 Mini 样本为 720×1280、约 10 秒、H.264/AAC，具有连贯多镜头、
  原生音频和更好的角色连续性，但带水印。本地 MiniMax fallback 原始视频无音轨且未直接满足竖版合同；
  通过 `generate_speech_minimax + compose_video_audio` 可交付 768×1366、10.125 秒、H.264/AAC 的
  旁白广告，但它不等于人物原生对白或口型同步。逐帧复核还发现：豆包样本没有表现温度显示、结尾退化为
  单独产品陈列且存在“豆包AI生成”水印；MiniMax 样本从城市窗景跳到纯蓝摄影棚，未遵循咖啡店场景，
  也没有温度显示。两者都不是可直接投放的商用成片；豆包只是在镜头叙事和原生声音上形成当前上界，
  不能把“整体更好”写成“已达标”。
- PPT：本地 8 页 PPTX 可编辑、无文本 overflow、视觉偏暗、素材少、版式重复；2026-07-27
  逐页复核进一步发现第 5 页仍虚构材质和交互属性，因此不能再标记为“事实口径安全”。
  豆包 8 页样本包含 67 个媒体对象、视觉更丰富，却虚构 NTC、316 不锈钢、12/24 小时保温、
  Bluetooth 5.0、APP、渠道/KOL 等未提供事实，而且第 2–7 页发生 overflow。
  因此本地赢在事实和结构安全，豆包赢在视觉密度；二者都未同时达到“精美 + 事实可信 + 无溢出 +
  可编辑”的商用门槛。

后续优化必须围绕上述可测差距，不允许把固定 prompt、隐藏水印、整图模糊或手工挑一张最好结果包装成
“全面提升”。

### PPT 自适应视觉合同（2026-07-28 本地第一阶段）

- 正式 PPT Runtime 现在生成 server-owned `PRESENTATION_VISUAL_POLICY`，按页数、档位和 brief
  计算最低独立图片数、最低版式数、单张图片复用上限和最低可编辑信息设计页数，不向用户暴露模板或
  Provider 选择；
- `slide_spec.json` 的 `adaptive-v1` 计划要求每页声明 `slide_type`、`visual_kind` 和
  `asset_ref`。图片页必须实际渲染所声明的本地素材，可编辑图表、流程、表格和信息设计不得伪装成图片；
- 校验器会拒绝连续重复版式、素材数量不足、图片超限复用、声明素材未出现在对应页面，以及可编辑视觉页
  不足。旧 `slide_spec` 没有 `visual_plan_version` 时继续按 v1 合同校验，避免破坏已启动的历史 Run；
- 8 页 Pro 图文商业提案的当前策略示例是：至少 3 张独立图片、4 种版式、单图最多用于 3 页、至少
  2 页采用可编辑图表/流程/表格/信息设计。它是随合同计算的下限，不是固定题材或固定页面模板；
- 本阶段只建立 `tool_ready + tests_pass` 的动态编排和 fail-closed 合同，没有再次调用付费 Provider，
  也尚未证明任意开放场景达到商用门槛。

该组同题样本只保留为**历史回归锚点**，不能成为产品支持的模式清单，也不能作为整体质量结论。
商业产品的持续评测必须同时使用：

- 滚动抽样的已授权、匿名化真实客户 brief，保留自然分布和长尾需求；
- 按行业、目标、渠道、受众、语言、输入素材、约束类型、画幅和风格动态组合的开放场景；
- 与开发集隔离的留出集，评测前只公开数量和 SHA-256 commitment，不公开题目正文；
- 隐藏 Provider、model、文件路径的盲评包，评分完成后才用私有 key 归因；
- 图片、视频和 PPT 各自的结构硬门禁，加上需求遵循、事实/身份一致性和商用可用性评分。

本地 provider-free 实现位于：

- `backend/app/services/creative_evaluation.py`
- `backend/app/services/creative_sample_ingestion.py`
- `backend/app/services/creative_artifact_evaluation.py`
- `backend/app/services/creative_blind_review.py`
- `backend/scripts/generate_creative_evaluation_suite.py`
- `backend/scripts/anonymize_creative_brief_export.py`
- `backend/scripts/inspect_creative_artifacts.py`
- `backend/scripts/prepare_creative_blind_review.py`
- `backend/scripts/score_creative_blind_review.py`
- `backend/scripts/validate_multimodal_capability_matrix.py`：核对文字、图片、视频、语音、音乐和 PPT
  的入口模板、角色 Skill、Tool 注册、typed runtime adapter 与默认/显式授权路径；只证明本地治理合同，
  不调用 Provider，也不替代账号资格或质量评审；
- `backend/tests/test_creative_evaluation.py`

2026-07-27 已完成一次只读生产抽样和历史回归盲评链路实证：

- 生产 release 仍为 `1.11.8 / 1d7dd40a50bb32a0917cb66a1a7fc0a0609bdd1e`；本轮只读查询，
  未修改生产配置或数据；
- 最近 30 天抽样看到 PPT deliverable 2 个，以及图片/视频 Tool 执行的成功与失败记录；真实需求主要
  仍落在 quick Tool/Media 路径，不能只抽正式 Deliverable 表；
- 19 条生产候选 brief 已在 SSH stdout→本地 stdin 流中脱敏，原文未落盘；10 条图片、7 条视频、
  2 条 PPT 全部保持 `pending_review`，在人工隐私/内容审核前不得进入评测集；
- 历史同题回归按隐藏清单 provider/model/原文件名的方式打包。图片 3 个、人物广告视频 2 个、PPT
  2 个候选均通过文件结构检查；但这只是 `manifest_and_filename_only`，二进制 metadata、可见水印、
  文档文字和音频不会被篡改，因此历史样本不能宣称感知层完全盲；
- 单人初审封存后再解盲：Agent Plan 图片为 `83.33/100`、达到本轮暂定 80 分线；MiniMax 图片为
  `45.83/100`；豆包图片因可见水印被硬门禁阻断。该结果只证明一个历史 brief 上的明确差距，
  不能外推为整体模型排名；
- 两个视频都不商用：MiniMax 成片因虚构杯身文字阻断，豆包 Seedance 2.0 Mini 因全程水印阻断；
  两者的对白可懂度、口型和混音仍需具备听音条件的人审；
- 两个 PPT 都不商用：豆包样本因虚构事实、2–7 页 overflow 和无来源阻断；本地可编辑样本无
  overflow，但仍因虚构材质/功能及无来源阻断。

上述单人结果是缺陷定位证据，不是正式 Benchmark 放行。正式周期仍要求滚动已批准真实 brief、动态场景、
隔离 holdout 和至少 3 人独立评审。

固定的是同一轮比较时的 brief、硬约束、输入素材 hash、候选预算和评分合同，不固定用户题材、创意模式、
模板或输出风格。不同 seed 必须生成不同组合；当前固定样本只能检测已知缺陷是否回归，不能被用于针对性
调 prompt 后宣称“全面提升”。本地 `restricted-holdout.json` 的 `chmod 0600` 只证明文件分离，
生产评测仍必须使用独立访问控制和不可由优化执行者读取的存储。

商业视频必须先选择音频模式：

- 镜头内人物同步对白：只允许有原生音轨能力的 Provider 路由，当前本地火山视频权益未恢复前不得承诺；
- 旁白广告：先用竖版首帧约束图生视频，再用语音工具生成旁白，最后通过
  `compose_video_audio` 做本地确定性混音并验证最终 MP4；
- 静音素材：显式交付静音，不得让用户误以为语音生成失败或丢失。

### 正式多人评审与感知证据（2026-07-27 本地 shadow）

- 新增 `creative_review_panel.py`，把历史单人缺陷定位评分与正式商用放行分开；
- 正式 panel 默认至少 3 名独立评审，每名评审必须完整评价所有匿名候选，评审 receipt 不能重复；
- 硬门禁只有全体一致通过或一致失败才形成结论；缺失或分歧保持 `incomplete`，不能按多数意见静默通过；
- 维度评分使用评审均值，但分差超过 1.5 分时保持 `incomplete` 并要求人工裁决；
- 图片要求 `OCR + 每位评审 human_visual`，视频要求
  `逐帧 OCR + 每位评审 human_visual + 每位评审 human_audio`，PPT 要求
  `document_semantic + 每位评审 human_visual`；
- 所有证据 receipt 必须绑定匿名候选的实际 Artifact SHA-256；旧文件、替换文件或 hash 不一致会
  fail closed；
- 新增 `collect_creative_ocr_evidence.py`，只做本地图片/视频逐帧 OCR 和私有 receipt，不调用 Provider，
  也不自动判定商用通过；
- 当前本机已安装 Tesseract `chi_sim`。OCR 使用稀疏文本模式、全图增强和四角增强，并对中文字符间
  空格/符号及单字符误识别做 exact/possible 两级禁用词检查；低置信度 possible match 只进入人审，
  exact prohibited match 会直接使 `no_unrequested_watermark` 硬门禁失败；
- 对滚动样本重扫后，豆包图片由原先 0 token 提升为 69 token，并命中
  `prohibited_term_possible_match=AI生成`；豆包视频 10 帧、60 个增强视图中明确命中 `豆包`，
  同时疑似命中 `AI生成`。MiniMax 图片/视频和 Agent Plan 图片未命中同组平台水印词。这个结果同时
  证明了“有中文语言包”仍不等于 OCR 可替代视觉人审；
- 新增 `prepare_creative_review_panel.py`、`assemble_creative_review_panel.py` 和
  `record_creative_human_evidence.py`。图片、视频、PPT 已分别生成 3 份隔离的 provider-free
  评审模板，共 9 份；模板不是实际评审结果，不得冒充 3 名独立评审已经完成；
- 对人物同步对白视频可额外要求 `human_av_sync` receipt；旁白广告不强制口型同步，但仍要求
  `human_audio` 检查听感、失真、混音和文案一致性；
- 新增 `score_creative_blind_review_panel.py`，只有 panel、感知证据和评分同时完整时才输出正式
  `commercially_usable=true`。

### Artifact 审批质量门禁（2026-07-27 本地 shadow）

- 新增 `deliverable_quality_gate.py`，把正式 panel 结论封装为版本化 receipt，并绑定整组交付文件的
  `artifact_key -> SHA-256`；receipt digest、文件 hash 或多文件 receipt 不一致时均拒绝批准；
- 只有至少 3 名独立评审、所需证据齐全、无硬门禁失败、无分歧且
  `commercially_usable=true` 的 panel 才能形成 `passed`；
- 自动 OCR 只能把 exact prohibited finding 转成 `blocked`，不能用“未识别到水印”或
  possible match 形成 `passed`；
- Artifact approval 已读取该 receipt。显式 `blocked/incomplete/invalid` 即使 rollout flag 关闭
  也会阻断；没有 receipt 时由 `DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED=false` 加 tenant/Agent
  双 allowlist 保持旧流程兼容。开关打开但 allowlist 为空时仍不影响任何客户；
- API 新增 additive `approval_readiness`，前端会解释质量状态并在阻断时禁用“批准交付”；
- 本地历史豆包视频 OCR receipt 已演练生成 hash-bound `blocked` receipt，命中硬门禁
  `no_unrequested_watermark`；该演练不调用 Provider，也不产生付费；
- 配置键已同步到本地 `.env.example`、部署 `.env.example` 和现有三个 compose 文件，默认均为
  `false + empty allowlists`，尚未修改生产环境。

2026-07-27 后续已补齐本地受管评审层：

- 新增评审批次、分配和 evidence 持久化模型与 Alembic migration；
- reviewers/create/latest/get/submit/evidence API 在服务端执行 tenant、assignment、交付创建者排除、
  `Identity` 去重、完整性和幂等约束；
- 每份评审绑定 Artifact 版本和 SHA-256；相同提交可安全重放，不同提交不能覆盖已封存判断；
- 管理员可写入受审计的私有 evidence ref；自动 finding 只能阻断，不能单独产生 `passed`；
- 前端交付卡片和独立评审工作台已接入，rollout 仍受
  `DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED` 与 tenant/Agent 双 allowlist 控制。

真实浏览器验证使用原开发库的隔离副本，不消耗 Provider Credits：

- PPT 批次由三名不同 Identity 的非创建者逐一登录，状态从 `0/3 open` 到
  `3/3 passed`，并生成 `managed-panel:<review_id>:<version>` 服务端 receipt；
- 视频批次在 0 名人工提交时绑定精确
  `prohibited_term_detected=sample-watermark` 的逐帧 OCR evidence，立即进入
  `0/3 blocked`，证明自动证据只负责 fail closed；
- 随后应用切回原开发库，确认原库 `deliverable_quality_reviews` 仍为 0；隔离数据库、SQL seed 和
  dump 均已删除；
- 原开发组织实际只有两名合格非创建者，页面显示创建者不可选择、
  `创建评审 (2)` disabled、`批准交付` disabled。这是当前真实组织配置的正确阻断，不得通过复用账号
  或伪造评审 receipt 绕过。

2026-07-31 又在原开发库增加了三名明确标记为 `local_quality_gate_qa` 的本地 QA Identity，只用于
证明主租户的产品状态机，不把这些账号冒充为三名真实独立质量评审人：

- 创建者从聊天流中的 PPT 交付卡片选择三名不同 QA Identity 并创建受管评审批次；
- 三个 QA Identity 分别封存完整提交，提交备注明确声明“仅用于状态机验证，不构成真实独立商业
  质量结论”，批次从 `0/3 open` 变为 `3/3 passed` 并生成 hash-bound receipt；
- 创建者回到同一真实浏览器会话，看到“质量检查已通过 / 3/3 位评审人已完成”，点击“确认交付”；
- 浏览器随后显示“交付已确认”，数据库请求进入 `succeeded / delivered`，两份最终 Artifact 均为
  `approved`；
- 该流程未调用付费 Provider、未修改生产环境，也没有改变正式 Benchmark 仍需三名真实独立人员的
  门槛。

因此当前可以分别标记：

- `code_exists=true`
- `tests_pass=true`
- `migration_smoke_passed=true`
- `local_review_state_transitions_proven=true`
- `local_main_tenant_full_approval_clickthrough=true`
- `independent_human_quality_approval_complete=false`
- `provider_verified` 沿用既有单项 Provider 证据，本阶段没有新付费调用
- `production_verified=false`

仍未完成的是生产隔离 evidence 服务和签名存储、生产 allowlist/迁移、真实生产评审人配置、真实独立
人员的质量判断、告警/回滚演练和生产灰度。当前管理员 evidence 写入是受审计的内部 attestation；
它可以阻断，但在接入独立 evaluator 身份、不可变对象存储和签名摘要前，不能被描述为第三方或机器独立
证明。

### 生产派生滚动 Pilot（2026-07-27 第二轮）

本轮把 19 条已脱敏生产候选从 `pending_review` 进入显式人工审核：

- 8 条批准、11 条需要补充信息；
- 图片为 `7 approved / 3 needs_clarification`，视频为
  `1 approved / 6 needs_clarification`，PPT 为
  `0 approved / 2 needs_clarification`；
- 重复尝试按语义 `benchmark_cluster` 去重；缺参考素材、品牌授权、受众、目标、来源或私有主体资料
  的需求不得进入 Provider 对比；
- 本轮只从批准集抽取一个图片 cluster 和一个视频 cluster，各 Provider 一个候选、零自动重试。
  它是缺陷定位 pilot，不满足正式 Benchmark 的样本量和 3 人独立盲评门槛。

真实同题结果：

- Agent Plan `doubao-seedream-5.0-lite` 的图片细节、空间层次和 prompt 遵循明显强于
  MiniMax `image-01`；旧 benchmark harness 将 `4K` 直接作为 size，Provider 实际返回 2:3，
  因此该候选只能证明 Provider 画质，不能计入 3:4 正式同题分数。harness 已改为
  `quality + aspect_ratio -> explicit pixels`；
- 豆包 4.5 同题返回 1728×2304 的 3:4 PNG，构图和细节较强，但下载文件带可见水印；
- 当前 Agent Plan 凭证的视频预检稳定为 `capability_mismatch`，没有 Provider task，继续证明行为级
  `Small` 无视频权益；
- MiniMax `MiniMax-Hailuo-2.3` 同题实得 1366×768、10.125 秒、无音轨，违反 9:16 + 音频合同；
- 豆包 `Seedance 2.0 Mini` 同题实得 720×1280、10.08 秒、H.264/AAC，画幅和音轨满足，但带
  “豆包AI生成”水印。

MiniMax 文字生视频官方请求没有画幅字段。生产路由现在把非 16:9 视为交付合同，不再只把
`aspect_ratio` 写入 metadata：当实际 fallback 为 MiniMax 且没有同画幅首帧时，在 Provider 提交和
Credits 检查前返回 `media_video_requires_first_frame_for_aspect_ratio`。下一阶段应实现受管的
`brief -> 同画幅首帧 -> I2V -> 实测画幅/音轨/水印 -> 必要时 TTS 混音 -> Artifact` 组合链，
而不是让 Agent 自由重试或向客户隐藏错误画幅。

两个生产 PPT brief 都缺少受众、目标和来源材料，其中一个还缺私有主体的可核验资料，因此本轮没有
PPT Provider 对比。拒绝凭空补齐 brief 是评测正确性，不是 PPT 能力失败。

详细私有证据保存在
`tmp/creative-evaluation/rolling-cycle-2026-07-27/RESULTS.md`，该目录不得提交或作为公开 fixture。

### v1.11.9 本地真实交付复核（2026-07-31）

本轮使用 `admin@reeftotem.ai` 的真实本地会话和原开发数据库复核，没有新增付费 Provider 调用：

- 图片 quick Tool：`volcengine_agent_plan / doubao-seedream-5.0-lite` 已生成
  `3072×1728` PNG，历史会话刷新后可在聊天右侧 Workspace 恢复预览；
- 图片正式工作流：`builtin.poster.v1` 已生成 `4096×2304` PNG Artifact，请求状态为
  `waiting_approval / output_review`；
- 视频 quick Tool：同画幅首帧由 Agent Plan Seedream 生成，Small 套餐不具备火山视频权益，
  运行时在 Provider 接受前切换到 MiniMax Hailuo，生成 `768×1364`、`5.875s`、H.264 静音 MP4；
- 视频正式工作流：已有两条通过结构校验的最终 MP4 Artifact，分别为竖屏和横屏，
  均包含 H.264 视频与 AAC 旁白音轨，状态为 `waiting_approval / output_review`；
- PPT 正式工作流：5 页 Astra 商业演示已生成 2 张真实图片、5 种版式、PPTX/PDF 双格式。
  逐页渲染未发现裁切、重叠、黑块或乱码，但第 3 页信息密度偏低、正文偏小，当前仍是候选版，
  状态为 `waiting_approval / output_review`；
- PPT 的 provider-neutral `commercial-presentation` Skill 已注册并同步到本轮
  `Douyin Operations Manager`，转换 Tool 仍由 product-wide 默认策略授权；该同步没有重新消耗
  Provider Credits，也没有改变现有 Deliverable、Artifact 或批准状态；
- 语音的 provider-neutral `commercial-voiceover` Skill 已加入同一角色合同；其受管 seeder
  已同步到本轮 `Douyin Operations Manager` 的 registry、模板与 workspace，workspace 文件与
  仓库源文件 SHA-256 一致，Runtime Skill 索引已能解析该 Skill；
- 交付卡片已位于生成 Agent 的聊天消息流，不在输入框中。右侧图片预览曾因把预览元数据请求
  当作显示前置条件而在恢复会话后显示 unavailable；本地已改为按 durable 文件路径直接显示，
  并让图片预览接口只返回流式 URL，不再读取并嵌入整份二进制。
- 同一批正式海报、竖版最终视频和 PPT 已通过 provider-free 自动检查：文件可解码、图片比例与
  identity、视频时长/比例/音轨合同、PPTX/PDF 结构与页数均通过；图片 1 帧 6 个 OCR 变体、
  视频 6 帧 36 个 OCR 变体未命中本轮禁止平台词。该结果只证明自动门禁未发现问题，事实安全、
  水印视觉判断、溢出细节和来源追溯仍需独立人工评审。
- 本地 SaaS“媒体路由”现按真实运行时展示：图片、语音为
  `volcengine_agent_plan -> minimax` 且两条路径就绪；当前 Small Agent Plan 不具备视频权益，
  所以视频仍显示相同自动顺序但实际可用 Provider 只有 MiniMax；音乐明确为 MiniMax-only。
  可编辑模型与质量参数只属于 MiniMax 兜底配置，不再伪装成整条统一路由。

因此当前准确状态是：

- `code_exists=true`
- `tests_pass=true`
- `local_provider_flow_proven=true`
- `local_browser_generation_flow_proven=true`
- `local_candidate_artifacts_exist=true`
- `presentation_skill_ready=true`
- `voiceover_skill_ready=true`
- `local_main_tenant_full_approval_clickthrough=true`
- `local_quality_approval_complete=false`
- `commercially_usable_proven=false`
- `production_release_v1_11_9=true`
- `production_agent_plan_media_route_verified=false`

不能再写成“图片/视频/PPT 没做”，也不能写成“已经全部完成”。下一阶段的核心不是继续增加生成按钮，
而是把正式质量评审、批准、修订、生产 Provider 配置和滚动 Benchmark 收成一个可运营产品闭环。

### Benchmark 整轮就绪审计（2026-07-31）

新增 `backend/scripts/audit_creative_benchmark_run.py`，将单候选/单评审的历史缺陷定位结果与正式商用
结论明确隔离：

- 同时检查图片、视频、PPT 三种 modality 的 batch、provider-free public package、私有解盲 key、
  候选全集和至少 3 份独立评审模板；
- 重新计算公开 Artifact SHA-256，并与结构观察 receipt 逐文件核对；文件替换、缺失、symlink、
  目录逃逸或候选集合不一致均为 `invalid`；
- 检查公开 JSON 是否泄漏私有 candidate/provider/model 标识；候选文件名仍保持 opaque；
- `sealed-scored-reviews.json` 和旧 `*-review-submissions.json` 明确标记为 provisional，只能做缺陷
  定位，不能签发 `commercial_ready`；
- 只有覆盖全部候选、每项绑定原 Artifact hash、至少 3 名真实评审并已经封存的
  `sealed-panel-results.json`，且所有候选通过商用门槛时，整轮才可能返回 `commercial_ready`；
- 正式评分输出同时绑定 batch spec SHA-256、public review package SHA-256 和逐候选 Artifact
  hash；现在还绑定 `panel-submissions.json` SHA-256、评审 receipt 列表和必需感知证据种类，并由
  审计器从原始 panel 重新计算评分。把评分文件复制到另一批次、替换产物/panel 或直接修改商用结论
  都会被整轮审计拒绝；
- 工具只读本地文件，不调用 Provider、不消耗 Credits，也不修改评测产物。

对 `tmp/creative-evaluation/blind-review-2026-07-27` 的实际审计结果为
`awaiting_human_review`、`issues=[]`：

- 图片：3 个候选、3 个 Artifact hash 通过、3 份空白评审模板、0 份正式评审；
- 视频：2 个候选、2 个 Artifact hash 通过、3 份空白评审模板、0 份正式评审；
- PPT：2 个候选、PPTX/PDF 共 4 个 Artifact hash 通过、3 份空白评审模板、0 份正式评审；
- 三类均存在 provisional 历史评分，但全部被审计器排除在正式商用证据之外。

因此现有包已经达到“可交给真实评审人执行”的工程准备状态，但准确结论仍是
`commercially_usable_proven=false`。

正式评审 receipt 现在要求三层一致绑定：panel 评审根 receipt、候选判断 receipt、人工视觉/听音/
口型/文档证据 receipt。人工证据只接受独立评审来源或 Astra 受管身份评审来源，且同一 receipt 不得
跨候选或评审复用；明确包含 `lip sync`、`口型同步` 或同步对白的场景会自动追加
`human_av_sync`。这里的 `commercial_ready` 仅表示被评 Artifact 通过正式质量 panel，成本、耗时和
默认 Provider 路由资格仍需独立执行/Credits receipt，不得由质量分数代替。

## 六、验收与后续扩展

图片、视频、PPT 必须用真实匿名化客户样本分别建立基线，并追踪：

- 任务完成率和首轮可用率；
- 动态场景各维度覆盖率、长尾分桶和留出集表现；
- brief/品牌/事实遵循；
- 人工修改次数；
- 平均时间与单位可用 Artifact 成本；
- blocked/recovery 成功率；
- 重复提交/重复计费为零；
- tenant、授权和审计正确。

后续新增模型、Provider、Skill、Tool 或 Agent 员工，一律执行 `.agents/workflows/add-product-capability.md`，不得绕过产品合同、真实样本、权限、Credits、降级、业务流和生产验证。
