# 抖音 Agent 自动化运营官方能力深度调研

更新时间：2026-07-09

## 结论

抖音官方开放平台可以支撑“抖音账号运营 Agent”的大部分运营能力，但不能把“后台代用户直接发布作品”作为通用 SaaS 默认承诺。

2026-07-09 重新核对官方能力中心后，当前更准确的结论是：

- 可以作为通用 SaaS 主路径：抖音 OAuth 扫码授权、账号绑定、视频数据、用户互动数据、评论列表、评论回复、授权/取消授权 Webhooks、基于 H5/SDK 的用户确认发布。
- 不能作为通用 SaaS 默认承诺：无感后台代用户直接发布视频/图文。当前官方“代替用户发布内容到抖音”能力页标注为 Beta，仅支持网站应用、自研调用方、不可用于沙盒，并把使用场景限制在政务或媒体机构内部多媒体管理平台；准入条件要求开发者主体为党政机关或事业单位。
- 需要控制台实测确认：旧版 `open.douyin.com` 迁移文档仍保留“OpenAPI 直接发布/视频发布及管理”的更宽泛描述，但当前能力中心和接口页都把创建视频/图文绑定到 `video.create.bind` 与“代替用户发布内容到抖音”能力实验室。生产设计必须按当前能力中心的限制执行，不能只按旧版文档乐观承诺。

目标定位已明确为：生产级、可给客户/员工长期使用的“抖音账号运营 Agent”。因此不能只靠一次性脚本、浏览器自动化、MCP 或 skill 来完成全部闭环；MCP/skill 可以承担 Agent 操作与策略层，但账号授权、token、审批、审计、任务状态、幂等和风控必须由 Clawith 后端控制面负责。

可走官方 OpenAPI 的确定性核心闭环是：

- 账号授权：通过抖音 OAuth 获取用户授权和 `access_token`。
- 内容发布：
  - 通用路径：Agent 生成素材、文案、发布包，用户通过 H5/SDK 唤起抖音并在抖音端确认发布。
  - 受限路径：只有在“代替用户发布内容到抖音”能力申请通过且主体/场景满足官方限制时，服务端才可上传视频/图片并创建视频或图文作品。
- 运营数据：可获取授权账号的粉丝、主页访问、视频列表、视频播放/点赞/评论/分享等数据。
- 评论运营：可获取视频评论、评论回复列表、回复评论；置顶评论仅企业号可用。
- 事件回调：可接收授权、取消授权、分享视频、企业号私信/留资等 Webhook 事件。

GitHub 调研后的工程判断：

- 官方/上游优先：抖音开放平台公开确认服务端 OpenAPI SDK 支持 Java、NodeJS、Go；当前未确认官方 Python SDK。Clawith 后端是 Python，因此第一版建议做轻量自研 HTTP client，而不是引入非官方 Python 包。
- 可借鉴但不采用为主线：GitHub 上有多套 Playwright/Cookie/创作者中心自动发布项目，部分项目产品形态成熟，但底层是浏览器自动化，不应作为 Clawith 抖音主路径。
- 不建议复用：爬虫/下载类项目、反检测脚本、Cookie 持久化发布脚本。这些能说明民间方案存在，但封号、失效和合规风险都高。

生产级推荐架构：

- Clawith 后端代码模块：做官方 OAuth、token 加密保存/刷新、账号绑定、任务状态机、审批、限流、幂等、审计、Webhook、指标快照。
- MCP/工具层：把后端受控能力暴露给 Agent，例如查询账号快照、创建发布任务、获取评论、执行已审批发布任务。
- Skill 层：沉淀运营 SOP，例如选题复盘、评论分级、发布前检查、日报/周报、什么时候必须请求人工审批。

边界判断：

- 内部 POC 可以用 skill + 外部 MCP/脚本先跑通。
- 生产级长期使用必须有 Clawith 后端控制面；否则无法稳定处理多客户账号、多员工权限、token 过期、误发防护、审计追责和平台能力变更。
- 对普通小白客户，不展示 OAuth 技术参数；只展示“连接抖音账号”“需要重新授权”“生成发布包”“打开抖音确认发布”“待审批任务”。
- 对平台/运维，只配置 Clawith 自有抖音开放平台应用；企业自带应用只作为大客户/私有化高级选项。

关键边界：

- 发布能力需要申请权限和用户授权，不是默认开放。
- “代替用户发布内容到抖音”当前不是通用 SaaS 能力；它有主体、行业、应用类型、授权触发和用户感知要求，并有平台审核、回查和能力收回风险。
- 多个数据和评论能力不可用于沙盒环境，真实验收需要正式应用和真实授权账号。
- 官方“投稿/分享”能力与“代替用户发布内容”不同：前者通常需要用户在抖音端确认发布；后者更接近后台自动发布。
- 普通账号私信等深度管理能力不应默认纳入 MVP，企业号相关 `enterprise.im` 能力需要单独确认当前开放状态和准入。
- 不建议把浏览器自动化作为抖音第一路径；第一路径应走官方 OpenAPI。浏览器自动化最多作为人工确认型临时兜底，不进入默认产品能力。

## 2026-07-09 官方能力确定性矩阵

| 能力 | 官方可用性 | 对 Clawith 的产品结论 |
| --- | --- | --- |
| OAuth 扫码授权 | 确认可用。网站应用通过授权码换取 `access_token`；`ClientKey/ClientSecret` 属于应用侧配置，不让普通用户接触。 | 主路径。用户只点“连接抖音账号”并扫码确认；Clawith 后端保存 token。 |
| Token 续期 | 确认可用。`access_token` 15 天，`refresh_token` 30 天；不合规授权会被取消，过期后需要重新授权。 | 必须有 token 刷新、失效状态、重新授权提醒。 |
| H5/SDK 用户确认发布 | 确认可用。H5 分享可生成 schema/二维码，用户在抖音编辑页或发布页确认发布；需要 `h5.share`、`aweme.share` / `aweme.forward` 等权限。 | 通用 SaaS 发布主路径。Agent 准备发布包，用户确认发布。 |
| OpenAPI 后台直接发布 | 受限可用。当前“代替用户发布内容到抖音”能力标注 Beta，仅网站应用、自研调用方、不可沙盒，当前准入偏政务/媒体机构内部平台。 | 不能默认卖给所有客户。仅作为专项客户/专项主体通过官方审核后的增强能力。 |
| 视频上传/图文上传 | 接口存在，但绑定 `video.create.bind` 和直接发布能力。 | 只有直接发布能力通过后才启用；否则不要在 UI 承诺“自动发布”。 |
| 视频列表与视频数据 | 确认可申请。支持授权账号视频列表、特定视频实时数据和近 30 天离线数据；不可沙盒。 | 运营问答和复盘核心能力。Agent 回答必须标注实时/T+1/近 30 天。 |
| 用户互动数据 | 确认可申请但准入较窄。仅企业、党政、事业单位类型开发者申请；不可用于对外售卖数据服务；首次授权后次日才完整产出。 | 可做账号自运营数据，不做泛平台数据售卖或竞品监控。 |
| 评论列表/回复 | 确认可申请。任一应用可申请，不支持沙盒；只能回复授权用户自己发布的视频。 | 第一版可做评论分诊、回复草稿、审批后回复。 |
| 私信/群聊/留资 | 官方 Webhooks 和 OpenAPI 存在，但当前多与企业号/私信经营关系能力相关。 | 不进普通版 MVP；企业号客户单独开通。 |
| Webhooks | 确认可用。包含授权、取消授权、经营关系授权、创建视频、私信/群聊等事件。 | 必须接入，用于同步授权状态、发布结果和企业号事件。 |
| 服务市场/服务商平台 | 确认存在，但主要面向 ISV、服务市场、小程序代开发、生活服务等场景。 | 可作为未来商业分发/服务商形态研究，不等于普通 OAuth 发布能力。 |

