import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.agentbay_client import (
    AGENTBAY_SDK_TIMEOUT_MS,
    AgentBayClient,
    _agentbay_sessions,
    get_existing_agentbay_client_for_agent,
    _get_active_agentbay_ledger,
    _inject_credentials,
    test_agentbay_channel as validate_agentbay_channel,
)
from app.api import agentbay_control
from app.api.admin import (
    AgentBayCleanupReconcileRequest,
    reconcile_agentbay_cleanup_required,
)


def _failed_result(**values):
    return SimpleNamespace(
        success=False,
        error_message="provider-internal-secret",
        data=values.pop("data", None),
        **values,
    )


def test_agentbay_client_applies_a_bounded_sdk_timeout():
    with patch("app.services.agentbay_client.AgentBay") as sdk:
        AgentBayClient("ak-test")

    config = sdk.call_args.kwargs["cfg"]
    assert config.timeout_ms == AGENTBAY_SDK_TIMEOUT_MS
    assert AGENTBAY_SDK_TIMEOUT_MS == 30_000


@pytest.mark.asyncio
async def test_cookie_injection_uses_ephemeral_env_without_secret_file_or_cookie_logs():
    secret_value = "session-cookie-secret"
    credential = SimpleNamespace(
        platform="example",
        cookies_json="encrypted",
        last_injected_at=None,
    )

    class CredentialResult:
        def scalars(self):
            return self

        def all(self):
            return [credential]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return CredentialResult()

        def add(self, _value):
            return None

        async def commit(self):
            return None

    command_exec = MagicMock(
        return_value=SimpleNamespace(
            success=True,
            stdout="INJECT_OK:1 injected, 0 skipped",
            stderr="",
        )
    )
    client = SimpleNamespace(
        _ensure_browser_initialized=AsyncMock(),
        _session=SimpleNamespace(command=SimpleNamespace(exec=command_exec)),
    )

    with (
        patch("app.database.async_session", return_value=Session()),
        patch(
            "app.core.security.decrypt_data",
            return_value=(
                '[{"name":"session","value":"'
                + secret_value
                + '","domain":"example.com","path":"/"}]'
            ),
        ),
    ):
        await _inject_credentials(
            client,
            uuid.uuid4(),
            uuid.uuid4(),
        )

    command = command_exec.call_args.args[0]
    envs = command_exec.call_args.kwargs["envs"]
    assert secret_value not in command
    assert "tc_inject_cookies.js" not in command
    assert ">" not in command
    assert "ASTRA_COOKIES_B64" in envs
    assert secret_value not in envs["ASTRA_COOKIES_B64"]
    assert "JSON.stringify(cookie)" not in command


@pytest.mark.asyncio
async def test_agentbay_configuration_test_creates_no_provider_resources():
    agent_id = uuid.uuid4()
    sdk = MagicMock()

    with (
        patch(
            "app.services.agentbay_client.get_agentbay_api_key_for_agent",
            AsyncMock(return_value="ak-configured"),
        ),
        patch(
            "app.services.agent_tools._get_tool_config",
            AsyncMock(return_value={"os_type": "windows"}),
        ),
        patch("app.services.agentbay_client.AgentBay", sdk),
    ):
        result = await validate_agentbay_channel(
            agent_id,
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=object(),
        )

    assert result["ok"] is True
    assert result["runtime_tested"] is False
    assert result["capabilities"]["browser"]["status"] == "configuration_validated"
    assert result["capabilities"]["computer"]["image"] == "windows_latest"
    assert result["capabilities"]["code"]["enabled"] is False
    assert (
        result["capabilities"]["code"]["status"]
        == "separate_production_authorization_required"
    )
    assert "Remote authorization and runtime were not tested" in result["message"]
    assert "Code requires separate production authorization" in result["message"]
    sdk.assert_not_called()


