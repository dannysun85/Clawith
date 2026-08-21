import base64
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.models.subscription import CreditPack, PaymentOrder, Plan
from app.services.billing_provider import (
    WeChatBillingProvider,
    billing_provider_readiness,
    get_billing_provider,
    resolved_payment_base_url,
)

API_V3_KEY = "0123456789abcdef0123456789abcdef"


def _settings(**overrides):
    base = dict(
        WECHAT_PAY_APPID="wx1234567890abcdef",
        WECHAT_PAY_MCHID="1230000109",
        WECHAT_PAY_SERIAL_NO="SERIAL123",
        WECHAT_PAY_PRIVATE_KEY="",
        WECHAT_PAY_PRIVATE_KEY_PATH="",
        WECHAT_PAY_API_V3_KEY=API_V3_KEY,
        WECHAT_PAY_PLATFORM_PUBLIC_KEY="",
        WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH="",
        WECHAT_PAY_PLATFORM_SERIAL_NO="PLATFORM-SERIAL-123",
        WECHAT_PAY_NOTIFY_URL="https://example.com/api/subscription/billing/webhook/wechat",
        WECHAT_PAY_API_BASE_URL="https://api.mch.weixin.qq.com",
        WECHAT_PAY_WEBHOOK_MAX_SKEW_SECONDS=300,
        WECHAT_PAY_ORDER_EXPIRE_MINUTES=120,
        BILLING_USD_CNY_RATE=7.2,
        PUBLIC_BASE_URL="https://example.com",
        PAYMENT_BASE_URL="",
        BILLING_PROVIDER="wechat",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _public_key_pem(key) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def _make_provider(**overrides) -> WeChatBillingProvider:
    overrides.setdefault("WECHAT_PAY_PRIVATE_KEY", _private_key_pem())
    if not overrides.get("WECHAT_PAY_PLATFORM_PUBLIC_KEY"):
        platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        overrides["WECHAT_PAY_PLATFORM_PUBLIC_KEY"] = _public_key_pem(platform_key)
    with patch("app.services.billing_provider.get_settings", return_value=_settings(**overrides)):
        return WeChatBillingProvider()


def _order(**overrides) -> PaymentOrder:
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        plan_id=uuid.uuid4(),
        amount_cents=16000,
        currency="USD",
        status="pending",
    )
    base.update(overrides)
    return PaymentOrder(**base)


def _encrypted_event(transaction: dict, key: str = API_V3_KEY) -> bytes:
    nonce = uuid.uuid4().hex[:16]
    associated_data = "transaction"
    ciphertext = AESGCM(key.encode("utf-8")).encrypt(
        nonce.encode("utf-8"),
        json.dumps(transaction).encode("utf-8"),
        associated_data.encode("utf-8"),
    )
    return json.dumps(
        {
            "id": "evt-wechat-1",
            "event_type": "TRANSACTION.SUCCESS",
            "resource": {
                "algorithm": "AEAD_AES_256_GCM",
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "nonce": nonce,
                "associated_data": associated_data,
            },
        }
    ).encode("utf-8")


def _signed_headers(payload: bytes, platform_key, **overrides) -> dict[str, str]:
    timestamp = str(overrides.get("timestamp", int(time.time())))
    nonce = str(overrides.get("nonce", "callback-nonce"))
    message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + payload + b"\n"
    signature = base64.b64encode(
        platform_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    return {
        "Wechatpay-Signature": str(overrides.get("signature", signature)),
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Serial": str(overrides.get("serial", "PLATFORM-SERIAL-123")),
    }


def test_get_billing_provider_supports_wechat():
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with patch(
        "app.services.billing_provider.get_settings",
        return_value=_settings(
            WECHAT_PAY_PRIVATE_KEY=_private_key_pem(),
            WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key),
        ),
    ):
        provider = get_billing_provider()
    assert isinstance(provider, WeChatBillingProvider)
    assert provider.name == "wechat"


