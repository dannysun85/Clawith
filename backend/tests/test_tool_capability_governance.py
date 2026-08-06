from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api import advanced, tools as tools_api
from app.services import (
    agent_tool_assignments,
    agent_tools,
    autonomy_service,
    template_capabilities,
    tool_seeder,
)
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.tool_capability_policy import (
    CENTRAL_CREDENTIAL_POOL_TOOL_NAMES,
    EXPLICIT_GRANT_TOOL_NAMES,
    GLOBAL_DEFAULT_MEDIA_TOOL_NAMES,
)
from app.services.tool_config import (
    remove_sensitive_fields,
    sanitize_tool_config_credential_ownership,
)
from app.services.tool_visibility import tool_enabled_for_agent
from app.services.skill_seeder import BUILTIN_SKILLS


def test_explicit_grant_policy_matches_runtime_and_persistence_definitions() -> None:
    canonical = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}
    persisted = {item["name"]: item for item in tool_seeder.BUILTIN_TOOLS}

    assert EXPLICIT_GRANT_TOOL_NAMES <= canonical.keys()
    assert EXPLICIT_GRANT_TOOL_NAMES <= persisted.keys()
    for name in EXPLICIT_GRANT_TOOL_NAMES:
        assert canonical[name]["is_default"] is False
        assert persisted[name]["is_default"] is False
        assert name in tool_seeder.SYNC_IS_DEFAULT_TOOL_NAMES


def test_complete_persisted_catalog_matches_canonical_defaults() -> None:
    canonical = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}
    persisted = {item["name"]: item for item in tool_seeder.BUILTIN_TOOLS}

    assert persisted.keys() == canonical.keys()
    assert {name: definition["is_default"] for name, definition in persisted.items()} == {
        name: definition["is_default"] for name, definition in canonical.items()
    }


def test_only_finish_bypasses_persisted_capability_resolution() -> None:
    assert agent_tools._ALWAYS_INCLUDE_CORE == {"finish"}


def test_explicit_grant_tools_never_use_stale_default_fallback() -> None:
    stale_role_tool = SimpleNamespace(
        name="generate_music_minimax",
        source="builtin",
        is_default=True,
    )
    explicit_grant = SimpleNamespace(enabled=True)
    explicit_revoke = SimpleNamespace(enabled=False)

    assert tool_enabled_for_agent(stale_role_tool, None) is False
    assert tool_enabled_for_agent(stale_role_tool, explicit_grant) is True
    assert tool_enabled_for_agent(stale_role_tool, explicit_revoke) is False


def test_image_speech_and_video_are_global_defaults_with_agent_opt_out() -> None:
    canonical = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}
    persisted = {item["name"]: item for item in tool_seeder.BUILTIN_TOOLS}

    assert GLOBAL_DEFAULT_MEDIA_TOOL_NAMES == {
        "generate_image_minimax",
        "check_image_generation",
        "generate_speech_minimax",
        "compose_video_audio",
        "generate_video_minimax",
        "check_video_minimax",
    }
    assert GLOBAL_DEFAULT_MEDIA_TOOL_NAMES.isdisjoint(EXPLICIT_GRANT_TOOL_NAMES)
    for name in GLOBAL_DEFAULT_MEDIA_TOOL_NAMES:
        assert canonical[name]["is_default"] is True
        assert persisted[name]["is_default"] is True
        assert name in tool_seeder.SYNC_IS_DEFAULT_TOOL_NAMES
        tool = SimpleNamespace(name=name, source="builtin", is_default=True)
        assert tool_enabled_for_agent(tool, None) is True
        assert tool_enabled_for_agent(tool, SimpleNamespace(enabled=False)) is False


