from pathlib import Path

from app.services.agent_workforce_catalog import (
    load_agent_workforce_catalog,
    workforce_record,
    workforce_records_by_decision,
)


EXPECTED_SOURCE_COMMIT = "e7c3050dd94212832158e478f0f0af17409070f5"
EXPECTED_COUNTS = {
    "upgrade_existing": 19,
    "add_candidate": 92,
    "conditional_pack": 142,
    "merge_or_reject": 15,
}


def test_catalog_freezes_complete_upstream_inventory() -> None:
    catalog = load_agent_workforce_catalog()

    assert catalog.source.commit == EXPECTED_SOURCE_COMMIT
    assert catalog.source.license == "MIT"
    assert catalog.summary.total == 268
    assert len(catalog.records) == 268
    assert len({record.role_id for record in catalog.records}) == 268
    assert len({record.source_path for record in catalog.records}) == 268

    for decision, expected in EXPECTED_COUNTS.items():
        assert len(workforce_records_by_decision(decision)) == expected


def test_catalog_freezes_current_clawith_template_baseline() -> None:
    catalog = load_agent_workforce_catalog()
    template_root = Path(__file__).resolve().parents[1] / "agent_templates"
    folder_count = sum(path.is_dir() for path in template_root.iterdir())

    assert catalog.local_baseline == {
        "source_template_count": 33,
        # 30 workforce folders + the Astra-native `ceo` system-role folder.
        "folder_template_count": 31,
        "legacy_template_count": 4,
        "folder_override_names": ["Project Manager"],
    }
    assert folder_count == catalog.local_baseline["folder_template_count"]


def test_upgrade_targets_are_existing_folder_templates() -> None:
    template_root = Path(__file__).resolve().parents[1] / "agent_templates"
    folder_role_keys = {path.name for path in template_root.iterdir() if path.is_dir()}

    upgrade_records = workforce_records_by_decision("upgrade_existing")
    assert {record.target_role_key for record in upgrade_records} <= folder_role_keys


def test_new_candidate_keys_are_unique_and_do_not_shadow_existing_templates() -> None:
    template_root = Path(__file__).resolve().parents[1] / "agent_templates"
    folder_role_keys = {path.name for path in template_root.iterdir() if path.is_dir()}
    candidates = workforce_records_by_decision("add_candidate")
    candidate_keys = [record.target_role_key for record in candidates]

    assert len(candidate_keys) == len(set(candidate_keys)) == 92
    assert not set(candidate_keys) & folder_role_keys
    assert all(record.lifecycle == "candidate_disabled" for record in candidates)


def test_conditional_and_rejected_roles_are_not_recruitable() -> None:
    conditional = workforce_records_by_decision("conditional_pack")
    rejected = workforce_records_by_decision("merge_or_reject")

    assert all(record.lifecycle == "conditional_disabled" for record in conditional)
    assert all(record.pack and record.activation_gate for record in conditional)
    assert all(record.lifecycle == "not_recruitable" for record in rejected)
    assert all(record.reason and record.resolution for record in rejected)

    assert workforce_record("accounts-payable-agent").resolution == "reject_default"
    assert workforce_record("agents-orchestrator").resolution == "runtime_capability"
    assert workforce_record("security-penetration-tester").resolution == "task_scoped_only"
