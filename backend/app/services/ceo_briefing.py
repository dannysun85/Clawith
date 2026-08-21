"""CEO business-panorama projection (FR-CEO-2).

Pure composition over existing read models — this module adds no new SQL:

- ``build_workforce_topology`` (one constant-query call) provides employee
  activity, in-flight work, and collaboration edges for the window.
- ``okr_reporting`` read functions provide OKR cadence status (existence and
  timestamps only, never full report bodies).

The assembled snapshot is hard-truncated to ``CEO_BRIEF_SNAPSHOT_MAX_CHARS``
with the priority order: blockers > in-progress work > output > activity.
The ``viewer`` only scopes visibility inside the caller's tenant; it never
widens the tenant boundary.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from functools import lru_cache

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.ceo import CeoOrchestratorSettings
from app.models.user import User
from app.services import okr_reporting
from app.services.workforce_topology import build_workforce_topology


DEFAULT_WINDOW_HOURS = 168
MAX_WINDOW_HOURS = 168
_ACTIVITY_PREVIEW_LIMIT = 10


@lru_cache(maxsize=32)
def _parse_exact_uuid_allowlist(raw: str, field_name: str) -> frozenset[str]:
    """Parse a rollout allowlist as canonical UUIDs and fail closed otherwise.

    Wildcards, labels, and malformed values are intentionally ignored.  The
    warning reports only a count so rollout configuration cannot leak through
    logs while still giving operators a deterministic diagnostic.
    """
    parsed: set[str] = set()
    invalid_count = 0
    for item in str(raw or "").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            parsed.add(str(uuid.UUID(value)))
        except (AttributeError, TypeError, ValueError):
            invalid_count += 1
    if invalid_count:
        logger.warning(
            "[CEO] Ignored {} non-UUID value(s) in {}; wildcard rollout is not supported",
            invalid_count,
            field_name,
        )
    return frozenset(parsed)


def ceo_orchestrator_allowed(
    *,
    tenant_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None = None,
    runtime_settings: Settings | None = None,
) -> bool:
    """Rollout gate: global switch AND (tenant OR agent allowlist hit).

    Same semantics as ``poster_v2_rollout_allowed`` — the "double allowlist" is
    a tenant hit OR an agent hit; both lists default to empty (fully closed).
    """
    s = runtime_settings or get_settings()
    if not s.CEO_ORCHESTRATOR_ENABLED:
        return False

    tenant_allowlist = _parse_exact_uuid_allowlist(
        s.CEO_ORCHESTRATOR_TENANT_IDS,
        "CEO_ORCHESTRATOR_TENANT_IDS",
    )
    agent_allowlist = _parse_exact_uuid_allowlist(
        s.CEO_ORCHESTRATOR_AGENT_IDS,
        "CEO_ORCHESTRATOR_AGENT_IDS",
    )
    if str(tenant_id) in tenant_allowlist:
        return True
    return agent_id is not None and str(agent_id) in agent_allowlist


def ceo_coordination_rollout_allowed(
    *,
    tenant_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None = None,
    runtime_settings: Settings | None = None,
) -> bool:
    """Return whether the independent P2 coordination canary is open.

    P2 can never widen the P1 boundary: both global gates and one matching
    allowlist entry for each layer are required.
    """
    s = runtime_settings or get_settings()
    if not ceo_orchestrator_allowed(
        tenant_id=tenant_id,
        agent_id=agent_id,
        runtime_settings=s,
    ):
        return False
    if not s.CEO_COORDINATION_ENABLED:
        return False

    tenant_allowlist = _parse_exact_uuid_allowlist(
        s.CEO_COORDINATION_TENANT_IDS,
        "CEO_COORDINATION_TENANT_IDS",
    )
    agent_allowlist = _parse_exact_uuid_allowlist(
        s.CEO_COORDINATION_AGENT_IDS,
        "CEO_COORDINATION_AGENT_IDS",
    )
    if str(tenant_id) in tenant_allowlist:
        return True
    return agent_id is not None and str(agent_id) in agent_allowlist


def ceo_coordination_allowed(
    settings_row: CeoOrchestratorSettings | None,
    *,
    runtime_settings: Settings | None = None,
) -> bool:
    """Fail-closed P2 authority check for one persisted company CEO."""
    if settings_row is None:
        return False
    if not bool(getattr(settings_row, "enabled", False)):
        return False
    if not bool(getattr(settings_row, "coordination_enabled", False)):
        return False
    return ceo_coordination_rollout_allowed(
        tenant_id=settings_row.tenant_id,
        agent_id=settings_row.ceo_agent_id,
        runtime_settings=runtime_settings,
    )


def ceo_operating_mode(
    settings_row: CeoOrchestratorSettings | None,
    *,
    runtime_settings: Settings | None = None,
) -> str:
    """Derive the authoritative CEO operating mode from gates plus row state."""
    if settings_row is None or not bool(getattr(settings_row, "enabled", False)):
        return "disabled"
    if not ceo_orchestrator_allowed(
        tenant_id=settings_row.tenant_id,
        agent_id=settings_row.ceo_agent_id,
        runtime_settings=runtime_settings,
    ):
        return "disabled"
    if not ceo_coordination_allowed(settings_row, runtime_settings=runtime_settings):
        return "observer"
    if bool(getattr(settings_row, "auto_dispatch_enabled", False)):
        return "coordinator_auto"
    return "coordinator"


async def maybe_attach_ceo_brief_snapshot(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    trigger_config: dict | None,
    context: str,
) -> str:
    """Append the read-only panorama to a CEO trigger run's input context.

    Best-effort augmentation: any failure logs and returns the original
    context so trigger firing is never broken by snapshot composition.
    """
    config = trigger_config if isinstance(trigger_config, dict) else {}
    if not config.get("attach_brief_snapshot"):
        return context
    try:
        result = await db.execute(
            select(CeoOrchestratorSettings).where(
                CeoOrchestratorSettings.ceo_agent_id == agent_id,
                CeoOrchestratorSettings.tenant_id == tenant_id,
            )
        )
        settings_row = result.scalar_one_or_none()
        if settings_row is None or not settings_row.enabled:
            return context
        if not ceo_orchestrator_allowed(tenant_id=tenant_id, agent_id=agent_id):
            return context
        snapshot = await build_company_brief_snapshot(
            db,
            tenant_id=tenant_id,
            viewer_user_id=settings_row.enabled_by_user_id,
        )
        rendered = snapshot.render_markdown(
            max_chars=get_settings().CEO_BRIEF_SNAPSHOT_MAX_CHARS
        )
        return f"{context}\n\n## 业务全景快照（只读）\n{rendered}"
    except Exception as exc:
        logger.warning(
            "[CEO] brief snapshot attach skipped agent_id={} error_type={}",
            agent_id,
            type(exc).__name__,
        )
        return context


class CeoBriefingError(ValueError):
    """Raised when the panorama cannot be composed for this tenant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BriefWorkItem(BaseModel):
    agent_name: str
    title: str
    stage: str