def test_managed_image_tool_exposes_role_aware_poster_copy_contract() -> None:
    canonical = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}
    persisted = {item["name"]: item for item in tool_seeder.BUILTIN_TOOLS}

    for definition in (
        canonical["generate_image_minimax"],
        persisted["generate_image_minimax"],
    ):
        description = definition["description"]
        assert "provider generates only the text-free visual background" in description
        assert "Astra's server deterministically composites" in description
        assert "one Tool call and one provider submission" in description
        assert "callers neither provide nor need a delivery_size field" in description
        assert "poster-v3 receipt" in description
        blocks = definition["parameters_schema"]["properties"]["overlay_blocks"]
        assert blocks["type"] == "array"
        assert blocks["maxItems"] == 8
        assert blocks["items"]["properties"]["role"]["enum"] == [
            "title",
            "subtitle",
            "tagline",
            "body",
            "cta",
        ]
        aspect_help = definition["parameters_schema"]["properties"]["aspect_ratio"][
            "description"
        ]
        assert "do not look for or invent a delivery_size argument" in aspect_help


def test_core_default_tools_keep_product_policy_fallback() -> None:
    core_tool = SimpleNamespace(
        name="read_file",
        source="builtin",
        is_default=True,
    )

    assert tool_enabled_for_agent(core_tool, None) is True


def test_agent_owned_tool_requires_exact_assignment_even_if_marked_default() -> None:
    agent_tool = SimpleNamespace(
        name="tenant_custom_action",
        source="agent",
        is_default=True,
    )

    assert tool_enabled_for_agent(agent_tool, None) is False


def test_self_scoped_objective_update_remains_a_core_capability() -> None:
    canonical = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}

    assert "update_objective" not in EXPLICIT_GRANT_TOOL_NAMES
    assert canonical["update_objective"]["is_default"] is True


def test_general_agent_operations_are_not_misclassified_as_role_grants() -> None:
    canonical = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}

    for name in {
        "delete_file",
        "set_trigger",
        "update_trigger",
        "cancel_trigger",
        "send_channel_file",
        "send_platform_message",
        "list_published_pages",
    }:
        assert name not in EXPLICIT_GRANT_TOOL_NAMES
        assert canonical[name]["is_default"] is True


def test_removed_legacy_plaza_tools_stay_disabled() -> None:
    canonical = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}

    for name in {
        "plaza_get_new_posts",
        "plaza_create_post",
        "plaza_add_comment",
    }:
        assert name not in EXPLICIT_GRANT_TOOL_NAMES
        assert canonical[name]["is_default"] is False


def test_external_delivery_and_automation_use_the_high_risk_autonomy_gate() -> None:
    assert autonomy_service.HIGH_RISK_DEFAULT_L3_ACTIONS >= {
        "send_external_message",
        "manage_automation",
    }
    for tool_name in {
        "send_channel_message",
        "send_platform_message",
        "send_channel_file",
    }:
        assert agent_tools._TOOL_AUTONOMY_MAP[tool_name] == "send_external_message"
        assert autonomy_service._approval_action_matches_tool(
            "send_external_message",
            tool_name,
        )
    for tool_name in {"set_trigger", "update_trigger", "cancel_trigger"}:
        assert agent_tools._TOOL_AUTONOMY_MAP[tool_name] == "manage_automation"
        assert autonomy_service._approval_action_matches_tool(
            "manage_automation",
            tool_name,
        )


def test_external_writes_and_capability_mutations_use_durable_l3_gates() -> None:
    expected = {
        "send_email": "send_external_message",
        "reply_email": "send_external_message",
        "import_mcp_server": "manage_agent_capabilities",
        "install_skill": "manage_agent_capabilities",
        "vercel_deploy": "manage_external_deployment",
        "vercel_set_env": "manage_external_deployment",
        "vercel_manage_domain": "manage_external_deployment",
        "neon_create_database": "manage_external_deployment",
        "publish_page": "publish_external_content",
    }

    assert set(expected.values()) <= autonomy_service.HIGH_RISK_DEFAULT_L3_ACTIONS
    for tool_name, action_type in expected.items():
        assert agent_tools._TOOL_AUTONOMY_MAP[tool_name] == action_type
        assert autonomy_service._approval_action_matches_tool(action_type, tool_name)


