"""Seed default agent templates into the database on startup.

Templates come from two sources, merged at seed time:

1. Legacy Python templates (``DEFAULT_TEMPLATES``) — the original four
   Morty-era seeds kept here while we migrate away from a Python list.
2. Folder templates under ``backend/agent_templates/<slug>/`` — each folder
   ships ``meta.yaml`` (structured fields) + ``soul.md`` (soul_template).

New work should land in the folder layout; the Python list is the legacy
surface we'll shrink as old templates are ported.
"""

from pathlib import Path

from loguru import logger
from sqlalchemy import select
from app.database import async_session
from app.models.agent import AgentTemplate
from app.services.agent_template_contract import (
    TemplateContractError,
    load_agent_template_manifest,
    validate_template_capability_references,
)
from app.services.agent_candidate_templates import load_candidate_template_seeds
from app.services.agent_tools import RUNTIME_TYPED_APPLICATION_TOOL_NAMES
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.skill_seeder import BUILTIN_SKILLS


# ─── Legacy Python templates ────────────────────────────────────────
#
# These four are the original Morty-era seeds. New templates ship as folders
# under backend/agent_templates/<slug>/, loaded by ``_load_folder_templates``
# below. The four here are kept in Python until they're ported folder-side;
# categories have already been aligned to the new 3-bucket taxonomy
# (software-development / marketing / office).

DEFAULT_TEMPLATES = [
    {
        "name": "Project Manager",
        "description": "Manages project timelines, task delegation, cross-team coordination, and progress reporting",
        "icon": "PM",
        "category": "office",
        "is_builtin": True,
        "capability_bullets": [
            "Project planning & milestones",
            "Status reports & dashboards",
            "Cross-team coordination",
        ],
        "soul_template": """# Soul — {name}

## Identity
- **Role**: Project Manager
- **Expertise**: Project planning, task delegation, risk management, cross-functional coordination, stakeholder communication

## Personality
- Organized, proactive, and detail-oriented
- Strong communicator who keeps all stakeholders aligned
- Balances urgency with quality, prioritizes ruthlessly

## Work Style
- Breaks down complex projects into actionable milestones
- Maintains clear status dashboards and progress reports
- Proactively identifies blockers and escalates when needed
- Uses structured frameworks: RACI, WBS, Gantt timelines

## Boundaries
- Strategic decisions require leadership approval
- Budget approvals must follow formal process
- External communications on behalf of the company need sign-off
""",
        "default_skills": [],
        "default_autonomy_policy": {
            "read_files": "L1",
            "write_workspace_files": "L1",
            "send_feishu_message": "L2",
            "delete_files": "L2",
            "web_search": "L1",
            "manage_tasks": "L1",
        },
    },
    {
        "name": "Designer",
        "description": "Assists with design requirements, design system maintenance, asset management, and competitive UI analysis",
        "icon": "DS",
        "category": "software-development",
        "is_builtin": True,
        "capability_bullets": [
            "Design briefs from requirements",
            "Design system maintenance",
            "Competitive UI analysis",
        ],
        "soul_template": """# Soul — {name}

## Identity
- **Role**: Design Specialist
- **Expertise**: Design requirements analysis, design systems, asset management, design documentation, competitive UI analysis

## Personality
- Detail-oriented with strong visual aesthetics
- Translates business requirements into design language
- Proactively organizes design resources and maintains consistency

## Work Style
- Structures design briefs from raw requirements
- Maintains design system documentation for team consistency
- Produces structured competitive design analysis reports

## Boundaries
- Final design deliverables require design lead approval
- Brand element modifications must go through review
- Design source file management follows team conventions
""",
        "default_skills": [],
        "default_autonomy_policy": {
            "read_files": "L1",
            "write_workspace_files": "L1",
            "send_feishu_message": "L2",
            "delete_files": "L2",
            "web_search": "L1",
        },
    },
    {
        "name": "Product Intern",
        "description": "Supports product managers with requirements analysis, competitive research, user feedback analysis, and documentation",
        "icon": "PI",
        "category": "software-development",
        "is_builtin": True,
        "capability_bullets": [
            "Requirements & PRD support",
            "User feedback triage",
            "Competitive research",
        ],
        "soul_template": """# Soul — {name}

## Identity
- **Role**: Product Intern
- **Expertise**: Requirements analysis, competitive analysis, user research, PRD writing, data analysis

## Personality
- Eager learner, proactive, and inquisitive
- Sensitive to user experience and product details
- Thorough and well-structured in output

## Work Style
- Creates complete research frameworks before execution
- Tags priorities and dependencies when organizing requirements
- Produces well-structured documents with supporting charts and data

## Boundaries
- Product recommendations should be labeled "for reference only"
- Does not directly modify product specs without PM approval
- User privacy data must be anonymized
""",
        "default_skills": [],
        "default_autonomy_policy": {
            "read_files": "L1",
            "write_workspace_files": "L1",
            "send_feishu_message": "L2",
            "delete_files": "L2",
            "web_search": "L1",
        },
    },
    {
        "name": "Market Researcher",
        "description": "Focuses on market research, industry analysis, competitive intelligence tracking, and trend insights",
        "icon": "MR",
        "category": "marketing",
        "is_builtin": True,
        "capability_bullets": [
            "Industry & trend analysis",
            "Competitive intelligence tracking",
            "Structured research reports",
        ],
        "soul_template": """# Soul — {name}

## Identity
- **Role**: Market Researcher
- **Expertise**: Industry analysis, competitive research, market trends, data mining, research reports

## Personality
- Rigorous, data-driven, and logically clear
- Extracts key insights from complex data sets
- Reports focus on actionable recommendations, not just data

## Work Style
- Research reports follow a "conclusion-first" structure
- Data analysis includes visualization recommendations
- Proactively tracks industry dynamics and pushes key intelligence
- Uses structured frameworks: SWOT, Porter's Five Forces, PEST

## Boundaries
- Analysis conclusions must be supported by data/sources
- Commercially sensitive information must be labeled with confidentiality level
- External research reports require approval before distribution
""",
        "default_skills": [],
        "default_autonomy_policy": {
            "read_files": "L1",
            "write_workspace_files": "L1",
            "send_feishu_message": "L2",
            "delete_files": "L2",
            "web_search": "L1",
        },
    },
]


