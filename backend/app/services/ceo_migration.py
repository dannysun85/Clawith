"""Read-only discovery for upgrading legacy CEO-like Agents.

The scanner deliberately returns counts and structural metadata only. It never
reads message text, Memory content, trigger configuration, Tool configuration,
or credentials, and it never changes tenant data. Any later adoption/archive
operation must consume a separately reviewed migration manifest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentTemplate
from app.models.audit import ChatMessage
from app.models.ceo import CeoOrchestratorSettings
from app.models.chat_session import ChatSession
from app.models.org import AgentAgentRelationship
from app.models.tool import AgentTool
from app.models.trigger import AgentTrigger
from app.models.workspace import WorkspaceFileRevision
from app.services.storage import agent_storage_key, get_storage_backend


CEO_MIGRATION_SCHEMA_VERSION = "1.12.0"

_CEO_EXACT_NAMES = frozenset(
    {
        "ceo",
        "companyceo",
        "corporateceo",
        "chiefexecutiveofficer",
        "公司ceo",
        "企业ceo",
        "首席执行官",
        "首席执行官ceo",
    }
)


@dataclass(frozen=True)
class LegacyCeoEvidence:
    agent_id: uuid.UUID
    name: str
    is_system: bool
    template_role_key: str | None = None
    session_count: int = 0
    message_count: int = 0
    active_trigger_count: int = 0
    control_plane_revision_count: int = 0
    control_plane_bytes: int = 0
    enabled_tool_count: int = 0
    relationship_count: int = 0
    has_last_activity: bool = False

    @property
    def has_behavioral_history(self) -> bool:
        return any(
            (
                self.session_count,
                self.message_count,
                self.active_trigger_count,
                self.control_plane_revision_count,
                self.control_plane_bytes,
                int(self.has_last_activity),
            )
        )

    def public_dict(self) -> dict:
        payload = asdict(self)
        payload["agent_id"] = str(self.agent_id)
        payload["has_behavioral_history"] = self.has_behavioral_history
        return payload


def _compact_identity_text(value: str | None) -> str:
    return re.sub(r"[\s._\-/]+", "", str(value or "").strip().lower())


def is_legacy_ceo_candidate(
    *,
    name: str,
    role_description: str | None,
    bio: str | None,
    template_role_key: str | None,
) -> bool:
    """Conservatively identify records requiring a governor's CEO review."""
    if str(template_role_key or "").strip().lower() == "ceo":
        return True
    if _compact_identity_text(name) in _CEO_EXACT_NAMES:
        return True

    role_text = f"{role_description or ''}\n{bio or ''}".lower()
    return bool(
        re.search(r"\bceo\b", role_text)
        or "chief executive officer" in role_text
        or "首席执行官" in role_text
    )


def classify_ceo_migration_state(
    *,
    formal_ceo_agent_id: uuid.UUID | None,
    candidates: list[LegacyCeoEvidence],
) -> tuple[str, str, list[str]]:
    """Return classification, recommended action, and non-mutating warnings."""
    if formal_ceo_agent_id is not None:
        extra_candidates = [
            item for item in candidates if item.agent_id != formal_ceo_agent_id
        ]
        warnings = (
            ["Additional CEO-like Agents require manual duplicate review."]
            if extra_candidates
            else []
        )
        return (
            "formal_ceo",
            "Keep the formal CEO. Review any additional CEO-like Agents without merging history.",
            warnings,
        )
    if not candidates:
        return (
            "none",
            "Offer explicit CEO enablement; do not create or activate one automatically.",
            [],
        )
    if len(candidates) > 1:
        return (
            "ambiguous_manual_review",
            "Require a company governor to select a disposition for each candidate before any change.",
            ["Multiple CEO-like Agents were found; automatic adoption is unsafe."],
        )

    candidate = candidates[0]
    if candidate.has_behavioral_history:
        return (
            "legacy_contaminated_archive",
            "Create a clean formal CEO, migrate only approved structural grants, then archive the legacy Agent.",
            ["Conversation, trigger, activity, or control-plane history must not be copied into the clean CEO."],
        )
    return (
        "legacy_clean_adoptable",
        "Generate a review manifest for governed adoption; activation still requires explicit approval.",
        ["Clean classification permits review, not automatic adoption."],
    )


async def _grouped_counts(db: AsyncSession, statement) -> dict[uuid.UUID, int]:
    result = await db.execute(statement)
    return {agent_id: int(count or 0) for agent_id, count in result.all()}


async def _control_plane_bytes(agent_id: uuid.UUID) -> int:
    storage = get_storage_backend()
    total = 0
    for path in ("soul.md", "memory.md", "memory/memory.md"):
        try:
            version = await storage.get_version(agent_storage_key(agent_id, path))
        except Exception:
            continue
        if version.exists and not version.is_dir:
            total += max(0, int(version.size or 0))
    return total