def test_platform_pool_media_credentials_are_removed_from_tool_overrides() -> None:
    assert CENTRAL_CREDENTIAL_POOL_TOOL_NAMES == {
        "generate_image_minimax",
        "check_image_generation",
        "generate_speech_minimax",
        "generate_music_minimax",
        "generate_video_minimax",
        "check_video_minimax",
    }
    cleaned = sanitize_tool_config_credential_ownership(
        "generate_image_minimax",
        {
            "api_key": "obsolete-object-key",
            "nested": {"access_token": "obsolete-token", "model": "image-01"},
            "aspect_ratio": "16:9",
        },
    )

    assert cleaned == {
        "nested": {"model": "image-01"},
        "aspect_ratio": "16:9",
    }


@pytest.mark.asyncio
async def test_platform_pool_legacy_tool_key_is_scrubbed_not_migrated(
    monkeypatch,
) -> None:
    tool = SimpleNamespace(
        name="generate_image_minimax",
        config={"api_key": "obsolete-object-key", "model": "image-01"},
        config_schema={},
    )
    write_tenant_config = AsyncMock()
    monkeypatch.setattr(tool_seeder, "set_tenant_tool_config", write_tenant_config)

    result = await tool_seeder._migrate_legacy_builtin_credentials(
        _Db([]),
        tool=tool,
        sole_tenant=SimpleNamespace(id=uuid.uuid4()),
    )

    assert result == "platform_pool_removed"
    assert tool.config == {"model": "image-01"}
    write_tenant_config.assert_not_awaited()


def test_platform_pool_credentials_are_rejected_by_tool_api() -> None:
    tool = SimpleNamespace(
        name="generate_video_minimax",
        config_schema={},
    )

    with pytest.raises(HTTPException) as exc_info:
        tools_api._reject_platform_pool_tool_credentials(
            tool,
            {"api_key": "object-level-key"},
        )

    assert exc_info.value.status_code == 422


def test_ambiguous_legacy_builtin_config_removes_secrets_not_only_encrypts() -> None:
    cleaned = remove_sensitive_fields(
        {
            "base_url": "https://provider.example/v1",
            "api_key": "shared-secret",
            "nested": {
                "access_token": "nested-secret",
                "model": "model-a",
            },
        },
        {
            "fields": [
                {"key": "api_key", "type": "password"},
                {"key": "base_url", "type": "text"},
            ]
        },
    )

    assert cleaned == {
        "base_url": "https://provider.example/v1",
        "nested": {"model": "model-a"},
    }


@pytest.mark.asyncio
async def test_legacy_builtin_migration_never_overwrites_existing_tenant_config(
    monkeypatch,
) -> None:
    tool = SimpleNamespace(
        name="provider_tool",
        config={"base_url": "https://provider.example/v1", "api_key": "stale"},
        config_schema={
            "fields": [
                {"key": "base_url", "type": "text"},
                {"key": "api_key", "type": "password"},
            ]
        },
    )
    tenant = SimpleNamespace(id=uuid.uuid4())
    db = _Db([_Result(scalar=object()), _Result(scalar=None)])
    write_tenant_config = AsyncMock()
    monkeypatch.setattr(tool_seeder, "set_tenant_tool_config", write_tenant_config)

    result = await tool_seeder._migrate_legacy_builtin_credentials(
        db,
        tool=tool,
        sole_tenant=tenant,
    )

    assert result == "preserved"
    assert tool.config == {"base_url": "https://provider.example/v1"}
    write_tenant_config.assert_not_awaited()
    assert len(db.added) == 1
    assert db.added[0].value["runtime_enabled"] is False
    assert "stale" not in str(db.added[0].value)

    # A second startup sees no credential on the global row and is a no-op.
    assert (
        await tool_seeder._migrate_legacy_builtin_credentials(
            db,
            tool=tool,
            sole_tenant=tenant,
        )
        == "ignored"
    )


@pytest.mark.asyncio
async def test_legacy_builtin_migration_quarantines_known_secret_without_schema(
    monkeypatch,
) -> None:
    tool = SimpleNamespace(
        name="legacy_provider_tool",
        config={"base_url": "https://provider.example/v1", "api_key": "legacy-secret"},
        config_schema={},
    )
    db = _Db([_Result(scalar=None)])
    write_tenant_config = AsyncMock()
    monkeypatch.setattr(tool_seeder, "set_tenant_tool_config", write_tenant_config)

    result = await tool_seeder._migrate_legacy_builtin_credentials(
        db,
        tool=tool,
        sole_tenant=None,
    )

    assert result == "quarantined"
    assert tool.config == {"base_url": "https://provider.example/v1"}
    write_tenant_config.assert_not_awaited()
    assert len(db.added) == 1
    assert db.added[0].value["tool_name"] == "legacy_provider_tool"
    assert db.added[0].value["runtime_enabled"] is False
    assert "legacy-secret" not in str(db.added[0].value)