@pytest.mark.asyncio
async def test_agentbay_operation_failures_have_correct_safe_messages():
    computer = SimpleNamespace(
        screenshot=MagicMock(return_value=_failed_result()),
        get_screen_size=MagicMock(return_value=_failed_result()),
        start_app=MagicMock(return_value=_failed_result()),
        get_installed_apps=MagicMock(return_value=_failed_result(data=[])),
        get_cursor_position=MagicMock(return_value=_failed_result()),
        get_active_window=MagicMock(return_value=_failed_result(window=None)),
        list_root_windows=MagicMock(return_value=_failed_result(windows=[])),
        close_window=MagicMock(return_value=_failed_result()),
        list_visible_apps=MagicMock(return_value=_failed_result(data=[])),
    )
    command = SimpleNamespace(exec=MagicMock(return_value=_failed_result()))
    client = AgentBayClient.__new__(AgentBayClient)
    client._session = SimpleNamespace(computer=computer, command=command)
    client._image_type = "linux_latest"
    client._ensure_computer_session = AsyncMock()

    with patch("app.services.agentbay_client.asyncio.sleep", AsyncMock()):
        results = [
            await client.command_exec("false"),
            await client.computer_screenshot(),
            await client.computer_get_screen_size(),
            await client.computer_start_app("missing"),
            await client.computer_get_installed_apps(),
            await client.computer_get_cursor_position(),
            await client.computer_get_active_window(),
            await client.computer_list_windows(),
            await client.computer_close_window(1),
            await client.computer_list_visible_apps(),
        ]

    assert [result["error_message"] for result in results] == [
        "AgentBay command execution failed",
        "AgentBay screenshot failed",
        "AgentBay screen-size lookup failed",
        "AgentBay application start failed",
        "AgentBay application list failed",
        "AgentBay cursor lookup failed",
        "AgentBay active-window lookup failed",
        "AgentBay window list failed",
        "AgentBay close-window failed",
        "AgentBay visible-application list failed",
    ]
    assert all("provider-internal-secret" not in str(result) for result in results)


@pytest.mark.asyncio
async def test_agentbay_operations_never_create_an_unmanaged_provider_session():
    client = AgentBayClient.__new__(AgentBayClient)
    client._session = None
    client._image_type = None
    client.create_session = AsyncMock()
    client.delete_session_strict = AsyncMock()

    operations = (
        client.browser_navigate("https://example.com"),
        client.browser_login("https://example.com/login", "{}"),
        client.code_execute("python", "print('safe')"),
        client.command_exec("true"),
        client._ensure_computer_session(),
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="existing managed session"):
            await operation

    client.create_session.assert_not_awaited()
    client.delete_session_strict.assert_not_awaited()


@pytest.mark.asyncio
async def test_agentbay_wrong_session_type_fails_without_provider_replacement():
    client = AgentBayClient.__new__(AgentBayClient)
    client._session = SimpleNamespace(session_id="provider-exact")
    client._image_type = "code_latest"
    client.create_session = AsyncMock()
    client.delete_session_strict = AsyncMock()

    with pytest.raises(RuntimeError, match="not available"):
        await client.browser_navigate("https://example.com")

    client._image_type = "browser_latest"
    with pytest.raises(RuntimeError, match="not available"):
        await client.code_execute("python", "print('safe')")
    with pytest.raises(RuntimeError, match="not available"):
        await client._ensure_computer_session()

    client.create_session.assert_not_awaited()
    client.delete_session_strict.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_login_reuses_the_exact_managed_browser_latest_session():
    provider_session_id = "provider-exact"
    operator = SimpleNamespace(
        navigate=MagicMock(return_value=None),
        login=MagicMock(return_value=SimpleNamespace(success=True, message="ok")),
    )
    client = AgentBayClient.__new__(AgentBayClient)
    client._session = SimpleNamespace(
        session_id=provider_session_id,
        browser=SimpleNamespace(operator=operator),
    )
    client._image_type = "browser_latest"
    client._browser_initialized = True
    client.create_session = AsyncMock()
    client.delete_session_strict = AsyncMock()

    result = await client.browser_login("https://example.com/login", "{}")

    assert result == {"success": True, "message": "ok"}
    assert client._session.session_id == provider_session_id
    client.create_session.assert_not_awaited()
    client.delete_session_strict.assert_not_awaited()


