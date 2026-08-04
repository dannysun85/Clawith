from app.services.agent_candidate_templates import (
    load_candidate_template_manifests,
    load_candidate_template_seeds,
)
from app.services.agent_workforce_catalog import workforce_records_by_decision
from app.services.skill_seeder import BUILTIN_SKILLS
from app.services.template_seeder import _merged_templates


def test_all_92_candidate_roles_have_strict_disabled_v2_contracts() -> None:
    manifests = load_candidate_template_manifests()
    source_records = {record.role_id: record for record in workforce_records_by_decision("add_candidate")}

    assert len(manifests) == 92
    assert len({manifest.role_key for manifest in manifests}) == 92
    assert len({manifest.workforce_source_role_id for manifest in manifests}) == 92
    for manifest in manifests:
        source = source_records[manifest.workforce_source_role_id]
        assert manifest.schema_version == 2
        assert manifest.lifecycle_status == "candidate_disabled"
        assert manifest.activation_gate == source.activation_gate
        assert manifest.workforce_decision == "add_candidate"
        assert manifest.default_tools == []
        assert manifest.default_mcp_servers == []
        assert manifest.responsibilities
        assert manifest.non_responsibilities
        assert manifest.workflows
        assert manifest.deliverables
        assert manifest.evaluation_criteria
        assert manifest.source_provenance is not None
        assert manifest.source_provenance.commit == ("e7c3050dd94212832158e478f0f0af17409070f5")
        assert source.source_path in manifest.source_provenance.paths


def test_candidate_skills_are_registered_and_templates_remain_non_executable() -> None:
    known_skills = {skill["folder_name"] for skill in BUILTIN_SKILLS}
    manifests = load_candidate_template_manifests()

    assert {skill for manifest in manifests for skill in manifest.default_skills} <= known_skills
    assert all(not manifest.default_tools for manifest in manifests)
    assert all(not manifest.default_mcp_servers for manifest in manifests)


def test_candidate_seeds_are_persistable_but_not_recruitable() -> None:
    seeds = load_candidate_template_seeds()

    assert len(seeds) == 92
    assert all(seed["is_builtin"] is True for seed in seeds)
    assert all(seed["lifecycle_status"] == "candidate_disabled" for seed in seeds)
    assert all(seed["activation_gate"] for seed in seeds)
    assert all("Candidate role" in seed["soul_template"] for seed in seeds)


def test_template_seeder_includes_92_candidates_without_replacing_existing_roles() -> None:
    templates = _merged_templates()

    assert len(templates) == 125
    assert sum(
        template.get("lifecycle_status", "enabled") == "candidate_disabled"
        for template in templates
    ) == 92
    assert sum(
        template.get("lifecycle_status", "enabled") == "enabled"
        for template in templates
    ) == 32
    assert sum(
        template.get("lifecycle_status", "enabled") == "not_recruitable"
        for template in templates
    ) == 1
