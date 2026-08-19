import base64
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.models.subscription import CreditPack, PaymentOrder, Plan
from app.services.billing_provider import WeChatBillingProvider, get_billing_provider

API_V3_KEY = "0123456789abcdef0123456789abcdef"


def _settings(**overrides):
    base = dict(
        WECHAT_PAY_APPID="wx1234567890abcdef",
        WECHAT_PAY_MCHID="1230000109",
        WECHAT_PAY_SERIAL_NO="SERIAL123",
        WECHAT_PAY_PRIVATE_KEY="",
        WECHAT_PAY_PRIVATE_KEY_PATH="",
        WECHAT_PAY_API_V3_KEY=API_V3_KEY,
        WECHAT_PAY_NOTIFY_URL="https://example.com/api/subscription/billing/webhook/wechat",
        WECHAT_PAY_API_BASE_URL="https://api.mch.weixin.qq.com",
        BILLING_USD_CNY_RATE=7.2,
        PUBLIC_BASE_URL="https://example.com",
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


def _make_provider(**overrides) -> WeChatBillingProvider:
    overrides.setdefault("WECHAT_PAY_PRIVATE_KEY", _private_key_pem())
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


def test_get_billing_provider_supports_wechat():
    with patch(
        "app.services.billing_provider.get_settings",
        return_value=_settings(WECHAT_PAY_PRIVATE_KEY=_private_key_pem()),
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
    assert result.session_id == str(order.id)
    assert result.session_url == "weixin://wxpay/bizpayurl?pr=abc123"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v3/pay/transactions/native"
    payload = captured["payload"]
    assert payload["out_trade_no"] == str(order.id)
    assert payload["amount"] == {"total": round(16000 * 7.2), "currency": "CNY"}
    assert payload["notify_url"] == "https://example.com/api/subscription/billing/webhook/wechat"


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
    provider = _make_provider()
    order_id = uuid.uuid4()
    payload = _encrypted_event(
        {"out_trade_no": str(order_id), "trade_state": "SUCCESS", "transaction_id": "4200001234567890"}
    )
    event = await provider.verify_webhook(payload, None)
    assert event["id"] == "evt-wechat-1"
    assert event["type"] == "TRANSACTION.SUCCESS"
    assert event["decrypted"]["out_trade_no"] == str(order_id)


async def test_verify_webhook_rejects_forged_ciphertext():
    provider = _make_provider()
    payload = _encrypted_event({"out_trade_no": str(uuid.uuid4())}, key="f" * 32)
    with pytest.raises(ValueError, match="decryption failed"):
        await provider.verify_webhook(payload, None)


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
    assert state.provider_session_id == str(order_id)
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


# --- Payment-domain gate (checkout must be initiated from PUBLIC_BASE_URL) ---


def _gate_request(host: str):
    return SimpleNamespace(headers={"host": host})


def _gate_settings(provider: str = "wechat", public_base_url: str = "https://opc.rama-server.com"):
    return SimpleNamespace(BILLING_PROVIDER=provider, PUBLIC_BASE_URL=public_base_url)


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


def test_payment_origin_gate_skips_manual_provider():
    from app.api.subscription import _enforce_payment_origin

    with patch("app.api.subscription.get_settings", return_value=_gate_settings(provider="manual")):
        _enforce_payment_origin(_gate_request("opc.reeftotem.ai"))


def test_payment_origin_gate_skips_without_public_base_url():
    from app.api.subscription import _enforce_payment_origin

    with patch("app.api.subscription.get_settings", return_value=_gate_settings(public_base_url="")):
        _enforce_payment_origin(_gate_request("opc.reeftotem.ai"))