def test_wechat_provider_requires_merchant_config():
    with patch(
        "app.services.billing_provider.get_settings",
        return_value=_settings(WECHAT_PAY_MCHID="", WECHAT_PAY_PRIVATE_KEY=_private_key_pem()),
    ):
        with pytest.raises(ValueError, match="WECHAT_PAY_MCHID"):
            WeChatBillingProvider()


def test_wechat_provider_requires_private_key():
    with patch("app.services.billing_provider.get_settings", return_value=_settings()):
        with pytest.raises(ValueError, match="WECHAT_PAY_PRIVATE_KEY"):
            WeChatBillingProvider()


def test_billing_readiness_distinguishes_manual_misconfigured_and_ready_wechat():
    manual = billing_provider_readiness(_settings(BILLING_PROVIDER="manual"))
    assert manual.status == "manual"
    assert manual.checkout_enabled is True
    assert manual.native_payment_enabled is False
    assert manual.webhook_ready is False

    incomplete = billing_provider_readiness(
        _settings(
            WECHAT_PAY_MCHID="",
            WECHAT_PAY_PRIVATE_KEY="",
            WECHAT_PAY_PLATFORM_PUBLIC_KEY="",
        )
    )
    assert incomplete.status == "misconfigured"
    assert incomplete.checkout_enabled is False
    assert "WECHAT_PAY_MCHID" in incomplete.missing_config
    assert "WECHAT_PAY_PRIVATE_KEY|WECHAT_PAY_PRIVATE_KEY_PATH" in incomplete.missing_config
    assert "WECHAT_PAY_PLATFORM_PUBLIC_KEY|WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH" in incomplete.missing_config

    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ready = billing_provider_readiness(
        _settings(
            WECHAT_PAY_PRIVATE_KEY=_private_key_pem(),
            WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key),
        )
    )
    assert ready.status == "ready"
    assert ready.checkout_enabled is True
    assert ready.native_payment_enabled is True
    assert ready.webhook_ready is True
    assert ready.missing_config == ()
    assert ready.issues == ()


def test_billing_readiness_rejects_bad_api_key_and_non_public_callback():
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    readiness = billing_provider_readiness(
        _settings(
            WECHAT_PAY_API_V3_KEY="short",
            WECHAT_PAY_PRIVATE_KEY=_private_key_pem(),
            WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key),
            WECHAT_PAY_NOTIFY_URL="http://localhost/callback",
        )
    )
    assert readiness.checkout_enabled is False
    assert set(readiness.issues) == {
        "wechat_api_v3_key_must_be_32_bytes",
        "wechat_notify_url_must_be_public_https",
    }


def test_readiness_cli_payload_is_secret_free():
    from app.scripts.check_billing_readiness import readiness_payload

    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = _settings(
        WECHAT_PAY_PRIVATE_KEY="merchant-private-secret",
        WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key),
    )
    with patch("app.services.billing_provider.get_settings", return_value=settings):
        payload = readiness_payload()

    rendered = json.dumps(payload)
    assert payload["provider"] == "wechat"
    assert "merchant-private-secret" not in rendered
    assert API_V3_KEY not in rendered


async def test_create_subscription_checkout_converts_usd_to_cny():
    provider = _make_provider()
    order = _order(amount_cents=16000, currency="USD")
    plan = Plan(id=order.plan_id, code="pro", name="Pro")

    captured = {}

    async def fake_request(method, path, payload=None):
        captured.update({"method": method, "path": path, "payload": payload})
        return {"code_url": "weixin://wxpay/bizpayurl?pr=abc123"}

    provider._request = fake_request  # type: ignore[assignment]
    result = await provider.create_subscription_checkout(order=order, plan=plan, period="monthly", seats=1)

    assert result.provider == "wechat"
    assert result.session_id == order.id.hex
    assert result.session_url == "weixin://wxpay/bizpayurl?pr=abc123"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v3/pay/transactions/native"
    payload = captured["payload"]
    # WeChat Pay caps out_trade_no at 32 chars: use the UUID's hex form, not the
    # 36-char hyphenated string (which WeChat rejects with PARAM_ERROR).
    assert payload["out_trade_no"] == order.id.hex
    assert len(payload["out_trade_no"]) <= 32
    assert payload["out_trade_no"].isalnum()
    assert payload["amount"] == {"total": round(16000 * 7.2), "currency": "CNY"}
    assert payload["notify_url"] == "https://example.com/api/subscription/billing/webhook/wechat"
    assert payload["attach"] == f"tenant:{order.tenant_id.hex}"
    assert "time_expire" in payload