class BriefReportRef(BaseModel):
    report_type: str
    period_start: str
    period_end: str
    updated_at: str | None = None
    needs_refresh: bool = False


class CompanyBriefSnapshot(BaseModel):
    """Bounded, renderable business panorama for the CEO role."""

    company_name: str
    window_hours: int = Field(ge=1, le=MAX_WINDOW_HOURS)
    generated_at: datetime
    employee_total: int = 0
    employee_active_in_window: int = 0
    work_executing: int = 0
    work_review: int = 0
    work_approval: int = 0
    work_blocked: int = 0
    work_completed_recent: int = 0
    blocked_items: list[BriefWorkItem] = Field(default_factory=list)
    in_progress_items: list[BriefWorkItem] = Field(default_factory=list)
    okr_tracked_members: int = 0
    okr_reports_today_submitted: int = 0
    okr_reports_today_missing: int = 0
    latest_daily_report: BriefReportRef | None = None
    latest_weekly_report: BriefReportRef | None = None
    recent_activities: list[str] = Field(default_factory=list)
    truncated: bool = False

    def render_markdown(self, *, max_chars: int) -> str:
        """Render with priority trimming: blockers > in-progress > output > activity."""
        lines = [
            f"# Company brief snapshot — {self.company_name}",
            f"Window: last {self.window_hours}h · Generated: {self.generated_at.isoformat()}",
            "",
            "## OKR cadence",
            (
                f"- Tracked members: {self.okr_tracked_members}; "
                f"daily reports today: {self.okr_reports_today_submitted} submitted, "
                f"{self.okr_reports_today_missing} missing"
            ),
        ]
        if self.latest_daily_report is not None:
            lines.append(
                f"- Latest daily report: {self.latest_daily_report.period_start}"
                + (" (needs refresh)" if self.latest_daily_report.needs_refresh else "")
            )
        else:
            lines.append("- Latest daily report: none")
        if self.latest_weekly_report is not None:
            lines.append(
                f"- Latest weekly report: {self.latest_weekly_report.period_start}"
                f" ~ {self.latest_weekly_report.period_end}"
                + (" (needs refresh)" if self.latest_weekly_report.needs_refresh else "")
            )
        else:
            lines.append("- Latest weekly report: none")

        def _section(title: str, items: list[str]) -> list[str]:
            if not items:
                return []
            return ["", title, *[f"- {item}" for item in items]]

        blocker_lines = [
            f"{item.agent_name}: {item.title} [{item.stage}]" for item in self.blocked_items
        ]
        progress_lines = [
            f"{item.agent_name}: {item.title} [{item.stage}]" for item in self.in_progress_items
        ]
        output_lines = [
            (
                f"Workforce: {self.employee_total} employees, "
                f"{self.employee_active_in_window} active in window; "
                f"work executing={self.work_executing} review={self.work_review} "
                f"approval={self.work_approval} blocked={self.work_blocked} "
                f"completed_recent={self.work_completed_recent}"
            )
        ]
        activity_lines = list(self.recent_activities)

        for section in (
            _section("## Blockers", blocker_lines),
            _section("## In progress", progress_lines),
            _section("## Output & workforce", output_lines),
            _section("## Recent activity", activity_lines),
        ):
            candidate = lines + section
            rendered = "\n".join(candidate)
            if len(rendered) > max_chars:
                omitted = len(section) - 1 if section else 0
                if section:
                    lines.append("")
                    lines.append(f"… section '{section[1]}' omitted ({omitted} lines) — snapshot truncated")
                self.truncated = True
                break
            lines = candidate
        return "\n".join(lines)


