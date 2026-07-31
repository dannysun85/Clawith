"""Seed builtin skills into the global skill registry."""

from __future__ import annotations

import hashlib
import json

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.skill import Skill, SkillFile
from app.services.skill_scope import resolve_agent_skills
from app.services.skill_workspace import (
    _remove_stale_automatic_skill,
    _skill_content_hash,
    _skill_file_map,
    _sync_managed_skill,
)


# These folders were incorrectly pushed to every Agent before role-specific
# capability governance was introduced. The hashes are the reviewed historical
# package contents. They allow a one-time, byte-exact cleanup for old workspaces
# that predate the managed provisioning marker; user edits fail closed.
LEGACY_AMBIENT_SKILL_HASHES: dict[str, frozenset[str]] = {
    "skill-creator": frozenset(
        {"26065555e54aa8e32ed903c4a2d73ddc41be0eafceee04cca095bfb074e77502"}
    ),
    "mcp-installer": frozenset(
        {"170e1c4e6e0ec89e5ab4b449293b9e4ed9e46c6dce3f7e817122f1a81b91e1f3"}
    ),
    "brand-safe-media": frozenset(
        {
            # First reviewed package shipped in 22984c5d. Older workspaces
            # predate the managed provenance marker, so the byte-exact hash is
            # the only safe proof that this is not a user-authored Skill.
            "011c738e322d2698a6bdfc252986553c1127e33f69155ac1cc006bffb77566f9",
            "63468e3dac198a61b292e5bcae657302efa6fe9ac7f29d8b69779087a74f6b0f",
            "33544bfb7bd148b0744b209a09789a6148bd7196b6f07d10678e45814c696f16",
        }
    ),
    "vercel-full-stack-deploy": frozenset(
        {
            "b3e900e1c293566e9301a1b66c433dce680ab14af6807700674e55014bac473b",
            "dd3955e421d281dbab240a9f01a16e77d0a4c99ca296902c4a7604ead12dc455",
        }
    ),
}
_SKILL_SYNC_POLICY_REVISION = "role-scoped-skills-v2"