async def test_create_native_order_omits_time_expire_when_disabled():
    provider = _make_provider(WECHAT_PAY_ORDER_EXPIRE_MINUTES=0)
    order = _order(amount_cents=7000, currency="CNY")
    plan = Plan(id=order.plan_id, code="pro", name="Pro")

    captured = {}

    async def fake_request(method, path, payload=None):
        captured["payload"] = payload
        return {"code_url": "weixin://wxpay/bizpayurl?pr=abc123"}

    provider._request = fake_request  # type: ignore[assignment]
    await provider.create_subscription_checkout(order=order, plan=plan, period="monthly", seats=1)
    assert "time_expire" not in captured["payload"]


async def test_create_topup_checkout_keeps_cny_amount():
    provider = _make_provider()
    order = _order(type="topup", plan_id=None, amount_cents=7000, currency="CNY")
    pack = CreditPack(id=uuid.uuid4(), code="boost_50k", name="50,000 Credits")

    captured = {}

    async def fake_request(method, path, payload=None):
        captured["payload"] = payload
        return {"code_url": "weixin://wxpay/bizpayurl?pr=topup"}

    provider._request = fake_request  # type: ignore[assignment]
    result = await provider.create_topup_checkout(order=order, pack=pack)

    assert result.session_url == "weixin://wxpay/bizpayurl?pr=topup"
    assert captured["payload"]["amount"] == {"total": 7000, "currency": "CNY"}


async def test_create_native_order_rejects_zero_amount():
    provider = _make_provider()
    order = _order(amount_cents=0, currency="CNY")
    plan = Plan(id=order.plan_id, code="free", name="Free")
    with pytest.raises(ValueError, match="minimum"):
        await provider.create_subscription_checkout(order=order, plan=plan, period="monthly", seats=1)


async def test_verify_webhook_decrypts_resource():
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = _make_provider(WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key))
    order_id = uuid.uuid4()
    payload = _encrypted_event(
        {"out_trade_no": str(order_id), "trade_state": "SUCCESS", "transaction_id": "4200001234567890"}
    )
    event = await provider.verify_webhook(payload, None, headers=_signed_headers(payload, platform_key))
    assert event["id"] == "evt-wechat-1"
    assert event["type"] == "TRANSACTION.SUCCESS"
    assert event["decrypted"]["out_trade_no"] == str(order_id)


async def test_verify_webhook_rejects_forged_ciphertext():
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = _make_provider(WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key))
    payload = _encrypted_event({"out_trade_no": str(uuid.uuid4())}, key="f" * 32)
    with pytest.raises(ValueError, match="decryption failed"):
        await provider.verify_webhook(payload, None, headers=_signed_headers(payload, platform_key))


async def test_verify_webhook_rejects_missing_or_forged_http_signature():
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = _make_provider(WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key))
    payload = _encrypted_event({"out_trade_no": uuid.uuid4().hex})

    with pytest.raises(ValueError, match="Missing WeChat callback signature headers"):
        await provider.verify_webhook(payload, None)

    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match="Invalid WeChat callback signature"):
        await provider.verify_webhook(payload, None, headers=_signed_headers(payload, attacker_key))


