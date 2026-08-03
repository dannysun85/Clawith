"""Strict, versioned contract for folder-based Agent role templates.

The database still stores the runtime fields used by ``AgentTemplate``.  This
module governs the repository manifest that produces those fields so a role
cannot silently reference a missing Skill or a catalog-only Tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AUTONOMY_LEVELS = frozenset({"L1", "L2", "L3"})
TEMPLATE_LIFECYCLE_ENABLED = "enabled"
TEMPLATE_LIFECYCLE_STATUSES = frozenset({"enabled", "candidate_disabled", "conditional_disabled", "not_recruitable"})
WORKFORCE_DECISIONS = frozenset({"upgrade_existing", "add_candidate", "conditional_pack", "merge_or_reject"})
TemplateLifecycle = Literal[
    "enabled",
    "candidate_disabled",
    "conditional_disabled",
    "not_recruitable",
]
TemplateWorkforceDecision = Literal[
    "upgrade_existing",
    "add_candidate",
    "conditional_pack",
    "merge_or_reject",
]
SUPPORTED_TEMPLATE_CATEGORIES = frozenset(
    {
        "software-development",
        "product-project",
        "marketing",
        "data-research",
        "customer-success",
        "office",
        "trading",
    }
)


class TemplateContractError(ValueError):
    """Raised when a repository Agent template cannot be made executable."""


class TemplateSourceProvenance(BaseModel):
    """Pinned upstream evidence for an adapted third-party role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    paths: list[str] = Field(min_length=1)
    license: str = Field(min_length=1)
    adaptation: str = Field(min_length=1)

    @field_validator("paths")
    @classmethod
    def _validate_paths(cls, values: list[str]) -> list[str]:
        return _normalize_unique_strings(values, field_name="source_provenance.paths")


