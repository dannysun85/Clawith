# 图片、视频与 PPT 能力基线和目标方案

## 文档性质

本文记录 2026-07-25 仓库实现基线和已决定的目标方案。它不表示目标能力已经上线；完成状态必须按 `.agents/rules/capability-and-agent-governance.md` 分级报告。

底座决策：当前定制线继续以既有 v1.11.0 底座为基础，本阶段不合并新的上游 Clawith 版本；先独立完成图片、视频和 PPT 的产品能力闭环。

## 一、已核验的当前实现

### 共用产品入口

- `backend/app/services/deliverable_workflows.py` 已定义 `builtin.presentation.v1`、`builtin.poster.v1`、`builtin.video.v1`。
- PPT 的 `launch_policy` 是 `agent_runtime`；海报和视频仍是 `dry_run`，当前只能保存工作说明和完成预检，不能视为正式执行闭环。
- 用户合同不包含 provider/model；运行时根据 tenant、tier、能力和健康状态路由。
- 请求、运行、批准、Credits 和 Workspace Artifact 应继续作为 durable truth。

### 图片

- 当前生产媒体执行以 MiniMax `image-01` 为主，已有平台凭据池、entitlement、Credits、Agent Tool 开关、资产保存和品牌安全处理。
- 当前主要短板不是单一因素：
  - 编排层仍接近一次 prompt、单候选、首个结果；
  - 缺少系统化 prompt compiler、候选比较、质量评分、选择回执和基于失败项的修订；
  - `image-01` 在多参考、复杂商品编辑、成套一致性等任务上也存在模型能力上限。
- `brand-safe-media` 已正确规定：精确文案使用 deterministic overlay，真实商品/logo 使用冻结资产层；静态 packshot 不得冒充商品本体参与运动。

结论：先量化 `MiniMax optimized - current`，再用同题 A/B 判断火山候选 Provider 的真实增量；不能只看文档或主观印象直接替换。

### 视频

- 当前已有 MiniMax Hailuo 文生视频/图生视频相关 Tool、异步任务检查、Credits 和文件验证路径。
- 当前交付物工作流尚未开放正式 launch。
- 主要缺口是 storyboard compiler、多参考/关键帧一致性、逐镜头状态、质量评分、剪辑包装和镜头级重做，不只是替换模型。

### PPT

- 已有 `convert_html_to_pptx`、`convert_html_to_pdf` 和 `builtin.presentation.v1`；正式合同要求同时生成结构有效的 PPTX 和匹配 PDF。
- 当前缺少统一 `PresentationBrief`、`DeckOutline`、`SlideSpec`、主题/版式系统、事实引用、溢出/对齐/对比度检查、PPTX/PDF 视觉一致性检查和按页修订。
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
8. 自动 QA：页数、overflow、最小字号、对齐、对比度、字体替换、图片分辨率、引用完整性、PPTX 结构和 PPTX/PDF 一致性。
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

## 六、验收与后续扩展

图片、视频、PPT 必须用真实匿名化客户样本分别建立基线，并追踪：

- 任务完成率和首轮可用率；
- brief/品牌/事实遵循；
- 人工修改次数；
- 平均时间与单位可用 Artifact 成本；
- blocked/recovery 成功率；
- 重复提交/重复计费为零；
- tenant、授权和审计正确。

后续新增模型、Provider、Skill、Tool 或 Agent 员工，一律执行 `.agents/workflows/add-product-capability.md`，不得绕过产品合同、真实样本、权限、Credits、降级、业务流和生产验证。