def test_builtin_role_skills_have_the_executable_grants_they_require() -> None:
    templates_root = Path(__file__).parents[1] / "agent_templates"
    for folder in {
        "backend-architect",
        "frontend-developer",
        "devops-automator",
        "rapid-prototyper",
    }:
        metadata = yaml.safe_load((templates_root / folder / "meta.yaml").read_text(encoding="utf-8"))
        assert "vercel-full-stack-deploy" in metadata["default_skills"]
        assert {"execute_code", "publish_page"} <= set(metadata["default_tools"])

    for folder in {"devops-automator", "rapid-prototyper"}:
        metadata = yaml.safe_load((templates_root / folder / "meta.yaml").read_text(encoding="utf-8"))
        assert "mcp-installer" in metadata["default_skills"]
        assert "import_mcp_server" in metadata["default_tools"]

    reviewer = yaml.safe_load((templates_root / "code-reviewer" / "meta.yaml").read_text(encoding="utf-8"))
    assert "execute_code" in reviewer["default_tools"]


def test_every_builtin_role_reference_resolves_to_a_real_skill_or_tool() -> None:
    templates_root = Path(__file__).parents[1] / "agent_templates"
    known_skills = {skill["folder_name"] for skill in BUILTIN_SKILLS}
    known_tools = {tool["name"] for tool in BUILTIN_TOOL_DEFINITIONS}

    for metadata_path in templates_root.glob("*/meta.yaml"):
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        assert set(metadata.get("default_skills") or []) <= known_skills, metadata_path
        assert set(metadata.get("default_tools") or []) <= known_tools, metadata_path


def test_role_skills_do_not_invent_missing_tool_or_deployment_authority() -> None:
    skill_files = {
        skill["folder_name"]: "\n".join(str(item.get("content") or "") for item in skill.get("files", []))
        for skill in BUILTIN_SKILLS
    }

    market_data = skill_files["market-data"]
    assert "The presence of this Skill does not grant installation Tools" in market_data
    assert "`import_mcp_server` is not visible" in market_data

    deployment = skill_files["vercel-full-stack-deploy"]
    assert "Never compensate" in deployment
    assert "for a missing Tool by claiming a deployment" in deployment
    assert "requires explicit user approval" in deployment
    assert "Otherwise, directly call the `vercel_deploy` tool" not in deployment
    assert "disable Vercel's Deployment Protection" not in deployment