class AgentTemplateManifest(BaseModel):
    """Repository source-of-truth for one folder-based Agent role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=2)
    role_key: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    role_revision: int = Field(default=1, ge=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    icon: str = Field(min_length=1, max_length=8)
    category: str
    capability_bullets: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    non_responsibilities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    evaluation_criteria: list[str] = Field(default_factory=list)
    default_skills: list[str] = Field(default_factory=list)
    default_tools: list[str] = Field(default_factory=list)
    default_mcp_servers: list[str] = Field(default_factory=list)
    default_autonomy_policy: dict[str, str] = Field(default_factory=dict)
    source_provenance: TemplateSourceProvenance | None = None
    lifecycle_status: TemplateLifecycle = TEMPLATE_LIFECYCLE_ENABLED
    activation_gate: str | None = None
    workforce_source_role_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    workforce_decision: TemplateWorkforceDecision | None = None
    workforce_pack: str | None = None

    @field_validator(
        "capability_bullets",
        "responsibilities",
        "non_responsibilities",
        "limitations",
        "workflows",
        "deliverables",
        "evaluation_criteria",
        "default_skills",
        "default_tools",
        "default_mcp_servers",
    )
    @classmethod
    def _validate_string_lists(
        cls,
        values: list[str],
        info: Any,
    ) -> list[str]:
        return _normalize_unique_strings(values, field_name=info.field_name)

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in SUPPORTED_TEMPLATE_CATEGORIES:
            supported = ", ".join(sorted(SUPPORTED_TEMPLATE_CATEGORIES))
            raise ValueError(f"unsupported category {normalized!r}; expected one of: {supported}")
        return normalized

    @field_validator("default_autonomy_policy")
    @classmethod
    def _validate_autonomy_policy(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for action, level in value.items():
            action_name = action.strip()
            if not action_name:
                raise ValueError("default_autonomy_policy contains a blank action")
            if level not in AUTONOMY_LEVELS:
                raise ValueError(f"default_autonomy_policy[{action_name!r}] must be L1, L2, or L3")
            normalized[action_name] = level
        return normalized

    @model_validator(mode="after")
    def _validate_versioned_fields(self) -> "AgentTemplateManifest":
        if self.schema_version < 2:
            return self
        missing: list[str] = []
        if not self.role_key:
            missing.append("role_key")
        if not self.responsibilities:
            missing.append("responsibilities")
        if not self.non_responsibilities:
            missing.append("non_responsibilities")
        if not self.workflows:
            missing.append("workflows")
        if not self.deliverables:
            missing.append("deliverables")
        if not self.evaluation_criteria:
            missing.append("evaluation_criteria")
        if self.source_provenance is None:
            missing.append("source_provenance")
        if missing:
            raise ValueError("schema_version=2 requires " + ", ".join(missing))
        if self.workforce_decision in {"upgrade_existing", "add_candidate"}:
            if not self.workforce_source_role_id:
                raise ValueError(f"workforce_decision={self.workforce_decision} requires workforce_source_role_id")
        expected_lifecycle = {
            "upgrade_existing": "enabled",
            "add_candidate": "candidate_disabled",
            "conditional_pack": "conditional_disabled",
            "merge_or_reject": "not_recruitable",
        }.get(self.workforce_decision)
        if expected_lifecycle and self.lifecycle_status != expected_lifecycle:
            raise ValueError(
                f"workforce_decision={self.workforce_decision} requires lifecycle_status={expected_lifecycle}"
            )
        if self.lifecycle_status != TEMPLATE_LIFECYCLE_ENABLED and not self.activation_gate:
            raise ValueError("disabled template lifecycle requires activation_gate")
        return self

    def to_seed_dict(self, *, soul_template: str) -> dict[str, Any]:
        """Return the reviewed fields persisted by ``AgentTemplate``."""
        return {
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "category": self.category,
            "is_builtin": True,
            "capability_bullets": self.capability_bullets,
            "soul_template": soul_template,
            "default_skills": self.default_skills,
            "default_tools": self.default_tools,
            "default_mcp_servers": self.default_mcp_servers,
            "default_autonomy_policy": self.default_autonomy_policy,
            "role_key": self.role_key,
            "role_revision": self.role_revision,
            "responsibilities": self.responsibilities,
            "non_responsibilities": self.non_responsibilities,
            "limitations": self.limitations,
            "workflows": self.workflows,
            "deliverables": self.deliverables,
            "evaluation_criteria": self.evaluation_criteria,
            "source_provenance": (self.source_provenance.model_dump() if self.source_provenance else {}),
            "lifecycle_status": self.lifecycle_status,
            "activation_gate": self.activation_gate,
            "workforce_source_role_id": self.workforce_source_role_id,
            "workforce_decision": self.workforce_decision,
            "workforce_pack": self.workforce_pack,
        }


def _normalize_unique_strings(values: list[str], *, field_name: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value:
            raise ValueError(f"{field_name} contains a blank value")
        if value in seen:
            raise ValueError(f"{field_name} contains duplicate value {value!r}")
        seen.add(value)
        normalized.append(value)
    return normalized


def load_agent_template_manifest(slug_dir: Path) -> AgentTemplateManifest:
    """Load and structurally validate one template folder."""
    meta_path = slug_dir / "meta.yaml"
    soul_path = slug_dir / "soul.md"
    if not meta_path.is_file():
        raise TemplateContractError(f"{slug_dir.name}: missing meta.yaml")
    if not soul_path.is_file():
        raise TemplateContractError(f"{slug_dir.name}: missing soul.md")

    try:
        raw = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateContractError(f"{slug_dir.name}: invalid meta.yaml: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise TemplateContractError(f"{slug_dir.name}: meta.yaml must be a mapping")

    try:
        manifest = AgentTemplateManifest.model_validate(raw)
    except ValueError as exc:
        raise TemplateContractError(f"{slug_dir.name}: {exc}") from exc
    if manifest.schema_version >= 2 and manifest.role_key != slug_dir.name:
        raise TemplateContractError(f"{slug_dir.name}: role_key must match its template folder")

    soul_template = soul_path.read_text(encoding="utf-8").strip()
    if not soul_template:
        raise TemplateContractError(f"{slug_dir.name}: soul.md must not be empty")
    return manifest


def validate_template_capability_references(
    manifest: AgentTemplateManifest,
    *,
    known_skill_folders: set[str] | frozenset[str],
    known_tool_names: set[str] | frozenset[str],
    runtime_typed_tool_names: set[str] | frozenset[str],
) -> None:
    """Fail closed when a role declares capability the runtime cannot provide."""
    missing_skills = sorted(set(manifest.default_skills) - set(known_skill_folders))
    missing_tools = sorted(set(manifest.default_tools) - set(known_tool_names))
    untyped_tools = sorted(set(manifest.default_tools) & set(known_tool_names) - set(runtime_typed_tool_names))
    problems: list[str] = []
    if missing_skills:
        problems.append(f"unknown Skills={missing_skills}")
    if missing_tools:
        problems.append(f"unknown Tools={missing_tools}")
    if untyped_tools:
        problems.append(f"Tools without Durable Runtime adapters={untyped_tools}")
    if problems:
        key = manifest.role_key or manifest.name
        raise TemplateContractError(f"{key}: " + "; ".join(problems))


__all__ = [
    "AUTONOMY_LEVELS",
    "AgentTemplateManifest",
    "SUPPORTED_TEMPLATE_CATEGORIES",
    "TemplateContractError",
    "TemplateSourceProvenance",
    "load_agent_template_manifest",
    "validate_template_capability_references",
]
