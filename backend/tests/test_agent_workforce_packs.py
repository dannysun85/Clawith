from app.services.agent_workforce_packs import load_workforce_conditional_registry


def test_all_142_conditional_roles_have_explicit_pack_activation_contracts() -> None:
    registry = load_workforce_conditional_registry()

    assert len(registry.conditional_roles) == 142
    assert len(registry.packs) == 15
    assert sum(registry.pack_counts().values()) == 142
    assert set(registry.pack_counts()) == {pack.pack for pack in registry.packs}
    for pack in registry.packs:
        assert pack.required_context
        assert pack.required_capabilities
        assert pack.activation_criteria
        assert pack.forbidden_defaults


def test_all_15_merge_or_reject_resolutions_remain_non_recruitable() -> None:
    registry = load_workforce_conditional_registry()

    assert len(registry.resolutions) == 15
    assert registry.resolution_counts() == {
        "merge": 9,
        "reject_default": 3,
        "runtime_capability": 1,
        "skill_only": 1,
        "task_scoped_only": 1,
    }
    assert all(role.lifecycle == "not_recruitable" for role in registry.resolutions)
    assert all(role.reason for role in registry.resolutions)
    assert all(role.resolution for role in registry.resolutions)


def test_restricted_packs_forbid_ambient_high_impact_authority() -> None:
    registry = load_workforce_conditional_registry()
    restricted = {pack.pack: pack for pack in registry.packs if pack.risk_level == "restricted"}

    assert {
        "paid-media",
        "regulated-finance",
        "legal",
        "behavioral-product",
        "security-specialist",
    } <= restricted.keys()
    assert all(any("No external action" in rule for rule in pack.forbidden_defaults) for pack in restricted.values())