## 对 Clawith 产品宗旨的重新定位

Clawith 不是“替所有企业老板无感接管抖音后台”的工具，而是“数字员工协作运营系统”。因此抖音 Agent 的正确产品承诺应分层：

1. **基础版，确定可落地**：连接抖音账号，读取授权范围内的数据，生成选题、脚本、标题、封面建议、评论回复草稿、日报周报和发布包。
2. **协作发布版，通用 SaaS 主推**：Agent 生成发布包，用户点击“打开抖音确认发布”或扫码，在抖音端完成最终发布；Clawith 记录任务、素材、文案、审批和后续数据。
3. **审批执行版，需官方专项能力**：只有客户主体、应用场景和能力审核满足“代替用户发布内容到抖音”限制时，才开放后台上传并创建视频/图文。
4. **企业号运营版，后续阶段**：企业号私信、留资、群聊、线索跟进等能力单独做企业号能力包。

小白创始人的默认体验应是：

```text
创建抖音运营 Agent
  -> 连接抖音账号，扫码授权
  -> Agent 读取数据并给建议
  -> Agent 生成发布包/评论回复草稿
  -> 用户确认或审批
  -> 若无后台发布权限：打开抖音确认发布
  -> 若有专项后台发布权限：Clawith 审批后调用 OpenAPI
  -> 指标和评论回流，Agent 复盘
```

## 源边界与“工具/sidecar”澄清

本调研只针对当前 Clawith 工作区：`/Users/sun/Documents/PythonProject/Clawith`。

此前工作区里曾混入一套 `backend/app/services/social_platform` 抽象，以及小红书方向的 `xiaohongshu_mcp_http` / sidecar 连接器代码；这批代码已确认不需要并清理。这里提到的 “sidecar” 是 Clawith 历史本地代码里对小红书浏览器自动化连接器的实现形态，不是抖音官方提供的能力，也不应直接套用到抖音。

抖音官方目前能确认的正规接入形态是：

- OAuth 授权。
- OpenAPI。
- 抖音开放能力 SDK。
- 抖音 SDK。
- 能力实验室里的“代替用户发布内容到抖音”。

抖音开放平台文档导航中出现了“MCP服务广场”，但公开文档中暂未确认它提供“后台代用户自动发布抖音作品”的官方 MCP，也未确认它提供浏览器自动化式 sidecar。第三方文章提到过从开放平台侧开通 MCP 服务，例如抖音视频搜索、图文搜索、跳转链接等，但这不是官方文档证据，必须以后续登录抖音开放平台控制台实际查看为准。

因此，当前判断应改为：

- 官方已确认：通过 OpenAPI/SDK 做授权、数据、评论、发布接口。
- 官方待确认：控制台里的 MCP服务广场是否有可用于运营管理的官方工具组，以及具体能否覆盖发布。
- 不建议默认采用：非官方浏览器自动化 sidecar，尤其是无人值守发布；这类路径封号和失效风险都高。

## 官方能力拆解

### 1. 授权与账号连接

抖音登录与授权基于 OAuth 2.0。网站应用可以展示抖音登录授权二维码，用户用抖音 App 扫码并确认后，应用获得授权码，再换取接口调用凭证。

重要约束：

- 大多数能力需要先在开放平台申请对应 `scope`。
- 用户授权通过后，才能通过 `access_token` 调用相关接口。
- `access_token` 有效期为 15 天，`refresh_token` 有效期为 30 天。
- 授权规范要求场景合理、用户有明确预期，不能诱导授权，不能一次性捆绑申请过多授权项。

产品含义：

- Clawith 应把抖音账号接入做成独立的 `DouyinAccount` + `DouyinToken` OAuth 连接流程。
- 不应存储抖音账号密码、短信码、Cookie 或浏览器本地存储。
- 每个抖音账号需要独立授权、独立 token、独立能力范围。

### 2. 内容发布

官方有两类发布相关能力。

第一类是“投稿/分享”能力：

- `aweme.share`：发布内容至抖音。
- `aweme.forward`：转发到日常。
- `h5.share`：H5 场景，生成 schema/二维码或从移动端 H5 唤起抖音。
- `open.get.ticket` / `client_token`：H5 分享签名链路需要服务端获取并缓存。

这类能力适合通用 SaaS：Agent 准备内容和参数，用户在抖音编辑页或发布页完成最终确认。它不是后台无人值守发布，但对小白创始人是最符合认知和合规边界的“扫码/打开抖音确认发布”。

第二类是“代替用户发布内容到抖音”能力：

- 能力中心显示为 Beta。
- 适用于网站应用。
- 支持的调用方当前标注为自研。
- 当前能力页标注不可用于沙盒环境。
- 当前使用场景限制为政务或媒体机构内部多媒体管理平台，且不可对外面向 C 端用户使用。
- 当前准入条件要求开发者主体为党政机关或事业单位，应用类型为正式网站应用，并符合平台支持的使用场景。
- 需要在“能力实验室”申请。
- 平台审核时间约 7 个工作日。
- 审核通过后，开发者可通过接口上传图片/视频并创建作品。
- 官方要求授权和发布环节都让用户有明确感知，包括用途、频次、当前绑定账号、发布内容信息，并支持平台回查。
- 官方说明开通后若滥用，平台可收回能力并处罚。

已确认的接口面：

- 上传视频：`POST https://open.douyin.com/api/douyin/v1/video/upload_video/`
- 分片上传初始化/上传/完成：适合大视频。
- 创建视频：`POST https://open.douyin.com/api/douyin/v1/video/create_video/`
- 上传图片：`POST https://open.douyin.com/api/douyin/v1/video/upload_image/`
- 创建图文：`POST https://open.douyin.com/api/douyin/v1/video/create_image_text/`

关键参数和限制：

