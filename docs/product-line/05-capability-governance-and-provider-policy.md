# 能力治理与 Provider 策略事实基线

- 状态：`active-design-baseline`
- 日期：2026-07-31
- 重要边界：本文同时记录“当前实现”与“目标策略”；两者不能互相冒充

## 1. 用户只选择业务能力

普通用户选择的是：制作 PPT、图片/海报、广告视频、配音、音乐、报告、表格、调研、自动化等业务结果。

平台内部治理以下层次：

```text
业务能力 / Work Contract
  → 执行者策略（助理 / 临时专家 / 员工 / Group）
  → Skill（步骤和质量方法）
  → Tool（可执行接口）
  → Grant + Scope（谁有权调用）
  → Plan + Entitlement + Credits（是否允许、是否可支付）
  → Provider Route + Model Profile（由谁执行）
  → Acceptance/Reconciliation（是否已被供应商接受）
  → Artifact + Quality + Approval（是否能交付）
```

Skill、Tool、Provider、模型、套餐和故障切换不得出现在普通用户的必填步骤中。

## 2. 当前代码事实

### 2.1 媒体

- `media_provider_order_for_modality()` 当前对 `image/audio/video` 返回 `volcengine_agent_plan → minimax`，对 `music` 只返回 `minimax`。
- 语音在未指定 `voice_id` 时允许火山优先、MiniMax 回退；显式 Provider voice ID 会固定到对应 Provider，避免换声线。
- 现有 Tool 内部名仍包含 `*_minimax`，但图片、语音和视频已是平台托管的 provider-neutral route；名称是兼容债务，不是用户承诺。
- Agent Plan 代码支持 Small/Medium/Large/Max；Small 不具备视频，Medium 使用 Seedance 1.5 Pro，Large/Max 使用 Seedance 2.0。
- 先前行为级验证表明当前 Agent Plan Key 是 Small：文字、Seedream、TTS 可用，火山视频不可用。该事实不能被写成“火山视频已在当前账号可用”。

### 2.2 文字

- 当前迁移 `202607261500_seed_agent_plan_text_routes.py` 为 Lite/Pro/Ultra 建立更高优先级的 Agent Plan 文字路由，并把非 Agent Plan 文字路由作为 fallback。
- MiniMax-M3 也有受保护的三档文字/理解路由和运行能力校验。
- 因此当前实现与本次确认的目标存在差异：当前是 Agent Plan 文字优先；目标改为 MiniMax-M3 文字优先。

## 3. 已确认的目标 Provider 策略

| 能力 | Primary | Secondary/Fallback | 故障语义 |
|---|---|---|---|
| 文字推理与写作 | `MiniMax-M3` | 火山文字模型，仅在合同、工具调用和上下文能力兼容时 | 可以自动切换，但 Run 必须记录 route snapshot；不能在已有有效输出/副作用后普通 failover |
| 图片/海报生成 | 火山 Seedream | MiniMax 仅作为明确的 degraded/应急路线 | 已知质量差异不得伪装成等价；正式商业交付可选择等待火山恢复 |
| 视频生成 | 火山 Seedance | MiniMax 仅作为经场景验收的 degraded/快速路线 | 当前 Small 无火山视频；正式商业视频不得假装已可用，升级 Medium+ 并通过真实验证后开放 |
| 语音/TTS | 火山 TTS | MiniMax TTS | 默认声线可自动切；显式声线/品牌音色需要保持身份或重新确认 |
| 音乐 | MiniMax | 无 | 不伪造备用 Provider；不可用时保留 brief 并明确等待/失败 |
| PPT | Provider-neutral workflow | 文字规划走 M3，视觉走图片策略，必要配音/视频走各自策略，PPTX/PDF 由确定性工具生成 | Provider Skill 不是 PPT 质量替代品；版式、结构、可编辑性和 QA 独立治理 |

这张表是下一实施阶段的目标策略，不是当前部署证明。

## 4. 等价、降级与不可用

### `available`

替代路线在输出格式、质量门槛、时延等级、成本上限、授权和安全上满足同一工作合同，可以在 Provider 接受前自动选择。

### `degraded`

仍能产出，但质量、格式、时延、成本或身份一致性有实质差异。系统必须在付费执行前让用户理解差异；正式商业交付默认不静默降级。

### `unavailable`

没有满足合同的路线。系统应保存 brief、脚本、分镜、版式或素材清单，告诉用户缺少的能力和恢复条件，绝不能把占位图、静音视频、文字提纲或损坏文件称为完成。