async def build_ceo_migration_preview(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> dict:
    """Build a tenant-scoped, secret-free, zero-mutation CEO migration preview."""
    settings_result = await db.execute(
        select(CeoOrchestratorSettings).where(
            CeoOrchestratorSettings.tenant_id == tenant_id
        )
    )
    settings = settings_result.scalar_one_or_none()
    formal_ceo_agent_id = settings.ceo_agent_id if settings is not None else None

    agents_result = await db.execute(
        select(Agent, AgentTemplate.role_key)
        .outerjoin(AgentTemplate, Agent.template_id == AgentTemplate.id)
        .where(
            Agent.tenant_id == tenant_id,
            Agent.deleted_at.is_(None),
        )
    )
    candidate_rows = [
        (agent, template_role_key)
        for agent, template_role_key in agents_result.all()
        if is_legacy_ceo_candidate(
            name=agent.name,
            role_description=agent.role_description,
            bio=agent.bio,
            template_role_key=template_role_key,
        )
        or agent.id == formal_ceo_agent_id
    ]
    candidate_ids = [agent.id for agent, _role_key in candidate_rows]

    session_counts: dict[uuid.UUID, int] = {}
    message_counts: dict[uuid.UUID, int] = {}
    trigger_counts: dict[uuid.UUID, int] = {}
    revision_counts: dict[uuid.UUID, int] = {}
    tool_counts: dict[uuid.UUID, int] = {}
    relationship_counts = {agent_id: 0 for agent_id in candidate_ids}
    if candidate_ids:
        session_counts = await _grouped_counts(
            db,
            select(ChatSession.agent_id, func.count(ChatSession.id))
            .where(
                ChatSession.agent_id.in_(candidate_ids),
                ChatSession.deleted_at.is_(None),
            )
            .group_by(ChatSession.agent_id),
        )
        message_counts = await _grouped_counts(
            db,
            select(ChatMessage.agent_id, func.count(ChatMessage.id))
            .where(ChatMessage.agent_id.in_(candidate_ids))
            .group_by(ChatMessage.agent_id),
        )
        trigger_counts = await _grouped_counts(
            db,
            select(AgentTrigger.agent_id, func.count(AgentTrigger.id))
            .where(
                AgentTrigger.agent_id.in_(candidate_ids),
                AgentTrigger.is_enabled.is_(True),
            )
            .group_by(AgentTrigger.agent_id),
        )
        normalized_revision_path = func.lower(WorkspaceFileRevision.path)
        revision_counts = await _grouped_counts(
            db,
            select(
                WorkspaceFileRevision.agent_id,
                func.count(WorkspaceFileRevision.id),
            )
            .where(
                WorkspaceFileRevision.agent_id.in_(candidate_ids),
                or_(
                    normalized_revision_path.in_(("soul.md", "memory.md", "memory")),
                    normalized_revision_path.like("memory/%"),
                ),
            )
            .group_by(WorkspaceFileRevision.agent_id),
        )
        tool_counts = await _grouped_counts(
            db,
            select(AgentTool.agent_id, func.count(AgentTool.id))
            .where(
                AgentTool.agent_id.in_(candidate_ids),
                AgentTool.enabled.is_(True),
            )
            .group_by(AgentTool.agent_id),
        )
        relationship_result = await db.execute(
            select(
                AgentAgentRelationship.agent_id,
                AgentAgentRelationship.target_agent_id,
            ).where(
                or_(
                    AgentAgentRelationship.agent_id.in_(candidate_ids),
                    AgentAgentRelationship.target_agent_id.in_(candidate_ids),
                )
            )
        )
        for source_id, target_id in relationship_result.all():
            if source_id in relationship_counts:
                relationship_counts[source_id] += 1
            if target_id in relationship_counts and target_id != source_id:
                relationship_counts[target_id] += 1

    candidates: list[LegacyCeoEvidence] = []
    for agent, template_role_key in candidate_rows:
        candidates.append(
            LegacyCeoEvidence(
                agent_id=agent.id,
                name=agent.name,
                is_system=bool(agent.is_system),
                template_role_key=template_role_key,
                session_count=session_counts.get(agent.id, 0),
                message_count=message_counts.get(agent.id, 0),
                active_trigger_count=trigger_counts.get(agent.id, 0),
                control_plane_revision_count=revision_counts.get(agent.id, 0),
                control_plane_bytes=await _control_plane_bytes(agent.id),
                enabled_tool_count=tool_counts.get(agent.id, 0),
                relationship_count=relationship_counts.get(agent.id, 0),
                has_last_activity=agent.last_active_at is not None,
            )
        )

    classification, recommended_action, warnings = classify_ceo_migration_state(
        formal_ceo_agent_id=formal_ceo_agent_id,
        candidates=candidates,
    )
    return {
        "schema_version": CEO_MIGRATION_SCHEMA_VERSION,
        "mode": "dry_run",
        "tenant_id": str(tenant_id),
        "classification": classification,
        "formal_ceo_agent_id": (
            str(formal_ceo_agent_id) if formal_ceo_agent_id is not None else None
        ),
        "candidates": [candidate.public_dict() for candidate in candidates],
        "recommended_action": recommended_action,
        "warnings": warnings,
        "safeguards": {
            "mutates_data": False,
            "includes_message_content": False,
            "includes_memory_content": False,
            "includes_trigger_or_tool_config": False,
            "automatic_adoption_allowed": False,
            "automatic_archive_allowed": False,
        },
    }