async def test_verify_webhook_rejects_stale_timestamp_wrong_serial_and_signtest():
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = _make_provider(WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key))
    payload = _encrypted_event({"out_trade_no": uuid.uuid4().hex})

    stale_headers = _signed_headers(payload, platform_key, timestamp=int(time.time()) - 301)
    with pytest.raises(ValueError, match="Stale WeChat callback timestamp"):
        await provider.verify_webhook(payload, None, headers=stale_headers)

    wrong_serial = _signed_headers(payload, platform_key, serial="UNKNOWN")
    with pytest.raises(ValueError, match="Unknown WeChat callback signing serial"):
        await provider.verify_webhook(payload, None, headers=wrong_serial)

    signtest = _signed_headers(payload, platform_key, signature="WECHATPAY/SIGNTEST/not-real")
    with pytest.raises(ValueError, match="signature test value rejected"):
        await provider.verify_webhook(payload, None, headers=signtest)


async def test_wechat_api_response_requires_valid_platform_signature():
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = _make_provider(WECHAT_PAY_PLATFORM_PUBLIC_KEY=_public_key_pem(platform_key))
    response_body = b'{"trade_state":"NOTPAY"}'
    response = httpx.Response(
        200,
        content=response_body,
        headers=_signed_headers(response_body, platform_key),
    )

    class ClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            return response

    with patch("app.services.billing_provider.httpx.AsyncClient", return_value=ClientStub()):
        result = await provider._request("GET", "/v3/pay/transactions/out-trade-no/order")
    assert result == {"trade_state": "NOTPAY"}


async def test_wechat_api_response_rejects_missing_platform_signature():
    provider = _make_provider()
    response = httpx.Response(200, content=b'{"trade_state":"NOTPAY"}')

    class ClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            return response

    with patch("app.services.billing_provider.httpx.AsyncClient", return_value=ClientStub()):
        with pytest.raises(ValueError, match="Missing WeChat callback signature headers"):
            await provider._request("GET", "/v3/pay/transactions/out-trade-no/order")


async def test_load_remote_event_state_confirms_paid_via_api():
    provider = _make_provider()
    order_id = uuid.uuid4()
    provider._request = AsyncMock(  # type: ignore[assignment]
        return_value={"trade_state": "SUCCESS", "transaction_id": "4200009999"}
    )
    event = {
        "id": "evt-wechat-2",
        "type": "TRANSACTION.SUCCESS",
        "decrypted": {"out_trade_no": str(order_id), "trade_state": "USERPAYING"},
    }
    state = await provider.load_remote_event_state(event)
    assert state.order_id == order_id
    assert state.status == "paid"  # remote API wins over the stale webhook body
    assert state.provider_session_id == order_id.hex
    assert state.provider_payment_id == "4200009999"


async def test_load_remote_event_state_falls_back_to_webhook_body():
    provider = _make_provider()
    order_id = uuid.uuid4()
    provider._request = AsyncMock(side_effect=ValueError("network down"))  # type: ignore[assignment]
    event = {
        "id": "evt-wechat-3",
        "type": "TRANSACTION.SUCCESS",
        "decrypted": {"out_trade_no": str(order_id), "trade_state": "SUCCESS", "transaction_id": "4200001111"},
    }
    state = await provider.load_remote_event_state(event)
    assert state.status == "paid"
    assert state.order_id == order_id


async def test_load_remote_event_state_ignores_missing_out_trade_no():
    provider = _make_provider()
    state = await provider.load_remote_event_state({"id": "evt-x", "type": "TRANSACTION.SUCCESS", "decrypted": {}})
    assert state.order_id is None
    assert state.status == "ignored"


# --- Payment-domain gate (checkout must be initiated from the payment origin) ---


def _gate_request(host: str):
    return SimpleNamespace(headers={"host": host})


def _gate_settings(
    provider: str = "wechat",
    public_base_url: str = "https://opc.rama-server.com",
    payment_base_url: str = "",
):
    return SimpleNamespace(
        BILLING_PROVIDER=provider,
        PUBLIC_BASE_URL=public_base_url,
        PAYMENT_BASE_URL=payment_base_url,
    )


def test_payment_origin_gate_rejects_non_payment_domain():
    from fastapi import HTTPException

    from app.api.subscription import _enforce_payment_origin

    with patch("app.api.subscription.get_settings", return_value=_gate_settings()):
        with pytest.raises(HTTPException) as exc_info:
            _enforce_payment_origin(_gate_request("opc.reeftotem.ai"))
    assert exc_info.value.status_code == 403
    assert "opc.rama-server.com" in str(exc_info.value.detail)