“客户不能看出供应商切换”只适用于 `available` 的等价路线；当 MiniMax 图片/视频已知明显更差时，静默切换会直接破坏商用质量目标。

## 5. Skill 是否需要定制

### 图片 Skill

需要保留平台定制层，不能直接把火山官方 Skill 当完整产品：

- 将业务 brief 编译为 Provider prompt；
- 处理品牌、人物、产品、文字和 Logo 的参考素材；
- 精确文字/Logo 使用确定性合成，不依赖模型绘字；
- 候选生成、自动检查、人工选择和 revision；
- 处理画幅、尺寸、Credits、Provider 接受状态和 Artifact 登记。

### 视频 Skill

必须定制：

- 先完成脚本、分镜、镜头合同和角色/产品参考；
- 区分人物广告、产品展示、口播、剧情和信息流等任务；
- 每个镜头独立生成与重做；
- 后期剪辑、字幕、配音、音乐、音量、封装和 QA 独立于生成模型；
- 对 Small/Medium/Large/Max 与具体 Seedance 模型做服务端能力校验；
- 供应商任务已接受但响应未知时进入对账，禁止重复扣费。

### PPT Skill

PPT 的质量核心不是换一个生成模型，而是：

- 来源和事实清单；
- `DeckOutline` 与逐页 `SlideSpec`；
- 主题、网格、版式、多种页面结构；
- 图表/表格/形状可编辑；
- 图片按需要生成和裁切，不把所有页面做成整图；
- PPTX/PDF 一致性、溢出、字体、对齐、对比度和引用检查；
- 页级修改而非全量重做。

## 6. Agent 如何获得能力

Agent 只有同时满足以下条件才可执行：

1. 工作合同已注册并匹配用户任务；
2. Skill 在租户/平台作用域解析成功；
3. Tool 已注册、启用且对该 Agent 可见；
4. Agent 或临时专家拥有最小 grant；
5. 租户 Plan 允许 modality/tier；
6. Provider 账号池健康且对应套餐/模型可用；
7. Credits 可预留；
8. Autonomy 和 Approval 门禁允许；
9. 已创建持久 Task/Run/Deliverable 与幂等键；
10. 输出通过 Artifact 和质量合同。

拥有 Skill 不自动拥有 Tool；拥有 Tool 不自动拥有 Provider Key；Provider 可用不自动代表套餐允许；生成成功不自动代表可交付。

## 7. 路由与故障切换要求

- 路由由服务端选择并在 Run 接受时固定，前端不能提交任意 Provider/model。
- 同一 modality 的临时失败不能阻断其他 modality。
- 已被 Provider 接受或接受状态未知的付费任务只能对账，不能立即切 Provider 重发。
- 每次调用记录 tenant、user、agent/task/run、modality、tier、provider、model、Credits reservation 和 reason code；不记录 Key 和不必要的用户敏感输入。
- 路由变更需要版本化、可回滚、可审计，并在本地/预发布/生产分别验证。
- 正式商业工作流和“快速生成”可使用不同降级政策，但必须在产品合同中明确。

## 8. 下一实施阶段

1. 把 MiniMax-M3 提升为三档文字 Primary，Agent Plan 文字调整为兼容 fallback；增加迁移与 route-integrity 测试。
2. 不改变现有媒体顺序，但把 MiniMax 图片/视频从“默认等价 fallback”改为按工作合同判定的 degraded 路线。
3. 在 SaaS Admin 显示目标策略、当前就绪 Provider、套餐级能力和最后验证证据；普通用户只看到 `可用/降级/不可用`。
4. 当前 Small 账号先用于 Seedream 和 TTS；火山视频保持 unavailable。升级 Medium+ 后用一条受控真实任务验证，再开放对应路线。
5. 给 PPT、图片、视频、语音、音乐工作合同补齐 route policy、quality gate 和 fallback policy。

## 9. 完成标准

- 文字实际路由、迁移、SaaS 控制台和测试都证明 MiniMax-M3 Primary，而非只改文案。
- 图片/视频正式交付不会静默落到已知低质量路线。
- 当前账号没有的火山视频能力被正确显示为 unavailable，不产生假成功。
- Agent 的 Skill、Tool、权限、套餐、Provider、Credits 和质量证据可逐层审计。
- 普通用户只理解业务结果、费用、进度、质量和下一步，不需要理解 Provider 内部结构。