@pytest.mark.asyncio
async def test_take_control_never_trusts_process_cache_without_ledger_lookup():
    agent_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    cache_key = (agent_id, session_id, "browser")
    cached_client = SimpleNamespace(_session=SimpleNamespace(session_id="stale-provider"))
    _agentbay_sessions[cache_key] = (cached_client, datetime.now())
    lookup = AsyncMock(return_value=None)

    try:
        with patch(
            "app.services.agentbay_client.get_existing_agentbay_client_for_agent",
            lookup,
        ):
            with pytest.raises(agentbay_control.HTTPException) as exc_info:
                await agentbay_control._get_client(agent_id, session_id, "browser")
    finally:
        _agentbay_sessions.pop(cache_key, None)

    assert exc_info.value.status_code == 404
    lookup.assert_awaited_once_with(
        agent_id,
        image_type="browser",
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_existing_agentbay_client_rejects_closed_durable_binding():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    cache_key = (agent_id, session_id, "browser")
    cached_client = SimpleNamespace(_session=SimpleNamespace(session_id="provider-exact"))
    _agentbay_sessions[cache_key] = (cached_client, datetime.now())
    closed = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        chat_session_id=session_id,
        provider_session_id="provider-exact",
        image_type="browser",
        status="closed",
        context={"binding_version": 2},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    try:
        with (
            patch(
                "app.services.agentbay_client._load_agentbay_lane",
                AsyncMock(return_value=(tenant_id, user_id, session_id)),
            ),
            patch(
                "app.services.agentbay_client._get_active_agentbay_ledger",
                AsyncMock(return_value=closed),
            ),
            patch(
                "app.services.agentbay_client._configured_agentbay_client",
                AsyncMock(return_value=(SimpleNamespace(attach_session=AsyncMock()), {})),
            ) as configured,
        ):
            result = await get_existing_agentbay_client_for_agent(
                agent_id,
                image_type="browser",
                session_id=session_id,
            )
    finally:
        _agentbay_sessions.pop(cache_key, None)

    assert result is None
    assert cached_client._session is None
    configured.assert_awaited_once()


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _LedgerSession:
    def __init__(self, rows):
        self.rows = rows
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        return _RowsResult(self.rows)


def _ledger(*, user_id, session_id, status="active", binding_version=2):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        chat_session_id=session_id,
        status=status,
        context={"binding_version": binding_version},
        expires_at=None,
        last_used_at=None,
        started_at=None,
        close_reason=None,
        error_message=None,
        closed_at=None,
    )


@pytest.mark.asyncio
async def test_v2_cleanup_required_poisons_only_the_exact_user_chat_lane():
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    active = _ledger(user_id=user_id, session_id="chat-a")
    unrelated_cleanup = _ledger(
        user_id=other_user_id,
        session_id="chat-b",
        status="cleanup_required",
    )
    session = _LedgerSession([unrelated_cleanup, active])

    with patch("app.database.async_session", return_value=session):
        result = await _get_active_agentbay_ledger(
            agent_id=uuid.uuid4(),
            user_id=user_id,
            session_id="chat-a",
            image_type="browser_latest",
        )

    assert result is active
    assert unrelated_cleanup.status == "cleanup_required"


@pytest.mark.asyncio
async def test_legacy_cleanup_required_remains_agent_wide_fail_closed():
    user_id = uuid.uuid4()
    legacy_cleanup = _ledger(
        user_id=uuid.uuid4(),
        session_id="legacy-chat",
        status="cleanup_required",
        binding_version=1,
    )
    session = _LedgerSession([legacy_cleanup])

    with patch("app.database.async_session", return_value=session):
        result = await _get_active_agentbay_ledger(
            agent_id=uuid.uuid4(),
            user_id=user_id,
            session_id="chat-a",
            image_type="browser_latest",
        )

    assert result is legacy_cleanup