- 发布 scope：`video.create.bind`。
- 创建视频/图文都需要用户授权。
- 创建后有审核过程，审核期间只有自己可见。
- 视频上传建议超过 50MB 使用分片，超过 300MB 必须分片，总大小 4GB 以内；视频时长不超过 15 分钟。
- 图文每次最多 30 张图片，单张图片最大 20MB。
- 图片上传接口文档标注图片大小不超过 300MB。
- `private_status` 可控制可见范围：公开、自见、好友可见。
- 标题/文案可以带话题和 @ 用户，但话题仍按抖音审核逻辑处理，强导流风险需要产品侧审核。

产品含义：

- “Agent 员工生成视频、图片、文案后自动发到抖音”不能作为通用 SaaS 默认承诺。通用客户默认只能做到“Agent 生成发布包，用户确认发布”；后台直接发布只在专项主体和专项场景审核通过后启用。
- Clawith 应新增 `douyin_openapi` adapter，而不是复用小红书浏览器 sidecar。
- 内部排期发布有两种状态：通用路径到点提醒用户打开抖音确认发布；专项后台发布路径由 Clawith job 在指定时间调用 OpenAPI。当前资料未确认抖音官方 OpenAPI 自带“定时发布”参数，标记为待确认。

### 3. 运营数据与账号问答

官方“用户互动数据”能力可以在用户授权下获取账号数据，包括：

- 粉丝总数。
- 每日新增粉丝数。
- 每日个人主页访问人数。
- 近一段时间发布作品总数。
- 每日发布作品数。
- 每日新增播放、点赞、评论、分享数。

限制：

- 仅限企业、党政和事业单位类型开发者申请。
- 审核约 2-3 个工作日。
- 不可用于开发对外售卖的数据服务型应用或进行平台统计分析。
- 用户首次授权后，需要第二天才会产生全部数据。
- 不支持沙盒环境。

官方“视频数据”能力支持：

- 查询授权账号视频列表数据，分页获取用户所有视频，实时返回。
- 查询特定视频数据，如点赞数、播放数等，实时返回。
- 查询视频来源端。
- 查询视频携带的 POI 信息。
- 离线数据：获取视频基础、点赞、评论、播放、分享数据；文档标注三十天内创建的视频才会返回数据。

产品含义：

- “随时询问 Agent 员工拿到账号运营信息”可以落地为指标快照 + Agent RAG/工具问答。
- 数据能力需要区分实时接口和 T+1/近 30 天离线接口。
- 不应承诺全平台竞品分析或售卖型数据服务；官方文档对数据服务型使用有明确限制。

### 4. 评论与互动管理

官方“视频评论管理”能力支持：

- 批量获取抖音视频评论内容。
- 回复评论。
- 统一查阅、监控、回复和复盘评论。
- 任一应用均可申请，移动/网站应用只能在页面申请。
- 不支持沙盒环境。
- 审核约 2-3 个工作日。

旧版/迁移文档中的评论接入方案还列出了：

- `GET /video/comment/list/`：评论列表。
- `GET /video/comment/reply/list/`：评论回复列表。
- `POST /video/comment/reply/`：回复视频评论。
- `POST /video/comment/top/`：置顶视频评论，仅企业号可用。
- Scope：`video.comment`。

产品含义：

- 评论抓取、评论聚合、评论回复和复盘分析可以纳入第一版运营闭环。
- 回复评论是写操作，必须继续保留 Clawith 的审批、限流、幂等、审计和敏感内容拦截。
- 评论置顶只有企业号可用，不应作为普通账号默认功能。

### 5. 私信、留资和企业号能力

官方 Webhooks 事件列表包含：

- `authorize` / `unauthorize`：授权和取消授权。
- `create_video`：用户使用开发者应用分享视频到抖音。
- `receive_msg`、`enter_im`、`dial_phone`、`website_contact`、`personal_tab_contact`：需要 `enterprise.im`，且文档说明 IM 事件要求当前抖音用户是企业号。

角色文档也说明企业号认证开发者可获得企业号开放能力。

产品含义：

- 私信自动回复、线索跟进、企业号客户运营属于第二阶段或企业号专版能力。
- MVP 不应默认承诺普通号私信自动化。
- 是否还能新开通移动/网站应用私信能力，需要以当前开放平台控制台审核结果为准，标记为待确认。

## GitHub 项目调研

### 官方或上游优先项目

#### `bytedance/douyin-openapi-sdk-go`

结论：可作为官方 SDK 参考，不直接引入 Clawith。

要点：

- GitHub 组织为 `bytedance`。
- README 明确是“抖音开放平台 OpenAPI SDK 的 Go 语言实现”。
- License 为 Apache-2.0。
- 官方文档同时说明移动/网站应用 OpenAPI SDK 支持 Java、NodeJS、Go，并提示 Token 需要开发者注入。

落地影响：

- Clawith 后端是 Python，不适合引入 Go SDK 作为运行时依赖。
- 适合作为接口命名、模型结构、错误处理的对照参考。
- 第一版 Python 侧应基于 `httpx.AsyncClient` 做薄封装，严格跟随官方文档的 endpoint、headers、form/json/multipart 要求。

#### 官方服务端 OpenAPI SDK

结论：官方确认有服务端 SDK，但当前公开页面未确认 Python。

官方文档显示：

- 支持语言：Java、NodeJS、Go。
- SDK 简化 OpenAPI 调用。
- Token 参数需要用户自己注入。

落地影响：

- 如果后续 Clawith 有 Node sidecar/worker，可以考虑把 Douyin SDK 放到 Node worker；但当前最小落地不建议为了 SDK 新增跨语言运行时。
- Python 方案要保留“可替换 adapter”边界，未来如果官方 Python SDK 出现，可以替换底层 client，不改业务状态机。

### 第三方 SDK / 平台抽象

#### `fudiwei/DotNetCore.SKIT.FlurlHttpClient.ByteDance`

结论：成熟度较高，但技术栈不匹配，只做接口设计参考。

要点：

- .NET SDK，覆盖抖音开放平台、抖音小程序、TikTok、TikTok Shop 等。
- README 标注强类型模型、全异步、多平台部署。
- GitHub 页面显示约 307 stars、66 forks。

落地影响：

- 不能直接被 Python 后端复用。
- 可参考其强类型模型、模块拆分、错误模型，但不能作为 Clawith 运行依赖。

#### `uimeet/douyin`

结论：不建议采用。

要点：

- GitHub 页面标题称“抖音 OpenAPI 官方 SDK for python”，但仓库不在 `bytedance` 组织。
- README 显示由 Swagger Codegen 生成，并保留 `GIT_USER_ID/GIT_REPO_ID` 这类模板占位。
- License 为 GPL-3.0，与商业闭源/混合授权项目兼容性存在风险。

落地影响：

- 不 vendoring，不作为依赖。
- 最多作为旧接口名称参考。

#### `ArtisanCloud/MediaX`

结论：可参考多平台抽象，不引入依赖。

要点：

- Go 项目，定位为多媒体平台 SDK/接口封装。
- README 的功能矩阵列出字节/抖音支持图文、视频、素材管理、评论管理、数据管理。

落地影响：

