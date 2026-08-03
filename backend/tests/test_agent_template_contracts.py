from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.agent_template_contract import (
    AgentTemplateManifest,
    TemplateContractError,
    load_agent_template_manifest,
    validate_template_capability_references,
)
from app.services.agent_tools import RUNTIME_TYPED_APPLICATION_TOOL_NAMES
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.skill_seeder import BUILTIN_SKILLS
from app.services.template_seeder import (
    _TEMPLATE_ROOT,
    _load_folder_templates,
    _merged_templates,
)
from app.services.tool_assignment_governance import (
    EXPLICIT_TOOL_ASSIGNMENT_EXEMPTIONS,
    validate_reviewed_tool_assignments,
)
from app.services.agent_workforce_catalog import workforce_records_by_decision


def test_all_folder_templates_are_strict_and_executable() -> None:
    known_skills = {skill["folder_name"] for skill in BUILTIN_SKILLS}
    known_tools = {tool["name"] for tool in BUILTIN_TOOL_DEFINITIONS}
    manifests = [load_agent_template_manifest(path) for path in sorted(_TEMPLATE_ROOT.iterdir()) if path.is_dir()]

    for manifest in manifests:
        validate_template_capability_references(
            manifest,
            known_skill_folders=known_skills,
            known_tool_names=known_tools,
            runtime_typed_tool_names=RUNTIME_TYPED_APPLICATION_TOOL_NAMES,
        )


def test_first_imported_role_set_is_present_and_legacy_project_manager_is_replaced() -> None:
    templates = _merged_templates()
    by_name = {template["name"]: template for template in templates}

    assert {
        "Product Manager",
        "Project Manager",
        "Multi-Agent Systems Architect",
        "Customer Success Manager",
        "Support Analytics Reporter",
        "Xiaohongshu Operations Manager",
        "Security Engineer",
    } <= by_name.keys()
    assert [template["name"] for template in templates].count("Project Manager") == 1
    assert by_name["Project Manager"]["category"] == "product-project"
    assert "complex-task-executor" in by_name["Project Manager"]["default_skills"]


def test_all_19_upgrade_roles_have_pinned_executable_v2_contracts() -> None:
    manifests = {
        manifest.role_key: manifest
        for path in sorted(_TEMPLATE_ROOT.iterdir())
        if path.is_dir()
        and (manifest := load_agent_template_manifest(path)).role_key is not None
    }
    records = workforce_records_by_decision("upgrade_existing")

    assert len(records) == 19
    for record in records:
        manifest = manifests[record.target_role_key]
        assert manifest.schema_version == 2
        assert manifest.role_revision == 2
        assert manifest.lifecycle_status == "enabled"
        assert manifest.workforce_decision == "upgrade_existing"
        assert manifest.workforce_source_role_id == record.role_id
        assert manifest.workflows
        assert manifest.deliverables
        assert manifest.evaluation_criteria
        assert manifest.source_provenance is not None
        assert manifest.source_provenance.commit == (
            "e7c3050dd94212832158e478f0f0af17409070f5"
        )
        assert record.source_path in manifest.source_provenance.paths


def test_every_explicit_tool_has_an_agent_route_or_reviewed_exception() -> None:
    canonical = {tool["name"]: bool(tool["is_default"]) for tool in BUILTIN_TOOL_DEFINITIONS}

    report = validate_reviewed_tool_assignments(
        _load_folder_templates(),
        canonical_default_by_name=canonical,
    )

    assert report.manual_or_system_scoped == tuple(sorted(EXPLICIT_TOOL_ASSIGNMENT_EXEMPTIONS))


def test_schema_v2_requires_role_boundaries_and_provenance() -> None:
    with pytest.raises(ValueError, match="schema_version=2 requires"):
        AgentTemplateManifest.model_validate(
            {
                "schema_version": 2,
                "name": "Incomplete",
                "description": "Missing the executable role contract.",
                "icon": "I",
                "category": "office",
            }
        )