async def _resolve_viewer(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    viewer_user_id: uuid.UUID | None,
) -> User:
    """Pick the panorama viewer: the enabler first, then any active org_owner.

    The viewer only affects per-agent visibility inside this tenant; the tenant
    boundary itself is fixed by ``tenant_id``.
    """
    if viewer_user_id is not None:
        result = await db.execute(
            select(User).where(
                User.id == viewer_user_id,
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
            )
        )
        viewer = result.scalar_one_or_none()
        if viewer is not None:
            return viewer
    fallback = await db.execute(
        select(User)
        .where(
            User.tenant_id == tenant_id,
            User.role == "org_owner",
            User.is_active.is_(True),
        )
        .limit(1)
    )
    viewer = fallback.scalar_one_or_none()
    if viewer is None:
        raise CeoBriefingError(
            "viewer_unavailable",
            "No active viewer (enabler or company owner) for the CEO snapshot",
        )
    return viewer


def _report_ref(report: object | None, *, report_type: str) -> BriefReportRef | None:
    if report is None:
        return None
    updated = getattr(report, "updated_at", None)
    return BriefReportRef(
        report_type=report_type,
        period_start=str(getattr(report, "period_start", "")),
        period_end=str(getattr(report, "period_end", "")),
        updated_at=updated.isoformat() if isinstance(updated, datetime) else None,
        needs_refresh=bool(getattr(report, "needs_refresh", False)),
    )


async def build_company_brief_snapshot(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    viewer_user_id: uuid.UUID | None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_chars: int | None = None,
) -> CompanyBriefSnapshot:
    """Compose the bounded CEO panorama from existing read models only."""
    window = max(1, min(int(window_hours or DEFAULT_WINDOW_HOURS), MAX_WINDOW_HOURS))
    limit = max_chars or get_settings().CEO_BRIEF_SNAPSHOT_MAX_CHARS
    viewer = await _resolve_viewer(db, tenant_id=tenant_id, viewer_user_id=viewer_user_id)

    topology = await build_workforce_topology(db, user=viewer, window_hours=window)

    tracked_members = await okr_reporting.list_tracked_okr_members(tenant_id)
    today_reports = await okr_reporting.list_member_daily_reports_for_date(
        tenant_id,
        date.today(),
    )
    daily_reports = await okr_reporting.list_company_reports(tenant_id, report_type="daily", limit=1)
    weekly_reports = await okr_reporting.list_company_reports(tenant_id, report_type="weekly", limit=1)

    snapshot = CompanyBriefSnapshot(
        company_name=topology.company_name,
        window_hours=window,
        generated_at=datetime.now(timezone.utc),
        employee_total=len(topology.nodes),
        okr_tracked_members=len(tracked_members),
        okr_reports_today_submitted=sum(
            1 for item in today_reports if item.get("status") in {"submitted", "revised", "late"}
        ),
        okr_reports_today_missing=sum(1 for item in today_reports if item.get("status") == "missing"),
        latest_daily_report=_report_ref(daily_reports[0] if daily_reports else None, report_type="daily"),
        latest_weekly_report=_report_ref(weekly_reports[0] if weekly_reports else None, report_type="weekly"),
    )

    activity_names = {node.id: node.name for node in topology.nodes}
    for node in topology.nodes:
        if node.last_active_at is not None:
            snapshot.employee_active_in_window += 1
        work = node.work
        if work is None:
            continue
        item = BriefWorkItem(agent_name=node.name, title=work.title, stage=work.stage)
        if work.stage == "blocked":
            snapshot.work_blocked += work.active_count or 1
            snapshot.blocked_items.append(item)
        elif work.stage in {"executing", "review", "approval"}:
            if work.stage == "executing":
                snapshot.work_executing += work.active_count or 1
            elif work.stage == "review":
                snapshot.work_review += work.active_count or 1
            else:
                snapshot.work_approval += work.active_count or 1
            snapshot.in_progress_items.append(item)
        elif work.stage == "completed":
            snapshot.work_completed_recent += work.recently_completed_count or 1

    for activity in topology.recent_activities[:_ACTIVITY_PREVIEW_LIMIT]:
        name = activity_names.get(activity.agent_id, str(activity.agent_id))
        snapshot.recent_activities.append(f"{name}: {activity.summary}")

    snapshot.render_markdown(max_chars=limit)
    return snapshot


__all__ = [
    "CeoBriefingError",
    "CompanyBriefSnapshot",
    "build_company_brief_snapshot",
    "ceo_coordination_allowed",
    "ceo_coordination_rollout_allowed",
    "ceo_operating_mode",
    "ceo_orchestrator_allowed",
    "maybe_attach_ceo_brief_snapshot",
]