- 可借鉴“平台能力矩阵”和统一能力层命名。
- 但 Clawith 这次应只落 `douyin_openapi`，避免重新引入大而泛的 `social_platform` 抽象。

### 浏览器自动化 / MCP / 创作者中心方案

这类项目能证明“民间可以做”，但不应成为 Clawith 主线。

#### `dreammis/social-auto-upload`

结论：产品形态成熟，可参考任务模型和 UI，不采用底层自动化为主线。

要点：

- GitHub 页面显示约 13.1k stars、2.3k forks。
- 架构是 Flask 后端 + Vue 前端 + SQLite。
- 功能包括多平台账号管理、视频文件管理、发布、定时发布。
- 明确使用 Playwright 与平台页面交互。
- CLI 提供 `sau douyin login/check/upload`。

可借鉴：

- 发布任务状态机。
- 多账号管理 UI。
- 登录状态检查。
- SSE/实时进度反馈。

不能直接采用：

- Cookie 登录态。
- Playwright 对创作者中心的发布自动化。

#### `flyerhzm/douyin-mcp`

结论：可作为 MCP 形态参考，不用于生产主线。

要点：

- 基于 Playwright 模拟浏览器操作。
- 通过抖音 App 扫码登录，保存 cookies。
- 提供 HTTP API：登录状态、二维码、发布视频。
- README 明确仅供学习和个人使用。

落地影响：

- 可参考“Agent 工具调用 HTTP service”的接口形态。
- 不能作为 Clawith 生产发布能力，因为它依赖 Cookie 和浏览器页面稳定性。

#### `DaBaoAgent/douyin-auto-publish`

结论：不采用，只作为风险样本。

要点：

- Playwright + CDP 驱动 Chrome。
- README 强调绕过反爬检测、随机间隔、拟人打字、鼠标模拟、自动点击发布。
- 支持 cron 定时发布。

落地影响：

- 这些反检测策略正说明其合规与稳定性风险较高。
- Clawith 不应引入“绕过检测”叙事或实现。

#### `LouisLin0723/social-auto-publisher`

结论：仅借鉴“human-confirmation gate”。

要点：

- Playwright + Chrome MCP 多平台发布。
- README 明确发布不可逆，脚本填完后等待人工确认再点击发布。

落地影响：

- 如果官方发布能力申请失败，临时兜底只能做“填表 + 人工确认”，不能做无人值守发布。
- 该路径必须作为实验/人工工具，不进入默认 Agent 自动执行链路。

#### `Evil0ctal/Douyin_TikTok_Download_API`

结论：不用于 Clawith 抖音运营自动化主线。

要点：

- 高 star 项目，定位是抖音/TikTok 数据爬取、解析、下载。
- README 提到 Web API、Cookie、`X-Bogus` / `A_Bogus` 等。

落地影响：

- 不解决官方发布、授权、评论回复。
- 不符合账号自运营合规边界，不作为依赖或架构参考。

## Clawith 落地方案

### 当前仓库事实

已清理小红书/sidecar 混入模块后，当前 Clawith 不再保留通用 `social_platform` 控制面。

现有可复用基础能力：

- FastAPI 路由注册：`backend/app/main.py`
- Agent 工具定义与执行：`backend/app/services/agent_tools.py`
- 内置工具入库：`backend/app/services/tool_seeder.py`
- Agent 级权限检查：`check_agent_access`
- 审计日志：`backend/app/services/audit_logger.py`
- 后台任务：现有 startup background task 模式
- Webhook 路由样例：`backend/app/api/webhooks.py`
- 现有 `agent_credentials` 是浏览器 Cookie 凭证模型，不适合作为抖音 OAuth token 主存储

### 总体架构

```text
Agent 员工
  -> Douyin builtin tools
  -> Douyin policy gates / approval gates
  -> Douyin application service
  -> DouyinOpenApiClient
  -> 抖音 OpenAPI
```

第一版只新增抖音官方能力模块，不恢复大而泛的 `social_platform` 抽象。

建议目录：

```text
backend/app/api/douyin.py
backend/app/models/douyin.py
backend/app/schemas/douyin.py
backend/app/services/douyin/
  __init__.py
  client.py
  auth.py
  token_store.py
  publish.py
  metrics.py
  comments.py
  policy.py
  planning.py
  webhooks.py
backend/tests/test_douyin_*.py
```

### 数据模型

建议新增独立表：

- `douyin_accounts`
  - `id`
  - `tenant_id`
  - `agent_id`
  - `open_id`
  - `union_id`
  - `nickname`
  - `avatar_url`
  - `account_type`
  - `scopes`
  - `status`: `active | needs_reauth | revoked | disabled`
  - `authorized_at`
  - `last_sync_at`

- `douyin_tokens`
  - `id`
  - `account_id`
  - `access_token_encrypted`
  - `refresh_token_encrypted`
  - `access_token_expires_at`
  - `refresh_token_expires_at`
  - `refresh_count`
  - `last_refresh_at`
  - `status`

- `douyin_publish_jobs`
  - `id`
  - `tenant_id`
  - `agent_id`
  - `account_id`
  - `content_type`: `video | image_text`
  - `title`
  - `body`
  - `hashtags`
  - `visibility`
  - `asset_refs`
  - `idempotency_key`
  - `approval_status`
  - `status`: `draft | approval_required | approved | uploading | uploaded | creating | created_reviewing | published_unverified | metrics_synced | failed | blocked | needs_reauth`
  - `external_item_id`
  - `external_video_id`
  - `external_image_ids`
  - `official_error_code`
  - `official_log_id`
  - `redacted_request_summary`
  - `created_at`
  - `scheduled_at`
  - `published_at`

- `douyin_metric_snapshots`
  - `id`
  - `account_id`
  - `external_item_id`
  - `metric_type`: `account | video`
  - `source_api`
  - `data_freshness`: `realtime | t_plus_1 | offline_30d`
  - `metrics_json`
  - `captured_at`

- `douyin_comments`
  - `id`
  - `account_id`
  - `external_item_id`
  - `comment_id`
  - `parent_comment_id`
  - `content`
  - `sentiment`
  - `intent`
  - `risk_level`
  - `needs_reply`
  - `last_seen_at`

- `douyin_operations`
  - `id`
  - `tenant_id`
  - `agent_id`
  - `account_id`
  - `operation_type`
  - `idempotency_key`
  - `approval_required`
  - `approval_status`
  - `status`
  - `request_summary`
  - `response_summary`
  - `created_at`
  - `finished_at`

### API 路由

建议新增：

- `GET /api/douyin/accounts`
- `POST /api/douyin/oauth/start`
- `GET /api/douyin/oauth/callback`
- `POST /api/douyin/accounts/{account_id}/sync`
- `GET /api/douyin/accounts/{account_id}/metrics`
- `GET /api/douyin/accounts/{account_id}/videos`
- `GET /api/douyin/videos/{item_id}/comments`
- `POST /api/douyin/publish-jobs`
- `POST /api/douyin/publish-jobs/{job_id}/approve`
- `POST /api/douyin/publish-jobs/{job_id}/run`
- `GET /api/douyin/publish-jobs/{job_id}`
- `POST /api/douyin/comments/{comment_id}/reply`
- `POST /api/douyin/webhooks`