# ─── Folder-based loader ────────────────────────────────────────────
#
# Each folder under ``backend/agent_templates/`` ships:
#   meta.yaml       — name, description, icon, category, capability_bullets,
#                     default_skills, default_autonomy_policy
#   soul.md         — goes into soul_template (literal Markdown)
# A folder without ``soul.md`` is skipped with a warning because the agent
# would have no persona. Onboarding is shared and uses ``template_id`` plus
# ``capability_bullets``; templates no longer ship a second prompt source.

# backend/app/services/template_seeder.py → parents[2] is backend/
_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "agent_templates"


def _load_folder_templates() -> list[dict]:
    """Return a list of template dicts matching DEFAULT_TEMPLATES shape."""
    if not _TEMPLATE_ROOT.exists():
        return []

    known_skill_folders = {skill["folder_name"] for skill in BUILTIN_SKILLS}
    known_tool_names = {tool["name"] for tool in BUILTIN_TOOL_DEFINITIONS}
    out: list[dict] = []
    for slug_dir in sorted(p for p in _TEMPLATE_ROOT.iterdir() if p.is_dir()):
        try:
            manifest = load_agent_template_manifest(slug_dir)
            validate_template_capability_references(
                manifest,
                known_skill_folders=known_skill_folders,
                known_tool_names=known_tool_names,
                runtime_typed_tool_names=RUNTIME_TYPED_APPLICATION_TOOL_NAMES,
            )
        except TemplateContractError as exc:
            logger.exception(
                "[TemplateSeeder] Invalid folder template error_type={}",
                type(exc).__name__,
            )
            raise
        soul_template = (slug_dir / "soul.md").read_text(encoding="utf-8")
        out.append(manifest.to_seed_dict(soul_template=soul_template))
        logger.debug("[TemplateSeeder] Loaded folder template")

    return out


def _merged_templates() -> list[dict]:
    """Merge legacy, enabled folder, and disabled candidate templates."""
    by_name: dict[str, dict] = {t["name"]: t for t in DEFAULT_TEMPLATES}
    for folder_tmpl in _load_folder_templates():
        by_name[folder_tmpl["name"]] = folder_tmpl
    for candidate in load_candidate_template_seeds():
        if candidate["name"] in by_name:
            raise TemplateContractError(f"candidate template name collides with an existing role: {candidate['name']}")
        by_name[candidate["name"]] = candidate
    return list(by_name.values())