@pytest.mark.asyncio
async def test_tools_ui_mirrors_channel_runtime_readiness() -> None:
    channel_tool = SimpleNamespace(name="send_channel_message")

    available, reason = await tools_api._agent_tool_availability(
        SimpleNamespace(),
        channel_tool,
        uuid.uuid4(),
        has_any_channel=False,
    )
    assert available is False
    assert "Configure an external Agent channel" in str(reason)

    available, reason = await tools_api._agent_tool_availability(
        SimpleNamespace(),
        channel_tool,
        uuid.uuid4(),
        has_any_channel=True,
    )
    assert available is True
    assert reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"default_tools": ["execute_code"]},
        {"default_autonomy_policy": {"delete_files": "L1"}},
    ),
)
async def test_unreviewed_template_cannot_carry_executable_policy(payload) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await advanced.create_template(
            advanced.TemplateCreate(name="Unreviewed", **payload),
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_l3_autonomy_blocks_typed_write_before_business_handler(monkeypatch) -> None:
    async def require_approval(**_kwargs):
        return {"allowed": False, "level": "L3", "approval_id": str(uuid.uuid4())}

    async def must_not_write(*_args, **_kwargs):
        pytest.fail("business handler must not run before approval")

    monkeypatch.setattr(agent_tools, "_enforce_tool_autonomy", require_approval)
    monkeypatch.setattr(agent_tools, "_write_file_outcome", must_not_write)

    outcome = await agent_tools.execute_builtin_tool_outcome(
        "write_file",
        {"path": "workspace/result.md", "content": "draft"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=str(uuid.uuid4()),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "tool_approval_required"


@pytest.mark.asyncio
async def test_l3_autonomy_blocks_capability_install_before_fetch(monkeypatch) -> None:
    async def require_approval(**_kwargs):
        return {"allowed": False, "level": "L3", "approval_id": str(uuid.uuid4())}

    async def must_not_install(*_args, **_kwargs):
        pytest.fail("Skill fetch/install must not run before approval")

    monkeypatch.setattr(agent_tools, "_enforce_tool_autonomy", require_approval)
    monkeypatch.setattr(agent_tools, "_install_skill_outcome", must_not_install)

    outcome = await agent_tools.execute_builtin_tool_outcome(
        "install_skill",
        {"source": "reviewed-skill"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=str(uuid.uuid4()),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "tool_approval_required"


@pytest.mark.asyncio
async def test_approved_external_deploy_preserves_unknown_provider_state(monkeypatch) -> None:
    async def unknown_write(*_args, **_kwargs):
        return agent_tools._typed_unknown(
            "Provider response is unknown.",
            "provider_write_unknown",
        )

    monkeypatch.setattr(agent_tools, "_deploy_simple_write_outcome", unknown_write)

    outcome = await agent_tools._execute_approved_tool(
        "vercel_set_env",
        {"project_id": "project-1", "key": "API_URL", "value_ref": "value://1"},
        uuid.uuid4(),
        approval_id=uuid.uuid4(),
        approval_claim_token=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    assert outcome.status == "ambiguous"
    assert outcome.error_code == "provider_write_unknown"


class _Result:
    def __init__(self, *, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self, results):
        self.results = iter(results)
        self.deleted = []
        self.added = []

    async def execute(self, _query):
        return next(self.results)

    async def delete(self, value):
        self.deleted.append(value)

    def add(self, value):
        self.added.append(value)


class _Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return False


class _StatementDb:
    statement = None

    async def execute(self, statement):
        self.statement = statement


@pytest.mark.asyncio
async def test_template_grant_can_replace_only_template_provenance() -> None:
    db = _StatementDb()
    await agent_tool_assignments.upsert_agent_tool(
        db,
        agent_id=uuid.uuid4(),
        tool_id=uuid.uuid4(),
        enabled=True,
        source="template",
        on_conflict="template",
    )

    compiled = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "DO UPDATE" in compiled
    assert "WHERE agent_tools.source =" in compiled


@pytest.mark.asyncio
async def test_globally_disabled_tool_keeps_role_assignment(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    tool = SimpleNamespace(
        id=uuid.uuid4(),
        name="execute_code",
        source="builtin",
        enabled=False,
    )
    db = _Db([_Result(rows=[tool])])
    upsert = AsyncMock()
    monkeypatch.setattr(template_capabilities, "upsert_agent_tool", upsert)

    granted, unresolved = await template_capabilities.grant_template_tools(
        db,
        agent_id=agent_id,
        tool_names=["execute_code"],
    )

    assert granted == 1
    assert unresolved == ()
    upsert.assert_awaited_once_with(
        db,
        agent_id=agent_id,
        tool_id=tool.id,
        enabled=True,
        source="template",
        on_conflict="template",
    )


@pytest.mark.asyncio
async def test_template_reconciliation_removes_only_stale_template_grants(
    monkeypatch,
) -> None:
    agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    keep = SimpleNamespace(agent_id=agent_id, source="template")
    stale = SimpleNamespace(agent_id=agent_id, source="template")
    detached = SimpleNamespace(agent_id=other_agent_id, source="template")
    legacy_role = SimpleNamespace(
        agent_id=agent_id,
        source="system",
        enabled=True,
    )
    legacy_ambient = SimpleNamespace(
        agent_id=other_agent_id,
        source="system",
        enabled=True,
    )
    legacy_opt_out = SimpleNamespace(
        agent_id=agent_id,
        source="system",
        enabled=False,
    )
    db = _Db(
        [
            _Result(rows=[(agent_id, ["execute_code"])]),
            _Result(
                rows=[
                    (legacy_role, "execute_code"),
                    (legacy_ambient, "execute_code"),
                    (legacy_opt_out, "execute_code"),
                ]
            ),
            _Result(
                rows=[
                    (keep, "execute_code"),
                    (stale, "publish_page"),
                    (detached, "generate_video_minimax"),
                ]
            ),
        ]
    )
    grant = AsyncMock(return_value=(1, ()))
    monkeypatch.setattr(template_capabilities, "grant_template_tools", grant)

    report = await template_capabilities.reconcile_template_tool_grants(db)

    assert report == template_capabilities.TemplateToolReconcileReport(
        agents_reviewed=1,
        granted=1,
        removed=2,
        missing_tool_names=(),
        migrated_to_template=1,
        disabled_ambient=0,
        preserved_opt_out=1,
        preserved_ambiguous=1,
    )
    assert report.changed is True
    assert db.deleted == [stale, detached]
    assert legacy_role.source == "template"
    assert legacy_role.enabled is True
    assert legacy_ambient.source == "legacy_ambiguous"
    assert legacy_ambient.enabled is True
    assert legacy_opt_out.source == "user_selected"
    assert legacy_opt_out.enabled is False
    grant.assert_awaited_once_with(
        db,
        agent_id=agent_id,
        tool_names=["execute_code"],
    )


@pytest.mark.asyncio
async def test_template_reconciliation_registers_already_imported_mcp_for_existing_agent(
    monkeypatch,
) -> None:
    agent_id = uuid.uuid4()
    mcp_tool = SimpleNamespace(name="mcp_shibui_finance_quote")
    db = _Db(
        [
            _Result(rows=[(agent_id, [], ["shibui/finance"])]),
            _Result(rows=[mcp_tool]),
            _Result(rows=[]),
            _Result(rows=[]),
        ]
    )
    grant = AsyncMock(return_value=(1, ()))
    monkeypatch.setattr(template_capabilities, "grant_template_tools", grant)

    report = await template_capabilities.reconcile_template_tool_grants(db)

    assert report.missing_mcp_servers == ()
    grant.assert_awaited_once_with(
        db,
        agent_id=agent_id,
        tool_names=["mcp_shibui_finance_quote"],
    )


@pytest.mark.asyncio
async def test_template_reconciliation_reports_mcp_that_still_needs_explicit_import(
    monkeypatch,
) -> None:
    agent_id = uuid.uuid4()
    db = _Db(
        [
            _Result(rows=[(agent_id, [], ["shibui/finance"])]),
            _Result(rows=[]),
            _Result(rows=[]),
            _Result(rows=[]),
        ]
    )
    grant = AsyncMock(return_value=(0, ()))
    monkeypatch.setattr(template_capabilities, "grant_template_tools", grant)

    report = await template_capabilities.reconcile_template_tool_grants(db)

    assert report.missing_mcp_servers == ("shibui/finance",)
    grant.assert_awaited_once_with(db, agent_id=agent_id, tool_names=[])


@pytest.mark.asyncio
async def test_empty_agent_capability_set_does_not_restore_hardcoded_tools(monkeypatch) -> None:
    agent = SimpleNamespace(tenant_id=uuid.uuid4(), is_system=False)
    sessions = iter(
        [
            _Session(_Db([_Result(scalar=agent)])),
            _Session(_Db([_Result(rows=()), _Result(rows=())])),
        ]
    )

    async def no_channel(_agent_id):
        return False

    async def default_os(_agent_id):
        return "linux"

    monkeypatch.setattr(agent_tools, "async_session", lambda: next(sessions))
    monkeypatch.setattr(agent_tools, "_agent_has_feishu", no_channel)
    monkeypatch.setattr(agent_tools, "_agent_has_any_channel", no_channel)
    monkeypatch.setattr(agent_tools, "_get_computer_os_type", default_os)

    tools = await agent_tools.get_agent_tools_for_llm(uuid.uuid4())

    assert [tool["function"]["name"] for tool in tools] == ["finish"]
