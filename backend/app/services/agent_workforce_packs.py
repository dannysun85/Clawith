"""Activation contracts for conditional workforce packs and rejected roles."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.agent_workforce_catalog import (
    WorkforceRoleRecord,
    workforce_records_by_decision,
)


class ConditionalPackContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pack: str
    display_name: str
    risk_level: Literal["medium", "high", "restricted"]
    required_context: tuple[str, ...] = Field(min_length=1)
    required_capabilities: tuple[str, ...] = Field(min_length=1)
    activation_criteria: tuple[str, ...] = Field(min_length=1)
    forbidden_defaults: tuple[str, ...] = Field(min_length=1)


class WorkforceConditionalRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    packs: tuple[ConditionalPackContract, ...]
    conditional_roles: tuple[WorkforceRoleRecord, ...]
    resolutions: tuple[WorkforceRoleRecord, ...]

    @model_validator(mode="after")
    def validate_complete_registry(self) -> "WorkforceConditionalRegistry":
        pack_names = [pack.pack for pack in self.packs]
        if len(pack_names) != len(set(pack_names)):
            raise ValueError("conditional registry contains duplicate packs")
        role_pack_names = {record.pack for record in self.conditional_roles}
        if role_pack_names != set(pack_names):
            raise ValueError("conditional pack contracts do not cover every catalog pack")
        if len(self.conditional_roles) != 142:
            raise ValueError("conditional registry must contain exactly 142 roles")
        if len(self.resolutions) != 15:
            raise ValueError("resolution registry must contain exactly 15 roles")
        return self

    def pack_counts(self) -> dict[str, int]:
        return dict(Counter(record.pack for record in self.conditional_roles))

    def resolution_counts(self) -> dict[str, int]:
        return dict(Counter(record.resolution for record in self.resolutions))


def _pack(
    name: str,
    display_name: str,
    risk_level: Literal["medium", "high", "restricted"],
    context: tuple[str, ...],
    capabilities: tuple[str, ...],
    criteria: tuple[str, ...],
) -> ConditionalPackContract:
    return ConditionalPackContract(
        pack=name,
        display_name=display_name,
        risk_level=risk_level,
        required_context=context,
        required_capabilities=capabilities,
        activation_criteria=criteria,
        forbidden_defaults=(
            "No Talent Market visibility before explicit promotion",
            "No Tool, MCP, credential, Provider, or private-data grant by prompt implication",
            "No external action, spend, regulated decision, or production mutation without approval and receipt",
        ),
    )


_PACKS = (
    _pack(
        "vertical-engineering",
        "垂直工程能力包",
        "high",
        ("Authorized repository or runtime", "Target-specific test environment"),
        ("Registered engineering Skill", "Sandboxed execution Tool", "Rollback and verification path"),
        ("Role-specific eval passes", "Runtime adapter is ready", "Security boundary is reviewed"),
    ),
    _pack(
        "channel-operations",
        "平台运营能力包",
        "high",
        ("Official account authorization", "Approved brand facts and assets"),
        ("Official platform integration", "Draft and media Tools", "Publish approval and receipt"),
        ("Read-only analysis works", "Draft quality eval passes", "Publish path is official and approval-gated"),
    ),
    _pack(
        "paid-media",
        "付费投放能力包",
        "restricted",
        ("Named ad account", "Approved budget and campaign policy"),
        ("Official ads API", "Budget guard", "Two-step approval and spend receipt"),
        ("Shadow analysis passes", "Hard budget caps are enforced", "Kill switch is verified"),
    ),
    _pack(
        "regulated-finance",
        "受监管金融能力包",
        "restricted",
        ("Authorized financial data", "Entity, jurisdiction, period, and accounting policy"),
        ("Reproducible analysis Tool", "Source lineage", "Human financial review"),
        ("Calculation eval passes", "Assumptions are traceable", "No payment or trade authority is granted"),
    ),
    _pack(
        "legal",
        "法务能力包",
        "restricted",
        ("Jurisdiction", "Current authoritative legal sources", "Named accountable counsel"),
        ("Versioned source retrieval", "Citation and redline support", "Mandatory legal review"),
        ("Currency of law is verified", "Legal disclaimer is explicit", "No autonomous legal commitment"),
    ),
    _pack(
        "supply-chain",
        "供应链能力包",
        "high",
        ("Authorized ERP or planning data", "Supplier and inventory policy"),
        ("Read-only domain integration", "Scenario planning", "Approval-gated operational writes"),
        ("Data quality passes", "Business constraints are modeled", "Write rollback is tested"),
    ),
    _pack(
        "behavioral-product",
        "行为产品能力包",
        "restricted",
        ("Explicit user-benefit case", "Consent and vulnerable-user analysis"),
        ("Ethics review", "Experiment guardrails", "Opt-out and harm monitoring"),
        ("No dark pattern", "Ethics gate passes", "Guardrail metrics and stop rule exist"),
    ),
    _pack(
        "project-integrations",
        "项目系统集成包",
        "high",
        ("Named project workspace", "Ownership and status policy"),
        ("Project-system integration", "Idempotent sync", "Conflict and rollback handling"),
        ("Read path passes", "Write ownership is explicit", "Sync and rollback tests pass"),
    ),
    _pack(
        "specialized-testing",
        "专项测试能力包",
        "high",
        ("Authorized target and environment", "Release identity and test oracle"),
        ("Target-specific test Tool", "Evidence capture", "Retest support"),
        ("Representative eval passes", "Evidence is reproducible", "Target boundary is enforced"),
    ),
    _pack(
        "specialized-business",
        "专项业务能力包",
        "high",
        ("Named business process", "Domain owner and source data"),
        ("Domain Skill", "Validated data connector", "Human approval for high-impact actions"),
        ("Domain eval passes", "Data access is least-privileged", "Decision accountability is explicit"),
    ),
    _pack(
        "spatial-computing",
        "空间计算能力包",
        "high",
        ("Named spatial project", "Device, engine, asset, and performance constraints"),
        ("Project toolchain", "Asset validation", "Device or simulator test path"),
        ("Target build runs", "Performance budget passes", "Rights and safety review passes"),
    ),
    _pack(
        "game-development",
        "游戏开发能力包",
        "high",
        ("Named game project", "Engine, platform, design, and asset constraints"),
        ("Project toolchain", "Playable build path", "Asset and telemetry validation"),
        ("Representative build runs", "Core loop eval passes", "Platform and rights constraints pass"),
    ),
    _pack(
        "academic",
        "学术能力包",
        "high",
        ("Education or research scope", "Citation and integrity policy"),
        ("Authoritative literature retrieval", "Citation checking", "Human academic review"),
        ("Sources are verifiable", "No fabricated results or citations", "Integrity policy passes"),
    ),
    _pack(
        "gis",
        "GIS 能力包",
        "high",
        ("Authorized geospatial data", "CRS, licensing, and privacy requirements"),
        ("GIS processing Tool", "Coordinate and topology validation", "Map artifact verification"),
        ("CRS tests pass", "Data license permits use", "Spatial output is visually verified"),
    ),
    _pack(
        "security-specialist",
        "安全专项能力包",
        "restricted",
        ("Explicit target-by-target authorization", "Allowed methods and time window"),
        ("Isolated security Tool", "Secret-safe evidence capture", "Approval and emergency stop"),
        ("Scope enforcement passes", "Evidence handling is approved", "No ambient offensive authority"),
    ),
)


def load_workforce_conditional_registry() -> WorkforceConditionalRegistry:
    return WorkforceConditionalRegistry(
        schema_version=1,
        packs=_PACKS,
        conditional_roles=workforce_records_by_decision("conditional_pack"),
        resolutions=workforce_records_by_decision("merge_or_reject"),
    )


__all__ = [
    "ConditionalPackContract",
    "WorkforceConditionalRegistry",
    "load_workforce_conditional_registry",
]
