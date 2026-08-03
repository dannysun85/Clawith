"""Validated repository catalog for external Agent workforce decisions."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "agent_workforce_catalog.v1.json"
)

WorkforceDecision = Literal[
    "upgrade_existing",
    "add_candidate",
    "conditional_pack",
    "merge_or_reject",
]
WorkforceLifecycle = Literal[
    "enabled_existing",
    "candidate_disabled",
    "conditional_disabled",
    "not_recruitable",
]


class WorkforceCatalogSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    license: str
    inventory_path: str


class WorkforceCatalogSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(gt=0)
    upgrade_existing: int = Field(ge=0)
    add_candidate: int = Field(ge=0)
    conditional_pack: int = Field(ge=0)
    merge_or_reject: int = Field(ge=0)


class WorkforceRoleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    department: str
    role_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    description: str
    origin: str
    source_path: str
    decision: WorkforceDecision
    lifecycle: WorkforceLifecycle
    target_role_key: str | None = None
    activation_gate: str
    pack: str | None = None
    resolution: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_decision_contract(self) -> "WorkforceRoleRecord":
        expected_lifecycle = {
            "upgrade_existing": "enabled_existing",
            "add_candidate": "candidate_disabled",
            "conditional_pack": "conditional_disabled",
            "merge_or_reject": "not_recruitable",
        }[self.decision]
        if self.lifecycle != expected_lifecycle:
            raise ValueError(
                f"{self.role_id}: {self.decision} requires lifecycle={expected_lifecycle}"
            )
        if self.decision in {"upgrade_existing", "add_candidate"} and not self.target_role_key:
            raise ValueError(f"{self.role_id}: enabled/candidate role requires target_role_key")
        if self.decision == "conditional_pack" and not self.pack:
            raise ValueError(f"{self.role_id}: conditional role requires pack")
        if self.decision == "merge_or_reject" and not self.reason:
            raise ValueError(f"{self.role_id}: merge/reject role requires reason")
        return self


class AgentWorkforceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    source: WorkforceCatalogSource
    local_baseline: dict[str, object]
    summary: WorkforceCatalogSummary
    records: tuple[WorkforceRoleRecord, ...]

    @model_validator(mode="after")
    def validate_inventory(self) -> "AgentWorkforceCatalog":
        role_ids = [record.role_id for record in self.records]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("workforce catalog contains duplicate role IDs")
        counts = {
            decision: sum(record.decision == decision for record in self.records)
            for decision in (
                "upgrade_existing",
                "add_candidate",
                "conditional_pack",
                "merge_or_reject",
            )
        }
        expected = self.summary.model_dump(exclude={"total"})
        if counts != expected:
            raise ValueError(f"workforce decision counts mismatch: {counts} != {expected}")
        if len(self.records) != self.summary.total:
            raise ValueError(
                f"workforce total mismatch: {len(self.records)} != {self.summary.total}"
            )
        return self


@lru_cache(maxsize=1)
def load_agent_workforce_catalog() -> AgentWorkforceCatalog:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return AgentWorkforceCatalog.model_validate(payload)


def workforce_records_by_decision(
    decision: WorkforceDecision,
) -> tuple[WorkforceRoleRecord, ...]:
    catalog = load_agent_workforce_catalog()
    return tuple(record for record in catalog.records if record.decision == decision)


def workforce_record(role_id: str) -> WorkforceRoleRecord | None:
    catalog = load_agent_workforce_catalog()
    return next((record for record in catalog.records if record.role_id == role_id), None)


__all__ = [
    "AgentWorkforceCatalog",
    "WorkforceDecision",
    "WorkforceRoleRecord",
    "load_agent_workforce_catalog",
    "workforce_record",
    "workforce_records_by_decision",
]