@pytest.mark.asyncio
async def test_duplicate_active_lane_is_quarantined_for_provider_cleanup():
    user_id = uuid.uuid4()
    keeper = _ledger(user_id=user_id, session_id="chat-a")
    duplicate = _ledger(user_id=user_id, session_id="chat-a")
    session = _LedgerSession([keeper, duplicate])

    with patch("app.database.async_session", return_value=session):
        result = await _get_active_agentbay_ledger(
            agent_id=uuid.uuid4(),
            user_id=user_id,
            session_id="chat-a",
            image_type="browser_latest",
        )

    assert result is duplicate
    assert duplicate.status == "cleanup_required"
    assert duplicate.close_reason == "duplicate_active_lane_cleanup_required"
    assert duplicate.closed_at is None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_can_close_orphan_cleanup_only_with_exact_out_of_band_proof():
    ledger_id = uuid.uuid4()
    provider_session_id = "provider-session-exact"
    ledger = SimpleNamespace(
        id=ledger_id,
        tenant_id=None,
        agent_id=None,
        user_id=None,
        status="cleanup_required",
        provider_session_id=provider_session_id,
        image_type="browser_latest",
        context={},
        close_reason=None,
        error_message="cleanup required",
        closed_at=None,
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_ScalarResult(ledger), _ScalarResult(ledger)]),
        rollback=AsyncMock(),
        commit=AsyncMock(),
        add=MagicMock(),
    )
    provider_client = AsyncMock()

    with patch(
        "app.services.agentbay_client._configured_agentbay_client",
        provider_client,
    ):
        result = await reconcile_agentbay_cleanup_required(
            ledger_id,
            AgentBayCleanupReconcileRequest(
                provider_session_id=provider_session_id,
                provider_deleted_out_of_band=True,
                verification_note=(
                    "Verified the exact provider session is absent in the AgentBay console"
                ),
            ),
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=db,
        )

    assert result["status"] == "closed"
    assert result["mode"] == "operator_confirmed_absent"
    assert ledger.status == "closed"
    db.rollback.assert_awaited_once()
    db.commit.assert_awaited_once()
    provider_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_releases_db_transaction_before_provider_cleanup():
    events: list[str] = []
    ledger_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    provider_session_id = "provider-session-exact"
    ledger = SimpleNamespace(
        id=ledger_id,
        tenant_id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        status="cleanup_required",
        provider_session_id=provider_session_id,
        image_type="browser_latest",
        context={},
        close_reason=None,
        error_message="cleanup required",
        closed_at=None,
    )

    async def execute(_statement):
        events.append("execute")
        return _ScalarResult(ledger)

    async def rollback():
        events.append("rollback")

    async def attach_session(_provider_session_id, _image_type):
        events.append("provider_attach")

    async def delete_session_strict():
        events.append("provider_delete")

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=execute),
        rollback=AsyncMock(side_effect=rollback),
        commit=AsyncMock(),
        add=MagicMock(),
    )
    client = SimpleNamespace(
        attach_session=AsyncMock(side_effect=attach_session),
        delete_session_strict=AsyncMock(side_effect=delete_session_strict),
    )

    with patch(
        "app.services.agentbay_client._configured_agentbay_client",
        AsyncMock(return_value=(client, {})),
    ):
        await reconcile_agentbay_cleanup_required(
            ledger_id,
            AgentBayCleanupReconcileRequest(
                provider_session_id=provider_session_id,
            ),
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=db,
        )

    assert events == [
        "execute",
        "rollback",
        "provider_attach",
        "provider_delete",
        "execute",
    ]


@pytest.mark.asyncio
async def test_admin_provider_cleanup_propagates_cancellation():
    ledger_id = uuid.uuid4()
    provider_session_id = "provider-session-exact"
    ledger = SimpleNamespace(
        id=ledger_id,
        agent_id=uuid.uuid4(),
        status="cleanup_required",
        provider_session_id=provider_session_id,
        image_type="browser_latest",
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_ScalarResult(ledger)),
        rollback=AsyncMock(),
    )
    client = SimpleNamespace(
        attach_session=AsyncMock(side_effect=asyncio.CancelledError()),
        delete_session_strict=AsyncMock(),
    )

    with patch(
        "app.services.agentbay_client._configured_agentbay_client",
        AsyncMock(return_value=(client, {})),
    ):
        with pytest.raises(asyncio.CancelledError):
            await reconcile_agentbay_cleanup_required(
                ledger_id,
                AgentBayCleanupReconcileRequest(
                    provider_session_id=provider_session_id,
                ),
                current_user=SimpleNamespace(id=uuid.uuid4()),
                db=db,
            )

    db.rollback.assert_awaited()
    client.delete_session_strict.assert_not_awaited()