def test_payment_origin_gate_allows_payment_domain():
    from app.api.subscription import _enforce_payment_origin

    with patch("app.api.subscription.get_settings", return_value=_gate_settings()):
        _enforce_payment_origin(_gate_request("opc.rama-server.com"))


def test_payment_origin_gate_ignores_request_port():
    from app.api.subscription import _enforce_payment_origin

    with patch("app.api.subscription.get_settings", return_value=_gate_settings()):
        _enforce_payment_origin(_gate_request("opc.rama-server.com:8443"))


def test_payment_origin_gate_honors_forwarded_host():
    from app.api.subscription import _enforce_payment_origin

    request = SimpleNamespace(headers={
        "host": "backend:8000",
        "x-forwarded-host": "opc.rama-server.com",
    })
    with patch("app.api.subscription.get_settings", return_value=_gate_settings()):
        _enforce_payment_origin(request)


def test_payment_origin_gate_rejects_mismatched_forwarded_host():
    from fastapi import HTTPException

    from app.api.subscription import _enforce_payment_origin

    request = SimpleNamespace(headers={
        "host": "backend:8000",
        "x-forwarded-host": "opc.reeftotem.ai",
    })
    with patch("app.api.subscription.get_settings", return_value=_gate_settings()):
        with pytest.raises(HTTPException) as exc_info:
            _enforce_payment_origin(request)
    assert exc_info.value.status_code == 403


def test_payment_origin_gate_skips_manual_provider():
    from app.api.subscription import _enforce_payment_origin

    with patch("app.api.subscription.get_settings", return_value=_gate_settings(provider="manual")):
        _enforce_payment_origin(_gate_request("opc.reeftotem.ai"))


def test_payment_origin_gate_skips_without_public_base_url():
    from app.api.subscription import _enforce_payment_origin

    with patch("app.api.subscription.get_settings", return_value=_gate_settings(public_base_url="")):
        _enforce_payment_origin(_gate_request("opc.reeftotem.ai"))


def test_payment_origin_gate_uses_payment_base_url_not_product_url():
    from fastapi import HTTPException

    from app.api.subscription import _enforce_payment_origin, _payment_host

    settings = _gate_settings(
        public_base_url="https://opc.reeftotem.ai",
        payment_base_url="https://opc.rama-server.com",
    )
    with patch("app.api.subscription.get_settings", return_value=settings):
        assert _payment_host() == "opc.rama-server.com"
        _enforce_payment_origin(_gate_request("opc.rama-server.com"))
        with pytest.raises(HTTPException) as exc_info:
            _enforce_payment_origin(_gate_request("opc.reeftotem.ai"))
    assert exc_info.value.status_code == 403
    assert "opc.rama-server.com" in str(exc_info.value.detail)


def test_resolved_payment_base_url_prefers_payment_origin():
    settings = _settings(
        PUBLIC_BASE_URL="https://opc.reeftotem.ai",
        PAYMENT_BASE_URL="https://opc.rama-server.com/",
    )
    assert resolved_payment_base_url(settings) == "https://opc.rama-server.com"


async def test_notify_url_prefers_payment_base_url():
    provider = _make_provider(
        WECHAT_PAY_NOTIFY_URL="",
        PUBLIC_BASE_URL="https://opc.reeftotem.ai",
        PAYMENT_BASE_URL="https://opc.rama-server.com",
    )
    assert provider._notify_url() == "https://opc.rama-server.com/api/subscription/billing/webhook/wechat"


async def test_query_order_state_maps_trade_states():
    provider = _make_provider()
    order = _order(status="pending")
    provider._request = AsyncMock(  # type: ignore[assignment]
        return_value={"trade_state": "SUCCESS", "transaction_id": "4200007777"}
    )

    state = await provider.query_order_state(order)

    assert state is not None
    assert state.order_id == order.id
    assert state.status == "paid"
    assert state.provider_payment_id == "4200007777"
    provider._request.assert_awaited_once_with(
        "GET", f"/v3/pay/transactions/out-trade-no/{order.id.hex}?mchid=1230000109"
    )