def test_schema_v2_persists_reviewed_lifecycle_and_provenance() -> None:
    manifest = AgentTemplateManifest.model_validate(
        {
            "schema_version": 2,
            "role_key": "research-candidate",
            "role_revision": 3,
            "name": "Research Candidate",
            "description": "A reviewed but not yet recruitable role.",
            "icon": "RC",
            "category": "data-research",
            "responsibilities": ["Produce sourced research briefs"],
            "non_responsibilities": ["Do not publish externally"],
            "limitations": ["No production credentials"],
            "workflows": ["Clarify scope, research, draft, and self-review"],
            "deliverables": ["Sourced research brief"],
            "evaluation_criteria": ["Every material claim has a source"],
            "source_provenance": {
                "repository": "https://github.com/jnMetaCode/agency-agents-zh",
                "commit": "e7c3050dd94212832158e478f0f0af17409070f5",
                "paths": ["research/example.md"],
                "license": "MIT",
                "adaptation": "Converted to a gated Clawith role contract.",
            },
            "lifecycle_status": "candidate_disabled",
            "activation_gate": "Pass role and tool readiness evaluation.",
            "workforce_source_role_id": "research-candidate",
            "workforce_decision": "add_candidate",
            "workforce_pack": "research",
        }
    )

    seeded = manifest.to_seed_dict(soul_template="# Soul")

    assert seeded["role_key"] == "research-candidate"
    assert seeded["role_revision"] == 3
    assert seeded["lifecycle_status"] == "candidate_disabled"
    assert seeded["workforce_decision"] == "add_candidate"
    assert seeded["deliverables"] == ["Sourced research brief"]
    assert seeded["source_provenance"]["commit"] == ("e7c3050dd94212832158e478f0f0af17409070f5")


def test_disabled_schema_v2_role_requires_activation_gate() -> None:
    with pytest.raises(ValueError, match="disabled template lifecycle requires activation_gate"):
        AgentTemplateManifest.model_validate(
            {
                "schema_version": 2,
                "role_key": "blocked-role",
                "name": "Blocked Role",
                "description": "A role that must remain disabled.",
                "icon": "BR",
                "category": "office",
                "responsibilities": ["Draft internal notes"],
                "non_responsibilities": ["Do not send messages"],
                "workflows": ["Draft then request review"],
                "deliverables": ["Internal draft"],
                "evaluation_criteria": ["No external side effect"],
                "source_provenance": {
                    "repository": "https://github.com/example/roles",
                    "commit": "e7c3050dd94212832158e478f0f0af17409070f5",
                    "paths": ["roles/blocked.md"],
                    "license": "MIT",
                    "adaptation": "Kept disabled pending review.",
                },
                "lifecycle_status": "candidate_disabled",
                "workforce_source_role_id": "blocked-role",
                "workforce_decision": "add_candidate",
            }
        )


def test_unknown_manifest_field_fails_closed(tmp_path: Path) -> None:
    folder = tmp_path / "strict-role"
    folder.mkdir()
    (folder / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "Strict Role",
                "description": "A test role.",
                "icon": "SR",
                "category": "office",
                "typo_default_tool": ["read_file"],
            }
        ),
        encoding="utf-8",
    )
    (folder / "soul.md").write_text("# Soul", encoding="utf-8")

    with pytest.raises(TemplateContractError, match="Extra inputs are not permitted"):
        load_agent_template_manifest(folder)


def test_catalog_only_tool_reference_fails_closed() -> None:
    manifest = AgentTemplateManifest.model_validate(
        {
            "name": "Untyped Role",
            "description": "A test role.",
            "icon": "UR",
            "category": "office",
            "default_tools": ["catalog_only"],
        }
    )

    with pytest.raises(TemplateContractError, match="without Durable Runtime adapters"):
        validate_template_capability_references(
            manifest,
            known_skill_folders=set(),
            known_tool_names={"catalog_only"},
            runtime_typed_tool_names=set(),
        )