### Agent 工具

建议通过 `AGENT_TOOLS` + `BUILTIN_TOOLS` 新增低风险工具，默认只启用只读工具：

- `douyin_account_snapshot`
  - 输入：`account_id`
  - 输出：账号数据、同步时间、数据新鲜度
  - 默认启用

- `douyin_video_metrics`
  - 输入：`account_id`、`date_range`、`video_id?`
  - 输出：视频指标
  - 默认启用

- `douyin_fetch_comments`
  - 输入：`item_id`
  - 输出：评论列表和风险分类
  - 默认启用

- `douyin_create_publish_job`
  - 输入：标题、文案、素材引用、可见性、计划时间
  - 输出：发布任务，不直接发布
  - 默认启用，但只创建任务

- `douyin_run_publish_job`
  - 输入：`job_id`
  - 输出：发布执行结果
  - 默认不启用；需要管理员开启，并受审批/限流控制

- `douyin_reply_comment`
  - 输入：`comment_id`、回复内容
  - 输出：回复结果
  - 默认不启用；高风险评论强制审批

- `douyin_make_operation_plan`
  - 输入：账号指标、评论摘要、业务目标
  - 输出：运营计划
  - 不写抖音，只生成计划

### 策略门禁

必须强制：

- 不保存抖音账号密码、短信码、Cookie、浏览器本地存储。
- OAuth token 服务端加密保存，API 响应不返回明文 token。
- 发布前必须有用户可感知动作。官方旧版创建视频文档明确提示，代用户创建视频除授权外，每次调用都需要在产品设计中让用户明确感知相关操作。
- 素材权属未确认，禁止发布。
- 写操作不自动重试；上传分片可按官方建议重试，但创建视频/图文、回复评论必须幂等保护。
- 同一 `idempotency_key` 不重复创建作品或重复回复。
- 发布任务有日上限、账号级最小间隔、全局暂停开关。
- 所有外部请求记录只存脱敏摘要、官方错误码和 log id。
- Agent 回答必须标注数据新鲜度：实时、T+1、近 30 天离线。

### 状态机

发布任务状态：

```text
draft
  -> approval_required
  -> approved
  -> uploading
  -> uploaded
  -> creating
  -> created_reviewing
  -> published_unverified
  -> metrics_synced
```

失败/阻断状态：

```text
blocked
failed
needs_reauth
revoked
rate_limited
permission_missing
```

注意：

- `create_video` / `create_image_text` 成功不等于公开成功。
- 审核中、自己可见、公开可见必须分开展示。
- 没拿到可验证外链时，不要告诉用户“已公开发布成功”，只能说“已创建，等待审核/待验证”。

## 界面产品设计

### 设计目标

抖音运营 Agent 的产品目标不是再造一个复杂的“抖音运营后台”，而是让 Clawith 里的 Agent 员工接管一部分抖音运营工作。用户应该感知到的是“我雇了一个抖音运营 Agent，并把抖音账号交给它协作运营”，而不是“我在配置一套开发者平台、MCP 和工具系统”。

界面目标：

- 小白用户可以先从 `智能体+` 创建抖音运营 Agent，再在 3-5 步内完成账号连接。
- 默认是人和 Agent 协作：Agent 提供数据、计划、草稿和任务；发布、评论回复等高风险写操作默认人工审批。
- 自动化程度可以逐步提高，但不是默认全自动。第一版默认 `审批后执行`。
- 用户不直接面对 `scope`、`MCP`、`tool name`、`adapter_kind`、`client_secret` 等工程概念。
- 所有复杂状态都收敛成用户能理解的任务卡：待确认、执行中、抖音审核中、待验证、失败。

界面风格应延续当前 Clawith 的后台工具风格：暗色/浅色 token、紧凑信息密度、8px 内卡片圆角、状态徽标、表格、队列、时间线和右侧详情抽屉；不做营销 hero，不做大面积装饰卡片。

### 信息架构

第一版不新增一个庞大的“抖音运营工作台”作为一级产品中心。否则会把 Clawith 从“Agent 员工平台”拖成一个垂直社媒 SaaS。第一版应围绕现有 Agent 体验做轻量扩展：

```text
智能体+：选择“抖音运营 Agent”
  -> 创建时初始化运营 SOP 和默认安全策略
  -> 创建完成后引导连接抖音账号
  -> 连接账号默认绑定到该 Agent
  -> 在 Agent 聊天里运营
  -> 发布/回复任务进入现有审批体系
```

#### 1. 智能体+：预设“抖音运营 Agent”

这是更直接的首选入口。用户不是先理解“哪个 Agent 负责抖音”，而是在 `智能体+` / `Talent Market` 里直接选择一个已经定义好职责的 Agent。

建议在 `marketing` 分类下增加模板：

- 模板名：`抖音运营助手` 或 `抖音运营经理`。如果产品语气偏员工化，建议用 `抖音运营经理`。
- 模板描述：负责抖音账号数据解读、内容选题、发布计划、评论处理和运营复盘。
- 默认工作方式：`审批后执行`。
- 默认能力：
  - 查看账号和作品数据。
  - 生成选题、脚本、标题和发布计划。
  - 根据已有视频/图片/文案创建待审批发布任务。
  - 分析评论，生成回复草稿。
  - 输出日报、周报和下一步运营建议。
- 默认限制：
  - 不自动公开发布。
  - 不自动回复高风险评论。
  - 发布和回复默认进入审批。
  - 无账号授权时只能做内容建议和计划，不能调用抖音接口。

创建体验：

- 用户在 `智能体+` 选择 `抖音运营经理`。
- `PostHireSettingsModal` 不要求用户理解工具配置，只展示 2 个简单选择：
  - `接管程度`：默认 `审批后执行`。
  - `连接抖音账号`：`现在连接` / `稍后连接`。
- 点击 `现在连接` 后进入 OAuth 授权。
- 授权成功后，系统把该抖音账号默认绑定到刚创建的 Agent。
- 如果用户点击 `稍后连接`，进入 Agent 聊天时展示一个设置卡：`还没有连接抖音账号，连接后我可以读取数据并创建发布任务`。

这样做的好处：

- 用户一开始看到的是“我雇了一个抖音运营员工”，不是“我在配置平台能力”。
- Agent 的人设、SOP、审批策略、默认工具可以在模板里一次性初始化。
- 企业设置仍然存在，但它变成账号管理入口，而不是小白用户的第一入口。

落到当前代码结构，优先新增一个 folder template：

```text
backend/agent_templates/douyin-operator/
  meta.yaml
  soul.md
  bootstrap.md
```