async def test_query_order_state_maps_closed_to_canceled():
    provider = _make_provider()
    order = _order(status="pending")
    provider._request = AsyncMock(return_value={"trade_state": "CLOSED"})  # type: ignore[assignment]

    state = await provider.query_order_state(order)

    assert state is not None
    assert state.status == "canceled"


async def test_close_order_calls_wechat_close_api():
    provider = _make_provider()
    order = _order(status="pending")
    provider._request = AsyncMock(return_value={})  # type: ignore[assignment]

    assert await provider.close_order(order) is True
    provider._request.assert_awaited_once_with(
        "POST",
        f"/v3/pay/transactions/out-trade-no/{order.id.hex}/close",
        {"mchid": "1230000109"},
    )


def test_wechat_paid_state_requires_exact_order_amount_tenant_and_merchant_contract():
    from app.services.billing_events import PaymentProviderEventState

    provider = _make_provider()
    order = _order(amount_cents=7000, currency="CNY")
    valid = dict(
        provider="wechat",
        event_id="evt-contract",
        event_type="TRANSACTION.SUCCESS",
        order_id=order.id,
        status="paid",
        provider_session_id=order.id.hex,
        provider_payment_id="4200000001",
        amount_cents=7000,
        currency="CNY",
        merchant_id="1230000109",
        app_id="wx1234567890abcdef",
        trade_type="NATIVE",
        tenant_id=order.tenant_id,
    )
    provider.validate_event_state(order, PaymentProviderEventState(**valid))

    mismatches = (
        ("order_id", uuid.uuid4(), "order mismatch"),
        ("amount_cents", 6999, "amount mismatch"),
        ("currency", "USD", "currency mismatch"),
        ("merchant_id", "other", "merchant mismatch"),
        ("app_id", "other", "appid mismatch"),
        ("trade_type", "JSAPI", "trade type mismatch"),
        ("tenant_id", uuid.uuid4(), "tenant mismatch"),
    )
    for field, value, message in mismatches:
        with pytest.raises(ValueError, match=message):
            provider.validate_event_state(
                order,
                PaymentProviderEventState(**{**valid, field: value}),
            )


def test_wechat_refund_state_is_explicit():
    provider = _make_provider()
    assert provider._order_status_from_trade_state("REFUND") == "refunded"


async def test_wechat_verified_partial_refund_preserves_refund_delta():
    provider = _make_provider()
    order = _order(amount_cents=7000, currency="CNY")
    provider._request = AsyncMock(  # type: ignore[assignment]
        return_value={
            "out_trade_no": order.id.hex,
            "trade_state": "SUCCESS",
            "transaction_id": "4200007777",
            "mchid": "1230000109",
            "appid": "wx1234567890abcdef",
            "trade_type": "NATIVE",
            "attach": f"tenant:{order.tenant_id.hex}",
            "amount": {"total": 7000, "currency": "CNY"},
        }
    )

    state = await provider.load_remote_event_state(
        {
            "id": "evt-refund-partial",
            "type": "REFUND.SUCCESS",
            "decrypted": {
                "out_trade_no": order.id.hex,
                "refund_status": "SUCCESS",
                "amount": {"total": 7000, "refund": 1250, "currency": "CNY"},
            },
        }
    )

    assert state.status == "refunded"
    assert state.refund_amount_cents == 1250
    assert state.amount_cents == 7000
    provider.validate_event_state(order, state)


async def test_query_order_state_returns_none_on_api_error():
    provider = _make_provider()
    order = _order(status="pending")
    provider._request = AsyncMock(side_effect=ValueError("ORDERNOTEXIST"))  # type: ignore[assignment]

    assert await provider.query_order_state(order) is None


async def test_manual_provider_query_order_state_unsupported():
    from app.services.billing_provider import BillingProvider

    provider = BillingProvider()
    assert await provider.query_order_state(_order()) is None
