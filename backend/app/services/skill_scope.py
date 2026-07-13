"""Tenant-aware visibility and override rules for registered skills."""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import or_

from app.models.skill import Skill


def skill_visibility_clause(tenant_id: uuid.UUID | None):
    """Return the SQL predicate for the global + current-tenant skill view."""
    if tenant_id is None:
        return Skill.tenant_id.is_(None)
    return or_(Skill.tenant_id.is_(None), Skill.tenant_id == tenant_id)


def scope_skill_query(query: Any, tenant_id: uuid.UUID | None):
    """Apply the tenant skill visibility contract to a SQLAlchemy query."""
    return query.where(skill_visibility_clause(tenant_id))


def prefer_tenant_skill_overrides(
    skills: Iterable[Skill],
    tenant_id: uuid.UUID | None,
) -> list[Skill]:
    """Return one visible skill per folder, preferring the tenant override."""
    by_folder: dict[str, Skill] = {}
    for skill in skills:
        owner_id = skill.tenant_id
        if owner_id is not None and owner_id != tenant_id:
            continue
        if tenant_id is None and owner_id is not None:
            continue

        existing = by_folder.get(skill.folder_name)
        if existing is None or owner_id == tenant_id:
            by_folder[skill.folder_name] = skill

    return sorted(
        by_folder.values(),
        key=lambda skill: (skill.name.casefold(), skill.folder_name.casefold()),
    )


def resolve_agent_skills(
    skills: Iterable[Skill],
    tenant_id: uuid.UUID | None,
    *,
    selected_ids: Iterable[uuid.UUID] = (),
    template_folders: Iterable[str] = (),
) -> list[Skill]:
    """Resolve effective default, explicitly selected, and template skills."""
    visible = [
        skill
        for skill in skills
        if skill.tenant_id is None or skill.tenant_id == tenant_id
    ]
    selected_id_set = set(selected_ids)
    selected_folders = set(template_folders)
    selected_folders.update(
        skill.folder_name
        for skill in visible
        if skill.is_default or skill.id in selected_id_set
    )

    return [
        skill
        for skill in prefer_tenant_skill_overrides(visible, tenant_id)
        if skill.folder_name in selected_folders
    ]