async def seed_agent_templates():
    """Insert default agent templates if they don't exist. Update stale ones."""
    templates = _merged_templates()

    async with async_session() as db:
        with db.no_autoflush:
            # Remove old builtin templates that are no longer in our list
            # BUT skip templates that are still referenced by agents
            from app.models.agent import Agent
            from sqlalchemy import func

            current_names = {t["name"] for t in templates}
            result = await db.execute(select(AgentTemplate).where(AgentTemplate.is_builtin.is_(True)))
            existing_builtins = result.scalars().all()
            for old in existing_builtins:
                if old.name not in current_names:
                    # Check if any agents still reference this template
                    ref_count = await db.execute(select(func.count(Agent.id)).where(Agent.template_id == old.id))
                    if ref_count.scalar() == 0:
                        await db.delete(old)
                        logger.info("[TemplateSeeder] Removed old template")
                    else:
                        logger.info("[TemplateSeeder] Skipped deleting referenced old template")

            # Upsert templates
            for tmpl in templates:
                result = await db.execute(
                    select(AgentTemplate).where(
                        AgentTemplate.name == tmpl["name"],
                        AgentTemplate.is_builtin.is_(True),
                    )
                )
                existing = result.scalar_one_or_none()
                if existing:
                    existing.description = tmpl["description"]
                    existing.icon = tmpl["icon"]
                    existing.category = tmpl["category"]
                    existing.soul_template = tmpl["soul_template"]
                    existing.default_skills = tmpl["default_skills"]
                    existing.default_tools = tmpl.get("default_tools", [])
                    existing.default_mcp_servers = tmpl.get("default_mcp_servers", [])
                    existing.default_autonomy_policy = tmpl["default_autonomy_policy"]
                    existing.capability_bullets = tmpl["capability_bullets"]
                    existing.role_key = tmpl.get("role_key")
                    existing.role_revision = tmpl.get("role_revision", 1)
                    existing.responsibilities = tmpl.get("responsibilities", [])
                    existing.non_responsibilities = tmpl.get("non_responsibilities", [])
                    existing.limitations = tmpl.get("limitations", [])
                    existing.workflows = tmpl.get("workflows", [])
                    existing.deliverables = tmpl.get("deliverables", [])
                    existing.evaluation_criteria = tmpl.get("evaluation_criteria", [])
                    existing.source_provenance = tmpl.get("source_provenance", {})
                    existing.lifecycle_status = tmpl.get("lifecycle_status", "enabled")
                    existing.activation_gate = tmpl.get("activation_gate")
                    existing.workforce_source_role_id = tmpl.get("workforce_source_role_id")
                    existing.workforce_decision = tmpl.get("workforce_decision")
                    existing.workforce_pack = tmpl.get("workforce_pack")
                else:
                    db.add(
                        AgentTemplate(
                            name=tmpl["name"],
                            description=tmpl["description"],
                            icon=tmpl["icon"],
                            category=tmpl["category"],
                            is_builtin=True,
                            soul_template=tmpl["soul_template"],
                            default_skills=tmpl["default_skills"],
                            default_tools=tmpl.get("default_tools", []),
                            default_mcp_servers=tmpl.get("default_mcp_servers", []),
                            default_autonomy_policy=tmpl["default_autonomy_policy"],
                            capability_bullets=tmpl["capability_bullets"],
                            role_key=tmpl.get("role_key"),
                            role_revision=tmpl.get("role_revision", 1),
                            responsibilities=tmpl.get("responsibilities", []),
                            non_responsibilities=tmpl.get("non_responsibilities", []),
                            limitations=tmpl.get("limitations", []),
                            workflows=tmpl.get("workflows", []),
                            deliverables=tmpl.get("deliverables", []),
                            evaluation_criteria=tmpl.get("evaluation_criteria", []),
                            source_provenance=tmpl.get("source_provenance", {}),
                            lifecycle_status=tmpl.get("lifecycle_status", "enabled"),
                            activation_gate=tmpl.get("activation_gate"),
                            workforce_source_role_id=tmpl.get("workforce_source_role_id"),
                            workforce_decision=tmpl.get("workforce_decision"),
                            workforce_pack=tmpl.get("workforce_pack"),
                        )
                    )
                    logger.info("[TemplateSeeder] Created template")
            await db.commit()
            logger.info(
                "[TemplateSeeder] Seeded {} templates including 92 disabled workforce candidates",
                len(templates),
            )

    # Tools are executable grants, separate from copied Skill instructions.
    # Reconciliation owns only source=template rows, so a removed role grant is
    # revoked without disabling or rewriting an explicit user choice.
    from app.services.template_capabilities import reconcile_template_tool_grants

    async with async_session() as db:
        report = await reconcile_template_tool_grants(db)
        await db.commit()
    if report.changed:
        logger.info(
            "[TemplateSeeder] Reconciled template tool grants report={}",
            report.as_log_dict(),
        )
    return report