`meta.yaml` 进入 `marketing` 分类；`soul.md` 写清楚运营职责、风险边界和审批优先；`bootstrap.md` 用来初始化首条引导消息和“连接抖音账号”动作。前端上，`TalentMarketModal` 负责展示模板，`PostHireSettingsModal` 负责在创建后给出连接抖音账号的下一步。

#### 2. 企业设置：`Enterprise Settings -> 连接账号`

用途：企业管理员连接抖音账号，并决定哪个 Agent 负责。

建议不要一开始叫“抖音运营”，可以叫更通用的 `连接账号` 或 `外部账号`，其中抖音只是一个平台卡片。

页面结构：

- 平台卡片：`抖音`
  - 状态：未连接 / 已连接 / 需要重新授权 / 权限不足。
  - 主按钮：`连接抖音账号`。
  - 次按钮：`查看已连接账号`。

- 连接向导
  - 第一步：连接账号。用户点击 OAuth 授权，不输入开发者密钥。
  - 第二步：确认负责的 Agent。若用户从 `抖音运营经理` 模板进入，默认选中新创建的 Agent；若用户先从企业设置连接账号，则推荐创建该模板 Agent 或选择已有 Agent。
  - 第三步：选择接管程度。
    - `建议模式`：只分析数据、生成计划和草稿。
    - `审批后执行`：Agent 可以创建发布/回复任务，人工批准后执行。第一版默认。
    - `低风险自动`：只允许明确低风险动作自动执行，需管理员开启。
  - 第四步：完成。给用户一个明确入口：`开始问 Agent`。

已连接账号列表只展示小白能理解的信息：

- 抖音昵称/头像。
- 授权状态。
- 可用能力：看数据、发作品、看评论、回复评论。
- 负责 Agent。
- 今日安全额度：可发布数、可回复数。

高级信息如 `open_id`、`scope`、token 过期时间、接口错误码放到“高级详情”里，默认折叠。

这里不使用 `AgentCredentials` 的 Cookie 凭证界面。`AgentCredentials` 当前是浏览器 Cookie 注入方向，抖音 OpenAPI 必须走独立 OAuth 账号模型。

#### 3. Agent 页面：不做复杂 `Agent Detail -> 抖音` 配置中心

上一版说新增 `Agent Detail -> 抖音` tab，确实会让小白困惑。校正后，Agent 页面只需要一个业务化的“负责账号/工作范围”区域，不展示工具名。

通过 `智能体+` 模板创建后，这个 Agent 已经带有抖音运营身份、SOP、默认审批策略和默认工具边界。Agent 页面不再让用户重新配置一堆能力，而是让用户确认“它负责哪个账号、能做哪些事、当前是否暂停执行”。

放置位置：

- Agent 创建成功后的 `PostHireSettingsModal`。
- Agent 详情里的 `Settings` 或 `Tools` 附近增加一个轻量区块：`负责的外部账号`。

用户看到的内容：

- 这个 Agent 负责哪个抖音账号。
- 它能做什么：
  - 查看运营数据。
  - 生成选题和发布计划。
  - 创建待审批发布任务。
  - 生成评论回复草稿。
  - 审批后执行发布/回复。
- 它今天还能做多少：
  - 今日发布任务额度。
  - 今日评论回复额度。
  - 当前是否暂停执行。

用户不应该看到：

- `douyin_account_snapshot`
- `douyin_run_publish_job`
- `adapter_kind`
- `scope`
- MCP server 配置
- OAuth token 细节

这些仍然存在于后端和工具层，但 UI 用业务语言包装。

#### 4. Agent 聊天：主要工作入口

对小白用户，最自然的入口应该是直接问 Agent：

- `今天这个抖音号怎么样？`
- `帮我看下哪些视频表现最好。`
- `根据最近评论，给我下周选题。`
- `把这条视频安排到明天上午发布，先给我确认。`
- `这些负面评论先生成回复草稿。`

Agent 回复不只是一段话，而是带结构化动作卡：

- 账号数据卡。
- 视频表现卡。
- 发布任务卡。
- 评论回复草稿卡。
- 待审批卡。

用户从聊天里点 `确认发布`、`提交审批`、`编辑草稿`、`生成计划`，而不是跑到一个复杂工作台找入口。

#### 5. 任务/审批：复用现有审批体系

发布和评论回复不应该另做一套审批中心。它们进入现有 `Approvals` / 通知 / Agent 活动流。

任务卡展示：

- Agent 要做什么。
- 用哪个抖音账号。
- 发布/回复内容预览。
- 风险检查结果。
- 素材权属确认。
- 执行后可能发生什么。

按钮：

- `批准执行`
- `退回修改`
- `拒绝`

审批通过后，后端执行官方 OpenAPI。

#### 6. 轻量账号详情页：不是大工作台

第一版可以有一个轻量的账号详情页，但它不是新的主入口，也不叫“大工作台”。

入口：

- 企业设置的账号列表。
- Agent 回复中的账号卡。

内容：

- 账号状态。
- 最近同步时间。
- 最近作品指标。
- 待审批任务。
- 最近评论摘要。
- 最近执行记录。

它是“查看和排障页面”，不是运营人员每天必须打开的大型工作台。

### SaaS 平台配置的校正

上一版把 `SaaS Admin / 平台能力` 写成了一个产品入口，这不准确。

抖音开放平台应用确实需要 `client_key` / `client_secret`，但它有两种部署形态：

- Clawith 托管模式：Clawith 平台统一持有抖音开放平台应用。普通企业用户不配置平台应用，只通过 OAuth 连接自己的抖音账号。
- 企业自带应用模式：大型客户或私有化客户使用自己的抖音开放平台应用。这个配置放在企业设置的“高级设置 / 使用自己的抖音开放平台应用”里，默认隐藏。

因此：

- 普通 SaaS 用户不应该看到“SaaS 平台配置”。
- 平台级配置是部署/运维配置，不是核心产品界面。
- 企业差异主要体现在“连接了哪些抖音账号、由哪个 Agent 负责、接管程度如何、审批策略如何”，不是每个公司都要配置一套开发者密钥。

### 人和 Agent 的关系

第一版默认是“Agent 协作运营”，不是“Agent 全自动运营”。

建议用一个小白能理解的三档模式：

| 模式 | 用户理解 | Agent 能做 | 默认 |
| --- | --- | --- | --- |
| 建议模式 | 只让 Agent 分析和出主意 | 看数据、生成计划、写草稿 | 可选 |
| 审批后执行 | Agent 干活前先让我确认 | 创建发布/回复任务，审批后执行 | 默认 |
| 低风险自动 | 明确安全的事可以自动做 | 低风险评论/固定计划可自动执行 | 高级 |

不要使用“全自动”作为第一版默认模式。即使未来支持，也应该叫“自动执行低风险任务”，并绑定日上限、暂停开关、审计和回滚说明。

### 工作流呈现

#### 发布任务时间线

每个发布任务详情抽屉显示不可折叠的关键状态线：

