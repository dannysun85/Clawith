/**
 * Translations for built-in agent templates.
 *
 * Backend stores templates with English source-of-truth (`name`,
 * `description`, `capability_bullets`). The Talent Market UI looks up the
 * Chinese rendering here and shows it when `i18n.language` starts with `zh`.
 *
 * Keys MUST match `AgentTemplate.name` exactly. Missing entries fall back to
 * the original English values, so it's safe to ship a partial map.
 */

interface TemplateLocale {
    name: string;
    description: string;
    bullets: string[];
}

interface TemplateLike {
    name: string;
    description: string;
    capability_bullets?: string[];
}

const ZH: Record<string, TemplateLocale> = {
    // ─── Product & Project ──────────────────────────────
    'Product Manager': {
        name: '产品经理',
        description: '把不确定的客户问题转化为有证据的产品决策、可衡量结果和可验收交付切片。',
        bullets: [
            '问题定义 —— 分开用户痛点、业务目标、假设和方案',
            '可决策 PRD —— 范围、验收、指标、依赖和明确非目标',
            '路线图取舍 —— 按证据、影响、风险、成本和可逆性排序',
        ],
    },
    'Project Manager': {
        name: '项目经理',
        description: '通过明确负责人、依赖、风险门禁、证据化状态和恢复方案推进复杂交付。',
        bullets: [
            '交付拆解 —— 里程碑、关键路径、负责人、依赖和退出标准',
            '证据化状态 —— 完成、风险、阻塞和未验证严格分开',
            '恢复规划 —— 给出影响、决策人和回滚清楚的有限选项',
        ],
    },

    // ─── Office ─────────────────────────────────────────
    'Chief of Staff': {
        name: '幕僚长',
        description: '你的私人幕僚长 —— 每日简报、优先级分诊、跟进追踪、用你的语气拟稿。',
        bullets: [
            '每日简报 —— 一分钟读完今天最要紧的事',
            '优先级分诊 —— 哪些做、哪些缓、哪些放、哪些扔',
            '跟进追踪 —— 跨会话不让任何事掉链子',
        ],
    },
    'Private Assistant': {
        name: '私人助理',
        description: '你的私人助理 —— 日常备忘、跟进提醒、草拟消息和轻量计划整理。',
        bullets: [
            '日常协调 —— 整理优先级、提醒和未完成事项',
            '拟稿支持 —— 消息、纪要、短更新先给你一版',
            '跟进记忆 —— 记录人、决策和待办',
        ],
    },

    // ─── Software Development ───────────────────────────
    'Frontend Developer': {
        name: '前端开发工程师',
        description: '构建响应式、无障碍的 Web 界面 —— React/Vue 组件、Core Web Vitals 优化、像素级实现。',
        bullets: [
            '组件实现 —— React/Vue + TypeScript，状态干净',
            '性能审计 —— LCP/INP/CLS 指标 + 具体修复方案',
            '无障碍 review —— WCAG、键盘、屏幕阅读器路径',
        ],
    },
    'Backend Architect': {
        name: '后端架构师',
        description: '设计撑得住的 API、数据模型和服务边界 —— 一致性、延迟、运维成本的取舍都摊在桌上。',
        bullets: [
            'API 设计 —— REST/GraphQL 形态,合约清晰,错误路径明确',
            '数据建模 —— Schema、索引、分区、迁移顺序',
            '取舍分析 —— CAP、一致性、延迟 vs 成本,坦诚说风险',
        ],
    },
    'Code Reviewer': {
        name: '代码审查员',
        description: '像资深工程师一样读 diff —— 抓正确性、安全、可维护性问题,跳过 bikeshedding。',
        bullets: [
            '正确性 & 边界情况 —— 月底凌晨什么会出岔子',
            '安全 —— OWASP 级问题在生产前抓住',
            '可维护性 —— 标记下任读不懂的"聪明代码"',
        ],
    },
    'DevOps Automator': {
        name: 'DevOps 自动化工程师',
        description: '搭 CI/CD、IaC 和 runbook —— 自动化要可观测、不靠魔法。',
        bullets: [
            'CI/CD 设计 —— 快、稳、失败模式清楚',
            '基础设施即代码 —— Terraform、Kubernetes 清单、Helm',
            '运维手册 —— 最常出问题的 10 个场景的应对剧本',
        ],
    },
    'Rapid Prototyper': {
        name: '快速原型工程师',
        description: '把想法变成几小时就能点的 demo —— 在你下重注前先让你"摸到"产品。',
        bullets: [
            'MVP 拆解 —— 把想法剥到只剩验证假设的 3 个核心功能',
            '全栈原型 —— 用最熟、最快的工具搭出可运行 demo',
            '可点击 demo —— 真能跑的,不是死的 mockup',
        ],
    },
    'Designer': {
        name: '设计师',
        description: '协助设计需求拆解、设计系统维护、素材管理和竞品 UI 分析。',
        bullets: [
            '从需求出设计简报',
            '设计系统维护',
            '竞品 UI 分析',
        ],
    },
    'Product Intern': {
        name: '产品实习生',
        description: '协助产品经理做需求分析、竞品研究、用户反馈分析和文档整理。',
        bullets: [
            '需求 & PRD 支持',
            '用户反馈分类',
            '竞品研究',
        ],
    },
    'Multi-Agent Systems Architect': {
        name: '多 Agent 系统架构师',
        description: '设计边界清晰的多 Agent 系统，覆盖拓扑、能力合同、最小权限、失败恢复和评估。',
        bullets: [
            '拓扑设计 —— 所有权、交接、共享状态和终止条件',
            '能力合同 —— 角色、Tool 授权、就绪、审批和回执',
            '可靠性设计 —— 重试、幂等、恢复单元、可观测和评估',
        ],
    },
    'Security Engineer': {
        name: '安全工程师',
        description: '在授权边界内进行防御性审查，输出威胁模型、证据化发现、最小修复和复测方案。',
        bullets: [
            '威胁建模 —— 资产、主体、信任边界、滥用案例和控制',
            '防御审查 —— 身份、权限、密钥、输入、供应链、日志和数据暴露',
            '可执行发现 —— 证据、可利用性、影响、最小修复和复测',
        ],
    },

    // ─── Marketing ──────────────────────────────────────
    'Growth Hacker': {
        name: '增长黑客',
        description: '设计增长实验、分析漏斗、挖掘获客循环 —— 推动真指标,不是虚荣数字。',
        bullets: [
            '漏斗诊断 —— 找到那个最关键的漏点',
            '实验设计 —— ICE 评分 + 假设清晰的 A/B 测试',
            '增长循环 —— 推荐、内容、产品自驱式增长引擎',
        ],
    },
    'Content Creator': {
        name: '内容创作者',
        description: '把想法变成跨平台内容 —— 编辑日历、博客、Newsletter、社媒文案,口吻像你的品牌。',
        bullets: [
            '编辑日历 —— 月度主题 + 各渠道具体选题',
            '长文撰写 —— 博客、Newsletter、落地页文案',
            '平台适配 —— 一个想法,按渠道节奏重写',
        ],
    },
    'SEO Specialist': {
        name: 'SEO 专家',
        description: '通过关键词策略、技术 SEO 审计、基于搜索意图的内容简报来增长自然搜索流量。',
        bullets: [
            '关键词地图 —— 按意图聚类、按机会值排序',
            '技术审计 —— 抓取性、Core Web Vitals、Schema、重复内容',
            '内容简报 —— 写给谁、要什么、对手在写什么',
        ],
    },
    'TikTok Strategist': {
        name: 'TikTok 策略师',
        description: '做能跑完播的短视频选题 —— 钩子驱动、懂算法、贴着你的领域调',
        bullets: [
            '开头钩子 —— 按完播率,不是按巧妙度做',
            '内容公式 —— 验证过的结构,适配你的赛道',
            '发布节奏 —— 测试计划,边发边学算法奖励什么',
        ],
    },
    'Douyin Operations Manager': {
        name: '抖音运营经理',
        description: '负责抖音账号数据复盘、内容选题、发布任务草稿、评论分诊和运营周报，默认审批后执行。',
        bullets: [
            '账号复盘 —— 标清数据时间和关键变化',
            '内容计划 —— 选题、脚本、标题、发布时间一起给',
            '审批后执行 —— 发布和回复先生成任务,确认后再做',
            '评论分诊 —— 负面、高风险、商机评论分开处理',
        ],
    },
    'LinkedIn Content Creator': {
        name: 'LinkedIn 内容创作者',
        description: '在 LinkedIn 上打造个人品牌和 B2B 思想领导力 —— 真有人读、真愿意转的帖子。',
        bullets: [
            '个人品牌口吻 —— 具体、有观点、可信度优先',
            '帖子工程 —— 钩子、故事、要点、一个明确的 CTA',
            '周度节奏 —— 主题积累成可识别的专业领域',
        ],
    },
    'Market Researcher': {
        name: '市场研究员',
        description: '专注市场研究、行业分析、竞争情报追踪和趋势洞察。',
        bullets: [
            '行业 & 趋势分析',
            '竞争情报追踪',
            '结构化研究报告',
        ],
    },
    'Xiaohongshu Operations Manager': {
        name: '小红书运营经理',
        description: '规划有证据的小红书内容、品牌安全创意、实验节奏和审批后发布流程。',
        bullets: [
            '内容策略 —— 用户任务、搜索意图、内容支柱和可检验假设',
            '原生创意 —— 真实标题、实用正文、视觉简报和品牌安全素材',
            '实验闭环 —— 曝光、阅读、收藏、评论、关注和转化',
        ],
    },

    // ─── Data & Research ────────────────────────────────
    'Support Analytics Reporter': {
        name: '客服数据分析报告员',
        description: '从工单数据生成可复现的客服洞察，先做数据质量检查，再给安全口径和运营建议。',
        bullets: [
            '数据质量优先 —— 覆盖、缺失、重复、分类漂移和新鲜度',
            '运营指标 —— 量、积压、响应、解决、重开、升级和满意度',
            '根因证据 —— 分群变化、问题聚类、置信度和行动负责人',
        ],
    },

    // ─── Customer Success ───────────────────────────────
    'Customer Success Manager': {
        name: '客户成功经理',
        description: '把客户目标、采用证据、风险信号和承诺整理成可执行的成功计划。',
        bullets: [
            '成功计划 —— 目标、干系人、里程碑、采用证据和下一价值',
            '健康与风险 —— 可解释信号、置信度、负责人、干预和复查',
            '业务复盘 —— 围绕决策，不做活动流水账',
        ],
    },

    // ─── Trading ────────────────────────────────────────
    'Market Intel Aggregator': {
        name: '市场情报聚合器',
        description: '每日财经情报 —— 扫全球新闻,把信号从噪音里筛出来,5 分钟读完今天对你盘面真有影响的事。',
        bullets: [
            '每日简报 —— 5-10 条真要紧的故事,按影响排序',
            '信号 vs 噪音 —— 标出炒作、堆头条、回锅故事',
            '一句话总结 —— 每条故事末尾给出交易相关解读',
        ],
    },
    'Macro Watcher': {
        name: '宏观观察员',
        description: '盯央行、关键数据、地缘事件 —— 给你日历、共识、二阶解读。',
        bullets: [
            '事件日历 —— 美联储/欧央行/日央行会议、CPI/NFP/GDP、地缘日期',
            '共识框定 —— 市场预期是什么、超预期/不及预期长什么样',
            '二阶解读 —— 一份数据如何重塑利率、汇率、风险偏好',
        ],
    },
    'Watchlist Monitor': {
        name: '盯盘助手',
        description: '盘中盯你的标的:抓有意义的价格变化、关键位突破、可命名催化剂 —— 闭市自动安静。',
        bullets: [
            '盘中告警 —— 价格异动、放量、关键位破位、新闻催化',
            '交易时段纪律 —— 你的市场开盘时跑,闭市静默',
            '收盘复盘 —— 你的 watchlist 今天发生了啥、晚上要想啥',
        ],
    },
    'Technical Analyst': {
        name: '技术分析师',
        description: '老实读图:当前形态、关键位、可能演化、还有让这个判断作废的失效条件。',
        bullets: [
            '看图 —— 趋势、结构、关键位、指标共振',
            '形态框定 —— 多条路径 + 明确的失效条件',
            '多周期对照 —— 日线/4 小时/1 小时是否一致',
        ],
    },
    'Risk Manager': {
        name: '风险管理员',
        description: '每个交易想法的看门人。Stage 你的想法 → 跑 guards → 拿 GREEN/YELLOW/RED → 你来决定要不要发。',
        bullets: [
            '交易暂存 —— 在动手前先把想法写下来',
            'Guard 检查 —— 单笔风险、仓位、集中度、冷静期、规则',
            'GREEN/YELLOW/RED 判定 —— 每笔交易过同一份 checklist',
        ],
    },
    'Trading Journal Coach': {
        name: '交易日志教练',
        description: '读你过去的交易、找重复犯的错、提议规则 —— 跟 Risk Manager 形成闭环,让你越来越好。',
        bullets: [
            '交易记录 —— 每次 push 自动落档 + 打标签',
            '周复盘 —— 跨多笔交易找模式,不是单笔尸检',
            '规则演化 —— 提议加进 trading_rules.md (你点头才生效)',
        ],
    },
    'Earnings & Filings Analyst': {
        name: '财报与公告分析师',
        description: '读季报、8-K、电话会纪要 —— 提炼经营、风险、估值锚相对上期的变化。',
        bullets: [
            '财报深读 —— 经营趋势 + 关键指标 vs 上期变化',
            '公告扫描 —— 8-K、S-3、内部人交易、重大事件',
            '电话会蒸馏 —— 指引变化、口吻转变、管理层回避了什么',
        ],
    },
    'COT Report Analyst': {
        name: 'COT 持仓分析师',
        description: '读 CFTC 周度持仓报告 —— 跟踪商业 vs 投机持仓,标出历史上反转之前的极端位置。',
        bullets: [
            '周度持仓摘要 —— 商业/投机/小户的持仓变化',
            '极端检测 —— 净持仓在多年高点或低点',
            '历史背景 —— 这周的极端值与历史拐点对照',
        ],
    },
    'Pre-Market & Open Briefer': {
        name: '盘前简报员',
        description: '美股交易日早 8 点 ET 的一屏简报:隔夜要闻、期指、关键合约、财报、数据 —— 开盘前你需要知道的。',
        bullets: [
            '隔夜摘要 —— 亚欧收盘、关键头条、期指水平',
            '开盘日 setup —— 盘前财报、数据公布、关键位',
            '8 点 ET 节奏 —— 美股交易日触发一次,其他时间静默',
        ],
    },
    'Tilt & Bias Coach': {
        name: '心态与偏差教练',
        description: '帮你检查现在适不适合交易。在你做出报复性、FOMO、过度仓位、疲劳决策之前抓住你。',
        bullets: [
            '盘前自检 —— "我现在该不该做交易"诊断',
            '偏差识别 —— 给你正在踩的认知陷阱命名',
            '行为干预 —— 状态不对时的具体行动建议',
        ],
    },
};

/**
 * Resolve a template's display fields for the current locale.
 * Falls back to backend (English) values for any field missing in the map.
 */
export function translateTemplate(tpl: TemplateLike, isChinese: boolean): TemplateLocale {
    const fallback: TemplateLocale = {
        name: tpl.name,
        description: tpl.description,
        bullets: tpl.capability_bullets || [],
    };
    if (!isChinese) return fallback;
    const zh = ZH[tpl.name];
    if (!zh) return fallback;
    return {
        name: zh.name || fallback.name,
        description: zh.description || fallback.description,
        bullets: zh.bullets.length ? zh.bullets : fallback.bullets,
    };
}