BUILTIN_SKILLS = [
    {
        "name": "Web Research",
        "description": "Systematic web searching and information synthesis. Use when: needing factual data from the web, evaluating sources, or cross-referencing claims. NOT for: simple trivia or local file search.",
        "category": "research",
        "icon": "🔍",
        "folder_name": "web-research",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Web Research
description: Systematic web searching, source evaluation, and information synthesis
---

# Web Research

## Overview
Use this skill when you need to find, evaluate, and synthesize information from the web.

**Keywords**: web search, information retrieval, source evaluation, fact-checking, research

## Process

### 1. Define Search Strategy
- Identify key search terms and variations
- Consider different angles and perspectives
- Plan multiple search queries

### 2. Evaluate Sources
- Check source credibility and recency
- Cross-reference claims across multiple sources
- Note publication dates and author expertise

### 3. Synthesize Findings
- Organize information by theme or relevance
- Highlight key findings and consensus views
- Note conflicting information and gaps

## Output Format
- Start with a brief summary of findings
- Provide detailed sections with source citations
- End with confidence assessment and limitations
""",
            },
            {
                "path": "scripts/search_helper.py",
                "content": (
                    "#!/usr/bin/env python3\n"
                    '"""Helper utilities for structured web search."""\n\n'
                    "from datetime import datetime\n\n\n"
                    "def format_search_results(results: list[dict]) -> str:\n"
                    '    """Format raw search results into a structured report."""\n'
                    "    output = []\n"
                    "    for i, r in enumerate(results, 1):\n"
                    "        title = r.get('title', 'Untitled')\n"
                    "        url = r.get('url', '#')\n"
                    "        snippet = r.get('snippet', 'No description')\n"
                    "        output.append(f'{i}. [{title}]({url})')\n"
                    "        output.append(f'   {snippet}')\n"
                    "        output.append('')\n"
                    "    return '\\n'.join(output)\n\n\n"
                    "def assess_source_credibility(url: str) -> dict:\n"
                    '    """Basic heuristics for source credibility."""\n'
                    "    trusted = ['.edu', '.gov', '.org', 'arxiv.org', 'nature.com']\n"
                    "    score = 0.5\n"
                    "    for d in trusted:\n"
                    "        if d in url:\n"
                    "            score = 0.8\n"
                    "            break\n"
                    "    return {'url': url, 'credibility_score': score,\n"
                    "            'assessed_at': datetime.now().isoformat()}\n"
                ),
            },
        ],
    },
    {
        "name": "Data Analysis",
        "description": "Data interpretation and structured reporting. Use when: analyzing CSV/dataset files, finding trends, or generating statistical summaries. NOT for: writing code to build data models.",
        "category": "analysis",
        "icon": "📊",
        "folder_name": "data-analysis",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Data Analysis
description: Data interpretation, pattern recognition, and structured reporting
---

# Data Analysis

## Overview
Use this skill for analyzing data, identifying patterns, and creating structured reports.

**Keywords**: data analysis, statistics, trends, visualization, reporting

## Process

### 1. Data Understanding
- Identify data types, ranges, and distributions
- Check for missing values and anomalies
- Understand the business context

### 2. Analysis Methods
- Descriptive statistics (mean, median, distribution)
- Trend analysis (time-series patterns)
- Comparative analysis (benchmarking, A/B)
- Correlation and relationship discovery

### 3. Reporting
- Lead with key insights and actionable findings
- Use tables and structured formats for clarity
- Include methodology notes for reproducibility

## Output Format
- Executive summary with top 3 findings
- Detailed analysis with supporting data
- Recommendations based on findings
""",
            },
            {
                "path": "scripts/analyze_csv.py",
                "content": (
                    "#!/usr/bin/env python3\n"
                    '"""Utility for quick CSV data analysis."""\n\n'
                    "import csv\nimport statistics\nfrom collections import Counter\n\n\n"
                    "def analyze_column(data: list[dict], column: str) -> dict:\n"
                    '    """Analyze a single column from CSV data."""\n'
                    "    values = [row.get(column) for row in data if row.get(column) is not None]\n"
                    "    if not values:\n"
                    '        return {"column": column, "count": 0, "error": "No data"}\n\n'
                    '    result = {"column": column, "count": len(values), "unique": len(set(values))}\n\n'
                    "    # Try numeric analysis\n"
                    "    try:\n"
                    "        nums = [float(v) for v in values]\n"
                    "        result.update({\n"
                    '            "type": "numeric",\n'
                    '            "min": min(nums), "max": max(nums),\n'
                    '            "mean": round(statistics.mean(nums), 2),\n'
                    '            "median": round(statistics.median(nums), 2),\n'
                    "        })\n"
                    "    except (ValueError, TypeError):\n"
                    "        freq = Counter(values).most_common(5)\n"
                    '        result.update({"type": "categorical", "top_values": freq})\n\n'
                    "    return result\n\n\n"
                    "def quick_summary(filepath: str) -> str:\n"
                    '    """Generate a quick summary of a CSV file."""\n'
                    "    with open(filepath, 'r') as f:\n"
                    "        reader = csv.DictReader(f)\n"
                    "        data = list(reader)\n"
                    "    columns = data[0].keys() if data else []\n"
                    "    return f'Rows: {len(data)}, Columns: {len(columns)}'\n"
                ),
            },
            {
                "path": "examples/sample_report.md",
                "content": """# Sample Analysis Report

## Executive Summary
Analysis of Q4 2024 sales data reveals a 12% increase in total revenue,
driven primarily by the Enterprise segment (+23%).

## Key Findings
1. **Revenue Growth**: Total revenue increased from $2.1M to $2.35M
2. **Top Segment**: Enterprise accounts grew 23% QoQ
3. **Churn**: SMB churn rate decreased from 5.2% to 4.1%

## Detailed Analysis

| Metric | Q3 2024 | Q4 2024 | Change |
|--------|---------|---------|--------|
| Total Revenue | $2.1M | $2.35M | +12% |
| Enterprise | $1.2M | $1.47M | +23% |
| SMB | $0.9M | $0.88M | -2% |
| Churn Rate | 5.2% | 4.1% | -1.1pp |

## Recommendations
1. Increase investment in Enterprise sales team
2. Investigate SMB revenue decline
3. Continue churn reduction initiatives
""",
            },
        ],
    },
    {
        "name": "Content Writing",
        "description": "Professional content creation and tone adaptation. Use when: drafting articles, emails, or marketing copy with specific stylistic requirements. NOT for: casual chat responses.",
        "category": "creation",
        "icon": "✍️",
        "folder_name": "content-writing",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Content Writing
description: Professional content creation, editing, and tone adaptation
---

# Content Writing

## Overview
Use this skill for creating, editing, and polishing written content across formats.

**Keywords**: writing, editing, copywriting, tone, style, proofreading

## Content Types
- **Articles & Blog Posts**: Informative, engaging long-form content
- **Business Communications**: Emails, memos, reports
- **Marketing Copy**: Headlines, descriptions, calls-to-action
- **Documentation**: Technical docs, guides, FAQs

## Guidelines

### Structure
- Hook readers with a compelling opening
- Use clear headings and logical flow
- Keep paragraphs short (3-5 sentences)
- End with a clear conclusion or call-to-action

### Tone Adaptation
- **Formal**: Business reports, official communications
- **Professional**: Client-facing content, documentation
- **Conversational**: Blog posts, social media
- **Technical**: Developer docs, specifications

### Quality Checklist
- [ ] Clear main message
- [ ] Consistent tone throughout
- [ ] No grammatical errors
- [ ] Appropriate length for format
""",
            },
        ],
    },
    {
        "name": "Competitive Analysis",
        "description": "Competitor research and comparison frameworks. Use when: asked to compare companies, products, or perform SWOT/feature matrix analysis. NOT for: general academic research.",
        "category": "research",
        "icon": "⚔️",
        "folder_name": "competitive-analysis",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Competitive Analysis
description: Market competitor research, comparison frameworks, and strategic insights
---

# Competitive Analysis

## Overview
Use this skill for analyzing competitors, market positioning, and strategic opportunities.

**Keywords**: competitors, market analysis, SWOT, positioning, benchmarking

## Frameworks

### SWOT Analysis
| | Helpful | Harmful |
|---|---|---|
| **Internal** | Strengths | Weaknesses |
| **External** | Opportunities | Threats |

### Feature Comparison Matrix
Compare products across key dimensions:
- Core features and capabilities
- Pricing and packaging
- Target audience
- Market positioning
- Technology stack

### Porter's Five Forces
1. Competitive rivalry intensity
2. Bargaining power of suppliers
3. Bargaining power of buyers
4. Threat of new entrants
5. Threat of substitutes

## Output Format
- Competitor overview table
- Detailed per-competitor analysis
- Strategic recommendations
- Key differentiators summary
""",
            },
        ],
    },
    {
        "name": "Meeting Notes",
        "description": "Meeting summarization and follow-up tracking. Use when: given meeting transcripts or rough notes to extract structured action items and key decisions. NOT for: generic document summarization.",
        "category": "productivity",
        "icon": "📝",
        "folder_name": "meeting-notes",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Meeting Notes
description: Meeting summarization, action item extraction, and follow-up tracking
---

# Meeting Notes

## Overview
Use this skill for processing meeting content into structured summaries with clear action items.

**Keywords**: meetings, notes, action items, decisions, follow-up

## Template

### Meeting Summary
```
Meeting: [Title]
Date: [Date]
Participants: [Names]
Duration: [Time]
```

### Key Decisions
- Numbered list of decisions made

### Action Items
| # | Action | Owner | Due Date | Status |
|---|--------|-------|----------|--------|
| 1 | [Task] | [Name] | [Date] | ⬜ Pending |

### Discussion Points
Brief summary of main topics discussed

### Next Steps
- Follow-up meeting date
- Items deferred to next meeting
""",
            },
        ],
    },
    {
        "name": "Complex Task Executor",
        "description": "Structured methodology for decomposing, planning, and executing complex multi-step tasks with progress tracking",
        "category": "productivity",
        "icon": "🎯",
        "folder_name": "complex-task-executor",
        "is_default": True,
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Complex Task Executor
description: Structured methodology for decomposing, planning, and executing complex multi-step tasks with progress tracking
---

# Complex Task Executor

## When to Use This Skill

Use this skill when a task meets ANY of the following criteria:
- Requires more than 3 distinct steps to complete
- Involves multiple tools or information sources
- Has dependencies between steps (step B needs output from step A)
- Requires research before execution
- Could benefit from a documented plan others can review
- The user explicitly asks for a thorough or systematic approach

**DO NOT use this for simple tasks** like answering a question, reading a single file, or performing one tool call.

## Workflow

### Phase 1: Task Analysis (THINK before acting)

Before creating any files, analyze the task:

1. **Understand the goal**: What is the final deliverable? What does "done" look like?
2. **Assess complexity**: How many steps? What tools are needed?
3. **Identify dependencies**: Which steps depend on others?
4. **Identify risks**: What could go wrong? What information is missing?
5. **Estimate scope**: Is the task feasible with available tools/skills?

### Phase 2: Create Task Plan

Create a task folder and plan file in the workspace:

```
workspace/<task-name>/plan.md
```

The plan.md MUST follow this exact format:

```markdown
# Task: <Clear title>

## Objective
<One-sentence description of the desired outcome>

## Steps

- [ ] 1. <First step — verb-noun format>
  - Details: <What specifically to do>
  - Output: <What this step produces>
- [ ] 2. <Second step>
  - Details: <...>
  - Depends on: Step 1
- [ ] 3. <Third step>
  - Details: <...>

## Status
- Created: <timestamp>
- Current Step: Not started
- Progress: 0/<total>

## Notes
<Any assumptions, risks, or open questions>
```

Rules for writing the plan:
- Each step should be completable in 1-3 tool calls
- Use verb-noun format: "Research competitors", "Draft report", "Validate data"
- Mark dependencies explicitly
- Include expected outputs for each step

### Phase 3: Execute Step-by-Step

For EACH step in the plan:

1. **Read the plan** — Call `read_file` on `workspace/<task>/plan.md` to check current state
2. **Mark as in-progress** — Update the checkbox from `[ ]` to `[/]` and update the "Current Step" field
3. **Execute the step** — Do the actual work (tool calls, analysis, writing)
4. **Record output** — Save results to `workspace/<task>/` (e.g., intermediate files, data)
5. **Mark as complete** — Update the checkbox from `[/]` to `[x]` and update "Progress" counter
6. **Proceed to next step** — Move to the next uncompleted step

### Phase 4: Completion

When all steps are done:
1. Update plan.md status to "✅ Completed"
2. Create a `workspace/<task>/summary.md` with:
   - What was accomplished
   - Key results and deliverables
   - Any follow-up items
3. Present the final result to the user

## Adaptive Replanning

If during execution you discover:
- A step is impossible → Mark it `[!]` with a reason, add alternative steps
- New steps are needed → Add them to the plan with `[+]` prefix
- A step produced unexpected results → Add a note and adjust subsequent steps
- The plan needs major changes → Create a new section "## Revised Plan" and follow it

Always update plan.md BEFORE changing course, so the plan stays the source of truth.

## Error Handling

- If a tool call fails, retry once. If it fails again, mark the step as blocked and note the error.
- Never silently skip a step. Always update the plan to reflect what happened.
- If you're stuck, tell the user what's blocking and ask for guidance.

## Example Scenarios

### Example 1: "Research our top 3 competitors and write a comparison report"

Plan would be:
```
- [ ] 1. Identify the user's company/product context
- [ ] 2. Research Competitor A — website, pricing, features
- [ ] 3. Research Competitor B — website, pricing, features
- [ ] 4. Research Competitor C — website, pricing, features
- [ ] 5. Create comparison matrix
- [ ] 6. Write analysis and recommendations
- [ ] 7. Compile final report
```

### Example 2: "Analyze our Q4 sales data and prepare a board presentation"

Plan would be:
```
- [ ] 1. Read and understand the sales data files
- [ ] 2. Calculate key metrics (revenue, growth, trends)
- [ ] 3. Identify top insights and anomalies
- [ ] 4. Create data summary tables
- [ ] 5. Draft presentation outline
- [ ] 6. Write each presentation section
- [ ] 7. Add executive summary
- [ ] 8. Review and polish final document
```

## Key Principles

1. **Plan is the source of truth** — Always update it before moving on
2. **One step at a time** — Don't skip ahead or batch too many steps
3. **Show your work** — Save intermediate results to the task folder
4. **Communicate progress** — The user can read plan.md at any time to see status
5. **Be adaptive** — Plans change; that's OK if you update the plan first
""",
            },
            {
                "path": "examples/plan_template.md",
                "content": """# Task: [Title]

## Objective
[One-sentence description of the desired outcome]

## Steps

- [ ] 1. [First step]
  - Details: [What specifically to do]
  - Output: [What this step produces]
- [ ] 2. [Second step]
  - Details: [...]
  - Depends on: Step 1
- [ ] 3. [Third step]
  - Details: [...]

## Status
- Created: [timestamp]
- Current Step: Not started
- Progress: 0/3

## Notes
- [Any assumptions, risks, or open questions]
""",
            },
        ],
    },
    # ─── Skill Creator (developer-role Skill) ──────
    {
        "name": "Skill Creator",
        "description": "Create new skills, modify and improve existing skills, and measure skill performance",
        "category": "development",
        "icon": "🛠️",
        "folder_name": "skill-creator",
        "is_default": False,
        "files": [],  # populated at runtime from skill_creator_content
    },
    # ─── Content Research Writer ──────────────────
    {
        "name": "Content Research Writer",
        "description": "Assists in writing high-quality content by conducting research, adding citations, improving hooks, iterating on outlines, and providing real-time section feedback",
        "category": "writing",
        "icon": "✍️",
        "folder_name": "content-research-writer",
        "files": [],  # populated at runtime
    },
    # ─── MCP Tool Installer (integration-role Skill) ─────────
    {
        "name": "MCP Tool Installer",
        "description": "Guide authorized users through discovering and importing MCP tools; provider authorization is completed on the Agent Tools page",
        "category": "development",
        "icon": "🔌",
        "folder_name": "mcp-installer",
        "is_default": False,
        "files": [],  # populated at runtime from agent_template/skills/mcp-installer/SKILL.md
    },
    # ─── Brand-safe media (media/content-role Skill) ──────────
    {
        "name": "Brand-safe Media",
        "description": "Required workflow for customer-facing images or videos containing exact copy, logos, packaging, or reference products",
        "category": "creation",
        "icon": "🎬",
        "folder_name": "brand-safe-media",
        "is_default": False,
        "files": [],  # populated from agent_template/skills/brand-safe-media/SKILL.md
    },
    {
        "name": "Volcengine Seedream Commercial",
        "description": "Commercial image planning and prompt protocol adapted from the official Volcengine Agent Plan Seedream Skill",
        "category": "creation",
        "icon": "🎨",
        "folder_name": "volcengine-seedream-commercial",
        "is_default": False,
        "files": [],
    },
    {
        "name": "Volcengine Seedance Commercial",
        "description": "Commercial real-person and product video workflow adapted from the official Volcengine Agent Plan Seedance Skill",
        "category": "creation",
        "icon": "🎬",
        "folder_name": "volcengine-seedance-commercial",
        "is_default": False,
        "files": [],
    },
    {
        "name": "Commercial Presentation",
        "description": "Create or revise customer-facing slide decks with editable PPTX/PDF, sourced claims, intentional layouts, real visuals, and governed quality review",
        "category": "creation",
        "icon": "📊",
        "folder_name": "commercial-presentation",
        "is_default": False,
        "files": [],
    },
    {
        "name": "Commercial Voiceover",
        "description": "Create or revise customer-facing narration and voice tracks with exact scripts, pronunciation control, durable audio, provider-safe routing, and listening review",
        "category": "creation",
        "icon": "🔊",
        "folder_name": "commercial-voiceover",
        "is_default": False,
        "files": [],
    },
    # ─── Market Data (trading agents) ──────────────
    {
        "name": "Market Data",
        "description": "Fetch stock quotes, OHLCV history, and fundamentals via a remote MCP server. Use when a trading agent needs price/financial data on US equities.",
        "category": "trading",
        "icon": "MD",
        "folder_name": "market-data",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Market Data
description: Stock quotes, OHLCV history, and fundamentals for US equities via Smithery MCP
---

# Market Data

## When to Use This Skill

Use when a trading agent needs:
- Real-time or historical price data on US equities (NYSE / NASDAQ)
- Financial statements (income, balance sheet, cash flow)
- Pre-computed technical indicators (RSI, MACD, Bollinger Bands, SMA, EMA, ADX, etc.)
- Quarterly EPS actuals, estimates, and surprises

**Scope (v1)**: US-listed equities only. **Not yet covered**: futures (CL=F, GC=F, ES=F), forex, crypto, international stocks. For these, fall back to `web-research`.

---

## Step-by-Step Protocol

### Step 1 — Check if Shibui Finance MCP is already installed

Look at your tool list. If you have `unlock_financial_analysis` and `stock_data_query` tools, skip to Step 3.

### Step 2 — Stop or request an explicit capability grant

The presence of this Skill does not grant installation Tools. If
`import_mcp_server` is not visible in the current tool list, explain that live
market data is not configured for this Agent and ask an authorized manager to
enable/import it from the Agent's Tools page. Fall back to `web-research` only
when the user accepts the lower freshness and structure guarantees.

If `import_mcp_server` is visible and the user explicitly asked to install the
data integration, import Shibui Finance:

```
import_mcp_server(
  server_id="shibui/finance",
  config={"smithery_api_key": "<key>"}  # only on first import; reused after
)
```

If the tool reports that no Smithery API key is configured, direct the user to
the Agent's Tools page. Never ask for or echo a credential in chat, and never
claim market data is live before the imported tools pass their readiness check.

### Step 3 — Activate the data session

The Shibui MCP requires a one-time activation per session before SQL queries work:

```
unlock_financial_analysis(...)
```

This returns an access token automatically managed by the MCP — you don't need to pass it in subsequent calls.

### Step 4 — Query data

The primary tool is `stock_data_query`, which takes natural-language prompts or SQL. Examples:

#### Get latest quote
```
stock_data_query(query="Get the most recent close price, daily change %, and volume for AAPL")
```

#### Get OHLCV history
```
stock_data_query(query="Daily OHLCV for TSLA over the past 90 trading days")
```

#### Get fundamentals
```
stock_data_query(query="Latest annual income statement and balance sheet for MSFT, with key ratios PE PB ROE")
```

#### Get pre-computed indicator
```
stock_data_query(query="14-day RSI for NVDA over the past 30 trading days")
```

#### Symbol screening
```
stock_data_query(query="US stocks with market cap > $10B, P/E < 20, and revenue growth > 15% YoY")
```

### Step 5 — Always cite as-of date

Every fetched number ships with the **as-of date** Shibui returns. Include it in your output to the user — never present stale data without timestamp context.

---

## Output Conventions

When you present market data to the user:

- Quote: `**AAPL** $192.45 +1.2% · Vol 48.2M · as of 2026-04-25 close`
- Indicator: `**TSLA RSI(14)** 68.4 (mildly overbought) · as of 2026-04-25`
- Fundamentals: bullet the headline numbers + 1-line interpretation, never dump raw tables

For OHLCV history with many rows, save to `workspace/<task>/<symbol>-history.csv` rather than rendering inline.

---

## What NOT to Do

- Do not present data without an as-of date — stale prices mislead
- Do not extrapolate from one query to another asset class (no futures, FX, crypto via this MCP)
- Do not exceed reasonable query depth — Shibui is free, but courtesy says don't run 100 SQL queries when 5 will do
- Do not fabricate numbers when the MCP can't answer — say "not available via this skill, falling back to web-research"

---

## Fallback (if Shibui MCP not available)

If the user can't / won't install the MCP, downgrade to `web-research`:
- Quotes: search "AAPL stock price now"
- History: search "AAPL daily chart 90 days"
- Fundamentals: search "AAPL 10-Q latest" or company IR page

Always tell the user "I'm using web search instead of structured market data — accuracy and timeliness will be lower."

---

## Asset Class Coverage (clawith roadmap)

| Asset class | v1 (this skill) | v2 plan |
|---|---|---|
| US equities | Yes (Shibui) | — |
| US ETFs | Partial (Shibui) | improve |
| Futures (CME) | No — use web-research | self-built yfinance MCP |
| Forex | No — use web-research | self-built MCP |
| Crypto | No — use web-research | dedicated crypto MCP |
| International stocks | No — use web-research | TBD |
""",
            },
        ],
    },
    # ─── Financial Calendar (trading agents) ──────────────
    {
        "name": "Financial Calendar",
        "description": "Look up earnings dates, FOMC meetings, CPI/NFP/GDP release dates, and other macro events that move markets. v1 uses structured web search; v2 will add dedicated MCP.",
        "category": "trading",
        "icon": "FC",
        "folder_name": "financial-calendar",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Financial Calendar
description: Earnings calendar + macro events (FOMC, CPI, NFP, central banks) via structured web research
---

# Financial Calendar

## When to Use This Skill

Use when a trading agent needs:
- Upcoming earnings release dates for specific companies (or this week's reporters)
- Federal Reserve FOMC meeting dates and minutes release
- US economic data release schedule: CPI, PPI, NFP, GDP, retail sales, ISM, PCE
- Central bank decision dates (ECB, BoE, BoJ, PBoC)
- Geopolitical / fiscal events (debt ceiling, election dates, OPEC meetings)

---

## Implementation Note (v1)

clawith does **not** ship a dedicated calendar MCP server in v1. Smithery doesn't yet have a robust earnings/macro calendar tool. So this skill is a **structured wrapper around `web-research`** with curated query templates and source preferences. v2 will add a dedicated MCP backed by a free API (likely finnhub or trading-economics).

This means: every calendar query in v1 takes a web round-trip. Cache results in `memory/calendar_<month>.md` so the agent doesn't re-fetch the same Fed schedule three times in one week.

---

## Step-by-Step Protocol

### Step 1 — Check memory first

Before web searching, check `memory/calendar_<YYYY-MM>.md` for the current month. If you've already cached this month's events, use them and only web-search for what's missing.

### Step 2 — Run targeted query (use templates below)

#### Earnings calendar
```
web_research("AAPL next earnings date 2026 site:investor.apple.com OR site:nasdaq.com")
```

For a sector / market scan: `"this week earnings calendar US large cap"` then verify each name against IR sources.

#### FOMC schedule
```
web_research("Federal Reserve FOMC meeting schedule 2026 site:federalreserve.gov")
```

Authoritative source: federalreserve.gov/monetarypolicy/fomccalendars.htm — the calendar page directly.

#### US economic data calendar
```
web_research("BLS CPI release schedule 2026 site:bls.gov")
web_research("Bureau of Economic Analysis GDP release schedule 2026 site:bea.gov")
web_research("BLS Employment Situation NFP schedule 2026 site:bls.gov")
```

#### Central bank decisions
```
web_research("ECB Governing Council meeting schedule 2026 site:ecb.europa.eu")
web_research("Bank of England MPC schedule 2026 site:bankofengland.co.uk")
```

#### Aggregate calendar (lower fidelity, faster)
```
web_research("economic calendar this week high impact events")
```
Trusted aggregators: investing.com/economic-calendar, forexfactory.com/calendar, tradingeconomics.com/calendar

### Step 3 — Persist to memory

After each successful fetch, append to `memory/calendar_<YYYY-MM>.md`:

```markdown
## 2026-04 Calendar (last updated: 2026-04-27)

### FOMC
- 2026-04-30: rate decision + press conference (1 day, both PM EDT)
- 2026-06-12: rate decision

### US Data
- 2026-04-30: GDP advance Q1 (8:30am ET, BEA)
- 2026-05-02: NFP April (8:30am ET, BLS)
- 2026-05-13: CPI April (8:30am ET, BLS)

### Earnings (tracked tickers only)
- 2026-04-30 AMC: AAPL Q2 (consensus EPS $1.57)
- 2026-05-01 BMO: AMZN Q1 (consensus EPS $0.99)
```

### Step 4 — Cite source + confidence

Every event ships with:
- The source URL (preferring official: federalreserve.gov, bls.gov, bea.gov)
- A "confidence" tag: `[official]` for sources directly from the agency, `[aggregator]` for investing.com / forexfactory etc.

---

## Output Conventions

For a single event lookup:
```
**AAPL Q2 earnings** — 2026-04-30 AMC (after market close) · consensus EPS $1.57 [aggregator: nasdaq.com]
```

For a weekly briefing block:
```
**This week (2026-04-28 to 2026-05-02)**
- Tue 4/29 — JOLTS (10am, low impact)
- Wed 4/30 — **FOMC decision + presser** (2pm/2:30pm, very high impact)
- Wed 4/30 — GDP Q1 advance (8:30am, high impact)
- Wed 4/30 AMC — **AAPL Q2** (very high impact)
- Fri 5/2 — **NFP April** (8:30am, very high impact)
```

---

## What NOT to Do

- Do not invent dates when web-research returns ambiguous results — say "I couldn't pin down the exact date, here's the source page to check"
- Do not present aggregator data (investing.com etc.) as authoritative when the user is making a decision — escalate to the official agency source
- Do not over-cache — events get rescheduled. Re-verify FOMC and NFP dates within 7 days of the event
- Do not flag everything as "high impact" — distinguish **very high** (FOMC, NFP, CPI), **high** (GDP, retail sales, ISM, mega-cap earnings), **medium** (sector earnings, Fed speakers), **low** (weekly claims, regional Fed indices)

---

## v2 Roadmap

When clawith builds a dedicated finance-calendar MCP server, this skill will switch to direct API calls:

```
get_earnings_calendar(start="2026-04-28", end="2026-05-02")
get_macro_calendar(start="2026-04-28", end="2026-05-02", min_impact="high")
get_econ_event_consensus(event_id="us-cpi-2026-05")
```

Until then, structured web search is the contract.
""",
            },
        ],
    },
    {
        "name": "Full-Stack App Deploy (Vercel + Neon)",
        "description": "Guides the agent through the planning, development, and deployment of a full-stack application (frontend, API routes, database) to Vercel and Neon. Recommend reading this skill at the project's inception to configure tokens, choose frameworks, and design the database architecture upfront, avoiding late-stage deployment surprises.",
        "category": "deploy",
        "icon": "🚀",
        "folder_name": "vercel-full-stack-deploy",
        "is_default": False,
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Full-Stack App Deploy (Vercel + Neon)
description: Guides the agent through the planning, development, and deployment of a full-stack application to Vercel and Neon, ensuring configuration, credentials, and architecture decisions are addressed early.
---

# Full-Stack App Deploy (Vercel + Neon)

## When to Use
Use this skill when the user requests a "website", "web app", or "online system" (product) that requires a database.
If the user only requests static frontend pages without a database or backend
APIs, use `publish_page` only when it is visible and enabled for this Agent.
Otherwise explain the missing capability; a Skill never grants a Tool.

> [!IMPORTANT]
> **Capability and verification boundary:**
> Develop inside `workspace`. Use `execute_code` for builds only when it is
> visible and authorized. If build execution or the required deployment Tools
> are absent, stop and report the exact missing capability. Never compensate
> for a missing Tool by claiming a deployment, and never use production as the
> first build test.

---

## Step 0: Guide the User to Enable Tools and Configure Tokens

> 🔔 All Vercel/Neon deployment-related tools are disabled by default and must be enabled manually by the user.

**The Agent should proactively check and guide the user through the following actions:**

### 0.1 Check if Vercel Tools are Enabled
- Verify if the Vercel tools under the "deploy" category in the tool list are enabled.
- If not enabled, inform the user:
  "To develop and deploy full-stack applications, you need to enable the Vercel-related tools in the 'Tool Management' page under the 'Deploy' category: Deploy to Vercel, List Vercel Deployments, Get Deploy Logs, Set Environment Variable, and Create Postgres Database. You can also enable Manage Domain if you want to use custom domains."

### 0.2 Guide the User to Sign Up for Vercel and Get a Token
- If Vercel tools are enabled but the `vercel_token` is missing or empty, guide the user:
  1. Visit https://vercel.com/signup to register (supports GitHub / Email sign up).
  2. Once logged in, go to https://vercel.com/account/tokens.
  3. Click "Create" to generate a new token (suggested name: "clawith", Scope: "Full Account").
  4. Copy the generated token, return to the Astra tool settings page, and paste it into the "Vercel Access Token" configuration field for "Deploy to Vercel" or any other Vercel tools.

### 0.3 Guide the User to Sign Up for Neon and Get an API Key
- If the project requires a database (Postgres), guide the user:
  1. Visit https://neon.tech to register (recommending GitHub OAuth for instant registration).
  2. Once registered, go to the API Keys section in the console settings (https://console.neon.tech/app/settings/api-keys).
  3. Click "Create new API Key", name it (e.g., "clawith"), and copy the generated key.
  4. Return to the Astra tool settings page, find the `Create Postgres Database` tool, and paste the key into the "Neon API Key" configuration field.

---

## Step 1: Choose Framework and Initialize

### 1.1 Confirm Development Framework
Confirm the framework to be used with the user:
- **Proactively Recommend Next.js**: Explain to the user: "Next.js is the official native framework for Vercel, offering the best integration, zero-config serverless deployments, API routes, and seamless database connections."
- **Default Framework**: If the user has no explicit preference, default to using **Next.js** to initialize the project.
- **Other Options**: If the user explicitly asks for a single-page app (SPA) or lighter alternatives, Vite/Astro can be used, but warn them about independent API hosting limitations.

---

## Step 2: Full-Stack Development and Debugging

### 2.1 Initialize Boilerplate
- Initialize the project using Next.js (prefer non-interactive setup: `npx create-next-app@latest ./ --typescript --eslint --tailwind --src-dir --app --import-alias "@/*"` or modify based on project directory).
- Write backend APIs under `src/app/api/`.

### 2.2 Optimized Deployment & Database Association Sequence (Crucial)
To avoid unnecessary deployments, save Vercel build limits, and prevent serving a broken state without database configuration, strictly follow this sequence:
1. **Create the Database first**: Call the `neon_create_database` tool to obtain the `DATABASE_URL`.
   - **Important**: If the tool returns a "Neon free limit reached" warning, notify the user and guide them to delete old projects or supply an existing database connection string.
2. **Configure Vercel Environment Variables**: Call the `vercel_set_env` tool to inject the `DATABASE_URL` into Vercel.
   - Key: `DATABASE_URL`
   - Value: `<The connection string obtained>`
3. **Deploy the application**: Once the environment variables are successfully configured in Vercel, call the `vercel_deploy` tool to deploy.
   - Keep Vercel Deployment Protection and any existing access controls in
     their current state. Changing authentication, domains, or protection is a
     separate security-sensitive action and requires explicit user approval.

### 2.3 Development, Testing, and Debugging
- **Local Verification**: If `execute_code` is enabled, run the repository's
  declared build/test command inside the workspace. If it is unavailable, state
  that local execution is unverified and ask before incurring a provider
  deployment merely to test a build.
- **Preview Deployment**: Call `vercel_deploy` (specifying `production=False`) to get a unique Preview URL.
- **Automated Verification**: Use a Browser tool only if one is actually visible;
  otherwise provide the Preview URL and a concrete manual verification list.
- **Build and Log Debugging**: If the build fails, call `vercel_get_deploy_logs` to view compilation or runtime logs to diagnose and fix errors.
- **Production Deployment**: A successful Preview is not permission to publish.
  Call `vercel_deploy(production=True)` only after the user explicitly approves
  production deployment in the current task.

---

## Debugging and Limit Status Monitoring
- **Build Failures** → Use `vercel_get_deploy_logs` to check build logs.
- **Runtime Errors** → Use `vercel_get_deploy_logs` to check runtime logs.
- **Limit Monitoring** → Report usage only when an enabled provider Tool returns
  that data. Do not invent bandwidth, build, or Neon quota percentages.
- **Visual Checks** → Use a Browser tool only when available; do not claim a
  page-level check from build output alone.
"""
            }
        ]
    }
]


async def seed_skills():
    """Insert builtin skills if they don't exist."""
    from app.services.skill_creator_content import get_skill_creator_files
    from pathlib import Path as _Path

    _files_dir = _Path(__file__).parent / "skill_creator_files"
    _template_skills_dir = _Path(__file__).parent.parent.parent / "agent_template" / "skills"

    # Populate skill-creator files at runtime
    for s in BUILTIN_SKILLS:
        if s["folder_name"] == "skill-creator" and not s["files"]:
            s["files"] = get_skill_creator_files()
        elif s["folder_name"] == "content-research-writer" and not s["files"]:
            # Load from downloaded file
            crw_file = _files_dir / "content_research_writer__SKILL.md"
            if crw_file.exists():
                s["files"] = [{"path": "SKILL.md", "content": crw_file.read_text(encoding="utf-8")}]
        elif s["folder_name"] == "mcp-installer" and not s["files"]:
            mcp_file = _template_skills_dir / "mcp-installer" / "SKILL.md"
            if mcp_file.exists():
                s["files"] = [{"path": "SKILL.md", "content": mcp_file.read_text(encoding="utf-8")}]
            else:
                logger.warning("[SkillSeeder] mcp-installer/SKILL.md not found in agent_template/skills/")
        elif s["folder_name"] in {
            "brand-safe-media",
            "commercial-presentation",
            "commercial-voiceover",
            "volcengine-seedream-commercial",
            "volcengine-seedance-commercial",
        } and not s["files"]:
            skill_root = _template_skills_dir / s["folder_name"]
            if skill_root.exists():
                s["files"] = [
                    {
                        "path": path.relative_to(skill_root).as_posix(),
                        "content": path.read_text(encoding="utf-8"),
                    }
                    for path in sorted(skill_root.rglob("*"))
                    if path.is_file()
                ]
            else:
                logger.warning(
                    "[SkillSeeder] managed creation Skill not found in agent_template/skills/"
                )

    async with async_session() as db:
        for skill_data in BUILTIN_SKILLS:
            result = await db.execute(
                select(Skill).where(
                    Skill.tenant_id.is_(None),
                    Skill.folder_name == skill_data["folder_name"],
                )
            )
            existing = result.scalar_one_or_none()
            is_default = skill_data.get("is_default", False)
            if existing:
                # Update metadata
                existing.name = skill_data["name"]
                existing.description = skill_data["description"]
                existing.category = skill_data["category"]
                existing.icon = skill_data["icon"]
                existing.is_default = is_default
                # Sync files — add missing ones
                from sqlalchemy.orm import selectinload
                res2 = await db.execute(
                    select(Skill).where(Skill.id == existing.id).options(selectinload(Skill.files))
                )
                sk = res2.scalar_one()
                existing_paths = {f.path: f for f in sk.files}
                for f in skill_data["files"]:
                    if f["path"] in existing_paths:
                        # Update content if changed
                        existing_file = existing_paths[f["path"]]
                        if existing_file.content != f["content"]:
                            existing_file.content = f["content"]
                            logger.info("[SkillSeeder] Updated builtin skill file")
                    else:
                        db.add(SkillFile(skill_id=existing.id, path=f["path"], content=f["content"]))
                        logger.info("[SkillSeeder] Added builtin skill file")
            else:
                skill = Skill(
                    name=skill_data["name"],
                    description=skill_data["description"],
                    category=skill_data["category"],
                    icon=skill_data["icon"],
                    folder_name=skill_data["folder_name"],
                    is_builtin=True,
                    is_default=is_default,
                )
                db.add(skill)
                await db.flush()
                for f in skill_data["files"]:
                    db.add(SkillFile(skill_id=skill.id, path=f["path"], content=f["content"]))
                logger.info("[SkillSeeder] Created builtin skill")
        await db.commit()
        logger.info("[SkillSeeder] Skills seeded")


async def push_default_skills_to_existing_agents():
    """Sync effective global and role Skills into every existing Agent.

    The historical function name is retained for startup compatibility. The
    effective set is tenant-scoped and includes global defaults plus the
    AgentTemplate's declared Skill folders; registry-managed files are updated
    only while the Agent copy remains unmodified.
    """
    from app.models.agent import Agent, AgentTemplate
    from app.models.skill import Skill
    from app.models.system_settings import SystemSetting
    from sqlalchemy.orm import selectinload
    from app.services.agent_manager import agent_manager
    from app.services.storage import get_storage_backend

    async with async_session() as db:
        # Load the registry once. Resolution below applies each agent's tenant
        # view and uses tenant overrides for matching global default folders.
        skills_r = await db.execute(
            select(Skill).options(selectinload(Skill.files))
        )
        all_skills = skills_r.scalars().all()
        templates_r = await db.execute(select(AgentTemplate))
        templates = templates_r.scalars().all()
        template_folders_by_id = {
            template.id: tuple(template.default_skills or [])
            for template in templates
        }
        template_skill_folders = {
            folder
            for folders in template_folders_by_id.values()
            for folder in folders
            if isinstance(folder, str) and folder
        }
        global_default_folders = {
            skill.folder_name
            for skill in all_skills
            if skill.tenant_id is None and skill.is_default
        }
        relevant_skills = [
            skill
            for skill in all_skills
            if (
                skill.is_default
                or skill.folder_name in global_default_folders
                or skill.folder_name in template_skill_folders
            )
        ]
        cleanup_candidate_folders = {
            skill.folder_name for skill in all_skills if skill.folder_name
        } | set(LEGACY_AMBIENT_SKILL_HASHES)

        # Agent/template identity is part of the desired state. Without this,
        # assigning an existing agent to a different role could be hidden by a
        # previously matching registry-only hash.
        agents_r = await db.execute(select(Agent))
        agents = agents_r.scalars().all()

        # Include owner and identity so a new tenant override triggers a safe
        # scoped rescan without ever copying it into another tenant.
        hasher = hashlib.sha256()
        hasher.update(_SKILL_SYNC_POLICY_REVISION.encode())
        for folder in sorted(cleanup_candidate_folders):
            hasher.update(folder.encode())
        for skill in sorted(
            relevant_skills,
            key=lambda item: (
                str(item.tenant_id or ""),
                item.folder_name,
                str(item.id),
            ),
        ):
            hasher.update(
                f"{skill.tenant_id}:{skill.id}:{skill.folder_name}:{skill.is_default}".encode()
            )
            hasher.update(_skill_content_hash(_skill_file_map(skill)).encode())
        for template in sorted(templates, key=lambda item: str(item.id)):
            hasher.update(str(template.id).encode())
            hasher.update(
                json.dumps(
                    list(template.default_skills or []),
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            )
        for agent in sorted(agents, key=lambda item: str(item.id)):
            hasher.update(f"{agent.id}:{agent.template_id}".encode())
        current_hash = hasher.hexdigest()

        # Check if we already synced this version of default skills
        setting_r = await db.execute(
            select(SystemSetting).where(SystemSetting.key == "default_skills_sync_hash")
        )
        setting = setting_r.scalar_one_or_none()
        if (
            setting
            and setting.value.get("hash") == current_hash
            and not setting.value.get("conflicts")
        ):
            logger.info(f"[SkillSeeder] Default skills sync hash '{current_hash}' matches, skipping sync for existing agents")
            return

        pushed = 0
        adopted = 0
        updated = 0
        removed = 0
        preserved = 0
        conflicts = 0
        storage = get_storage_backend()
        for agent in agents:
            default_skills = resolve_agent_skills(
                relevant_skills,
                agent.tenant_id,
                template_folders=template_folders_by_id.get(agent.template_id, ()),
            )
            agent_prefix = agent_manager._agent_storage_prefix(agent.id)
            desired_folders = {skill.folder_name for skill in default_skills}
            for folder_name in sorted(cleanup_candidate_folders - desired_folders):
                cleanup_status = await _remove_stale_automatic_skill(
                    storage,
                    f"{agent_prefix}/skills/{folder_name}",
                    accepted_legacy_hashes=LEGACY_AMBIENT_SKILL_HASHES.get(
                        folder_name,
                        frozenset(),
                    ),
                )
                if cleanup_status == "removed":
                    removed += 1
                elif cleanup_status == "preserved":
                    preserved += 1
                elif cleanup_status == "conflict":
                    conflicts += 1
                    logger.warning(
                        "[SkillSeeder] Preserved ambiguous or user-edited stale Skill "
                        "folder agent_id={} skill_folder={}",
                        agent.id,
                        folder_name,
                    )
            for skill in default_skills:
                if not skill.files:
                    continue

                skill_prefix = f"{agent_prefix}/skills/{skill.folder_name}"
                status, changed_files = await _sync_managed_skill(
                    storage,
                    skill_prefix,
                    skill,
                    accepted_legacy_hashes=LEGACY_AMBIENT_SKILL_HASHES.get(
                        skill.folder_name,
                        frozenset(),
                    ),
                )
                if status == "conflict":
                    conflicts += 1
                    logger.warning(
                        "[SkillSeeder] Preserved user-edited Skill folder "
                        "agent_id={} skill_folder={}",
                        agent.id,
                        skill.folder_name,
                    )
                    continue
                if status == "adopted":
                    adopted += 1
                elif status == "updated":
                    updated += 1
                pushed += changed_files

        # Save/update the sync hash in settings
        sync_state = {
            "hash": current_hash,
            "conflicts": conflicts,
            "removed": removed,
            "preserved": preserved,
        }
        if setting:
            setting.value = sync_state
        else:
            db.add(SystemSetting(key="default_skills_sync_hash", value=sync_state))
        await db.commit()

        if pushed or adopted or updated or removed or preserved or conflicts:
            logger.info(
                "[SkillSeeder] Default Skill sync files={} adopted={} updated={} "
                "removed={} preserved={} conflicts={}",
                pushed,
                adopted,
                updated,
                removed,
                preserved,
                conflicts,
            )
        else:
            logger.info("[SkillSeeder] All existing agents already have all default skills")