```text
Agent 生成内容
  -> 风险检查
  -> 素材权属确认
  -> 人工审批
  -> 上传素材
  -> 创建视频/图文
  -> 抖音审核中
  -> 公开状态待验证
  -> 指标回流
```

每个节点展示：

- 当前状态：未开始 / 进行中 / 已完成 / 失败 / 被阻断。
- 执行人或执行者：Agent、审批人、系统。
- 时间戳。
- 官方错误码和 log id。
- 脱敏请求摘要。
- 可重试/不可重试提示。

关键文案：

- 创建成功：`已提交抖音，等待平台审核`
- 待验证：`已创建作品，但公开可见状态待确认`
- 失败：`未发布，未消耗幂等键` 或 `创建请求已发出，请先回查再重试`

#### 评论处理时间线

评论详情显示：

```text
评论同步
  -> Agent 分类
  -> 回复草稿
  -> 风险检查
  -> 审批/人工编辑
  -> 回复发送
  -> 结果回写
```

高风险评论不显示“自动回复”按钮，只显示“生成草稿”和“提交审批”。

#### Agent 问答中的工作流呈现

Agent 聊天窗口不应该只输出自然语言结论。回答抖音运营问题时，消息内嵌结构化引用：

- 使用了哪个抖音账号。
- 数据同步时间。
- 数据来源接口。
- 数据新鲜度。
- 相关作品/评论链接。
- 下一步可执行动作：创建发布任务、生成评论草稿、生成周计划。

这样用户知道 Agent 不是凭空判断。

### 配置归属

| 配置项 | 位置 | 角色 |
| --- | --- | --- |
| Clawith 托管应用密钥 | 部署/运维配置，普通用户不可见 | platform_admin / ops |
| 企业自带抖音开放平台应用 | Enterprise Settings -> 连接账号 -> 高级设置 | org_admin |
| 企业抖音账号 OAuth 连接 | Enterprise Settings -> 连接账号 | org_admin |
| 负责该账号的 Agent | 智能体+ 模板创建时默认生成；连接账号向导中可改 | org_admin / agent manager |
| 接管程度 | 连接账号向导 | org_admin / agent manager |
| 发布/回复审批策略 | 连接账号向导里的简化选项，详细项默认折叠 | org_admin |
| 数据同步策略 | 高级设置，默认自动 | org_admin |
| 发布任务审批 | Agent 聊天动作卡 + Approvals | approver |
| 评论处理 | Agent 聊天动作卡 + 轻量账号详情 | operator / approver |
| 运营计划 | Agent chat | operator |

### 首版页面优先级

第一版页面优先级应进一步收敛：

1. `智能体+ -> 抖音运营 Agent 模板`
2. `PostHireSettingsModal -> 连接抖音账号 / 稍后连接`
3. `Enterprise Settings -> 连接账号 -> 抖音账号管理`
4. `Agent chat -> 抖音数据卡、发布任务卡、审批动作卡`
5. `Approvals -> 抖音发布/回复审批`
6. `轻量账号详情页 -> 状态、最近作品、最近评论、执行记录`

暂不做大型 `抖音运营工作台`。等出现多账号、多 Agent、多运营人员协同的真实复杂度后，再把轻量账号详情扩展成运营中心。

## MVP 分阶段落地

### 阶段 0：官方准入验证

目标：确认官方能力是否能申请、目标主体能申请哪些 scope。

检查项：

- 开放平台主体类型：自研开发者、系统服务商、企业号认证开发者。
- 应用类型：优先网站应用。
- 目标账号类型：普通号、企业号、品牌号、员工号。
- 申请通用发布相关能力：`h5.share`、`aweme.share`、`aweme.forward`、`open.get.ticket` 或当前控制台对应 scope。
- 单独确认后台直接发布是否可申请：`video.create.bind` / “代替用户发布内容到抖音”。若主体不是党政机关/事业单位或场景不是政务/媒体内部多媒体管理平台，默认判断为不可承诺。
- 申请数据能力：`data.external.user`、`data.external.item` 或当前控制台对应 scope。
- 申请评论能力：`video.comment` 或当前控制台对应 scope。
- 确认 `enterprise.im` 是否可申请，普通号是否不可用。
- 准备能力申请材料：真实产品场景、后台截图、风控机制、审核机制、素材版权机制、客户授权机制、用户确认发布链路。

阶段 0 不写发布执行代码，先拿到控制台能力状态。

### 阶段 1：OAuth + 只读运营问答

目标：先做低风险闭环。

实现：

- `DouyinOpenApiClient` 基础封装。
- OAuth start/callback。
- token 加密保存、自动刷新。
- 同步账号基础信息、视频列表、视频指标、评论列表。
- Agent 只读工具：账号快照、视频指标、评论摘要、运营计划。

验收：

- 能接入一个真实授权账号。
- Agent 能引用真实抖音授权账号数据回答。
- 指标有 `captured_at`、`source_api`、`data_freshness`。
- 不把 T+1 或离线数据说成实时。

### 阶段 2：协作发布任务，用户确认发布

目标：Agent 能生成发布任务和发布包，人工确认后通过 H5/SDK 打开抖音发布页，由用户在抖音端完成最终发布。

实现：

- `douyin_create_publish_job`
- 素材检查、标题/话题检查、可见性检查。
- 管理员/账号负责人审批。
- 生成 H5/SDK 分享 schema、二维码或移动端打开链接。
- 记录用户确认发布状态、`share_id`、后续 Webhooks 或视频数据回查结果。

验收：

- 用户不用理解 OAuth、回调、scope，只能看到“打开抖音确认发布”。
- Clawith 不把“已打开抖音”说成“已公开发布”。
- 发布后通过 Webhooks、视频列表或人工确认回填状态。
- 失败时展示可理解原因，并保留官方错误码/log id 到高级详情。

### 阶段 2B：专项后台直接发布，仅限官方审核通过客户

目标：在主体、行业、能力审核都满足官方“代替用户发布内容到抖音”要求时，启用 OpenAPI 上传和创建视频/图文。

实现：

- `DOUYIN_DIRECT_PUBLISH_ENABLED=false` 作为全局默认。
- 租户/账号级灰度开关：只有通过能力审核的租户可以启用。
- 上传视频/图片。
- 创建视频/图文。
- 保存 `item_id`、`video_id`、`image_ids`、审核状态。
- 平台回查所需测试账号、使用链路、审计记录和监管说明。

验收：

- 同一 idempotency key 不重复发帖。
- 创建成功和公开成功分开展示。
- 所有发布任务保留用户明确感知证据和审批记录。
- 失败时展示官方错误码、log id 和脱敏摘要。

### 阶段 3：评论运营

目标：让 Agent 处理评论，但高风险写操作受控。

实现：

- 拉取评论和回复列表。
- 评论分类：高意向、负面、售后、选题反馈、无需回复。
- 回复草稿生成。
- 低风险评论可配置自动回复；中高风险必须审批。

验收：

- 回复操作写入 `douyin_operations` 和审计日志。
- 高风险评论不能自动回复。
- 日回复上限、最小间隔、暂停开关生效。

### 阶段 4：运营计划和复盘

目标：让 Agent 做运营决策，不增加额外 API 风险。

实现：

- 每日/每周运营简报。
- 选题复盘。
- 发布时间建议。
- 评论区问题反向生成内容选题。
- 视频结构建议。

验收：

- 计划引用具体数据和时间范围。
- 区分事实、推断、建议。
- 可生成下周发布日历，但不直接发布。

## 浏览器自动化兜底边界

仅当官方发布能力申请失败，才考虑人工确认型兜底：

```text
Agent 生成素材和文案
  -> Clawith 生成发布任务
  -> 浏览器工具打开抖音创作者中心并填表
  -> 人类在浏览器里最终点击发布
```

禁止：

- 保存或分发 Cookie。
- 无人值守点击发布。
- 绕过检测、模拟真人、反风控。
- 把浏览器自动化包装成“官方能力”。

可允许：

- 自动填表。
- 自动上传素材到表单。
- 截图给人工确认。
- 人工点击发布后，Clawith 记录“人工发布完成，待运营数据回填”。

## 风险清单

| 风险 | 判断 | 产品处理 |
| --- | --- | --- |
| 后台直接发布不是通用能力 | 高影响 | 默认产品只承诺用户确认发布；后台发布仅作为专项审核增强能力 |
| 官方发布权限申请不通过 | 高影响 | 不影响基础运营 Agent；改用 H5/SDK 用户确认发布和人工发布包 |
| 能力被收回 | 高影响 | 严格场景申请、限流、审批、审计、素材权属证明 |
| 数据接口不能用于售卖型数据服务 | 高影响 | 产品定位为账号自运营工具，不做泛平台数据售卖 |
| 无沙盒环境 | 中高 | 灰度账号实测，测试账号和生产账号分离 |
| 数据延迟 | 中 | UI 和 Agent 回答标注实时/T+1/近 30 天 |
| 内容审核失败或仅自己可见 | 高 | 发布后状态必须有审核中/可见性状态，不把创建成功等同公开成功 |
| 私信能力不确定 | 中高 | 不纳入普通版 MVP，仅做企业号待确认能力 |
| 浏览器自动化封号或失效 | 高 | 官方 OpenAPI 优先；浏览器路径仅人工确认型临时工具 |
| AI 内容合规 | 高 | 发布前内容审核、敏感词/广告法/平台规则检查、人工审批可配置 |

## 需要向抖音控制台确认的问题

- `h5.share`、`aweme.share`、`aweme.forward`、`open.get.ticket` 当前对网站应用的申请材料、审核周期和配额。
- `video.create.bind` 当前是否仅适用于“代替用户发布内容到抖音”能力，主体/行业限制是否严格按能力中心执行。
- 若 Clawith 作为系统服务商或服务市场应用，是否存在面向普通企业客户的替代后台发布能力；若没有，通用 SaaS 只走用户确认发布。
- 目标主体是否符合“用户互动数据”的企业/党政/事业单位准入。
- `data.external.user`、`data.external.item` 的默认配额、QPS、提额路径。
- `video.comment` 的默认配额、评论实时性、企业号/普通号差异。
- `enterprise.im` 是否接受新申请，普通号是否完全不可用。
- H5/SDK 用户确认发布是否能稳定回传 `share_id` 与最终 `item_id`，以及 Webhooks 覆盖情况。
- 创建视频/图文接口是否有官方定时发布参数；若无，Clawith 只能在专项后台发布路径自己做定时任务。
- 多客户账号代运营是否需要系统服务商身份或服务商平台接入。

## 官方资料

- 抖音开放平台能力概览：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/ability/common-solution>
- 抖音登录和授权：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/ability/opensdk/user-authorization/solution>
- 登录与授权凭证说明：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/sdk/mobile-app/permission/overall-permission>
- 移动/网站应用 OpenAPI SDK 总览：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/sdk-overview>
- 获取 access_token：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/account-permission/get-access-token>
- 刷新 access_token：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/account-permission/refresh-access-token>
- 刷新 refresh_token：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/account-permission/refresh-token>
- 发布内容至抖音 H5 场景：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/sdk/web-app/h5/share-to-h5>
- 代替用户发布内容到抖音：<https://developer.open-douyin.com/capacity-center-page/capacity-detail/7224121299067469881>
- 旧版/迁移抖音内容发布接入方案，保留更宽泛发布描述，需以当前能力中心复核为准：<https://open.douyin.com/platform/resource/docs/ability/content-management/douyin-publish-solution/>
- 创建视频：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-create>
- 上传视频：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/upload-video>
- 旧版/迁移创建视频文档，包含用户明确感知要求：<https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/create-video>
- 旧版/迁移上传视频文档，包含分片与素材限制说明：<https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/upload/>
- 创建图文：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-image-text/create-image-text/>
- 上传图片：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-image-text/image-upload>
- 用户互动数据：<https://developer.open-douyin.com/capacity-center-page/capacity-detail/7180284622729658424>
- 视频数据：<https://developer.open-douyin.com/capacity-center-page/capacity-detail/7180522194714230845>
- 视频评论管理：<https://developer.open-douyin.com/capacity-center-page/capacity-detail/7180530418775490619>
- 视频评论管理接入方案：<https://open.douyin.com/platform/resource/docs/ability/interaction-management/video-comment-management-solution>
- Webhooks 事件列表：<https://open.douyin.com/platform/resource/docs/develop/webhooks/event-list>
- 开发者角色与权限：<https://developer.open-douyin.com/docs/resource/zh-CN/developer/introduction/type-and-permission>
- 状态码排查工具：<https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/status-code>
- 抖音开放平台升级/迁移指引：<https://open.douyin.com/platform/resource/docs/transfer>

## GitHub 资料

- 官方 Go SDK：<https://github.com/bytedance/douyin-openapi-sdk-go>
- 第三方 .NET SDK：<https://github.com/fudiwei/DotNetCore.SKIT.FlurlHttpClient.ByteDance>
- 第三方 Python Swagger 生成仓库，不建议采用：<https://github.com/uimeet/douyin>
- 多平台 Go SDK/抽象参考：<https://github.com/ArtisanCloud/MediaX>
- 多平台 Playwright 自动发布，产品形态参考但不采用底层：<https://github.com/dreammis/social-auto-upload>
- Playwright 版 Douyin MCP，MCP 形态参考但不用于生产主线：<https://github.com/flyerhzm/douyin-mcp>
- Playwright 自动发布风险样本：<https://github.com/DaBaoAgent/douyin-auto-publish>
- Playwright + human-confirmation gate 参考：<https://github.com/LouisLin0723/social-auto-publisher>
- 爬虫/下载类风险样本：<https://github.com/Evil0ctal/Douyin_TikTok_Download_API>
