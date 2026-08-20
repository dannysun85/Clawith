#!/usr/bin/env python3
"""Provider-free PostgreSQL smoke for the WeChat payment safety contract.

The harness generates ephemeral RSA/AES material, stubs only the outbound
WeChat API, and exercises the real database transaction path.  It never calls
WeChat, creates a real provider order, or charges money.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, func, select

from app.database import async_session, engine
from app.models.agent import Agent  # noqa: F401 - register FK metadata
from app.models.subscription import (
    BillingWebhookEvent,
    CreditBalance,
    CreditTransaction,
    PaymentOrder,
)
from app.models.tenant import Tenant
from app.models.user import User  # noqa: F401 - register FK metadata
from app.services.billing_events import (
    close_expired_pending_order,
    process_billing_webhook_event,
    sync_pending_order_from_provider,
)
from app.services.billing_provider import WeChatBillingProvider


API_V3_KEY = "0123456789abcdef0123456789abcdef"
APP_ID = "wx-provider-free-smoke"
MERCHANT_ID = "1900000001"
PLATFORM_SERIAL = "PLATFORM-SMOKE-SERIAL"


class TwoPartyBarrier:
    def __init__(self) -> None:
        self._arrivals = 0
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        self._arrivals += 1
        if self._arrivals >= 2:
            self._ready.set()
        await self._ready.wait()


def private_key_pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def public_key_pem(key) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def encrypted_event(event_id: str, transaction: dict[str, object]) -> bytes:
    nonce = uuid.uuid4().hex[:16]
    associated_data = "transaction"
    ciphertext = AESGCM(API_V3_KEY.encode("utf-8")).encrypt(
        nonce.encode("utf-8"),
        json.dumps(transaction, separators=(",", ":")).encode("utf-8"),
        associated_data.encode("utf-8"),
    )
    return json.dumps(
        {
            "id": event_id,
            "event_type": "TRANSACTION.SUCCESS",
            "resource": {
                "algorithm": "AEAD_AES_256_GCM",
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "nonce": nonce,
                "associated_data": associated_data,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def signed_headers(payload: bytes, key) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + payload + b"\n"
    signature = base64.b64encode(
        key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    return {
        "Wechatpay-Signature": signature,
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Serial": PLATFORM_SERIAL,
    }


def remote_state(
    order: PaymentOrder,
    *,
    status: str = "SUCCESS",
    amount: int | None = None,
    tenant_id: uuid.UUID | None = None,
    out_trade_no: str | None = None,
) -> dict[str, object]:
    return {
        "appid": APP_ID,
        "mchid": MERCHANT_ID,
        "out_trade_no": out_trade_no or order.id.hex,
        "transaction_id": f"provider-free-{order.id.hex[:12]}",
        "trade_type": "NATIVE",
        "trade_state": status,
        "attach": f"tenant:{(tenant_id or order.tenant_id).hex}",
        "amount": {"total": amount if amount is not None else order.amount_cents, "currency": "CNY"},
    }


async def main() -> None:
    tenant_id = uuid.uuid4()
    orders: list[PaymentOrder] = []
    event_ids = {
        "paid": f"smoke-paid-{uuid.uuid4().hex}",
        "amount": f"smoke-amount-{uuid.uuid4().hex}",
        "tenant": f"smoke-tenant-{uuid.uuid4().hex}",
        "order": f"smoke-order-{uuid.uuid4().hex}",
        "refund": f"smoke-refund-{uuid.uuid4().hex}",
    }
    merchant_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = SimpleNamespace(
        WECHAT_PAY_APPID=APP_ID,
        WECHAT_PAY_MCHID=MERCHANT_ID,
        WECHAT_PAY_SERIAL_NO="MERCHANT-SMOKE-SERIAL",
        WECHAT_PAY_PRIVATE_KEY=private_key_pem(merchant_key),
        WECHAT_PAY_PRIVATE_KEY_PATH="",
        WECHAT_PAY_API_V3_KEY=API_V3_KEY,
        WECHAT_PAY_PLATFORM_PUBLIC_KEY=public_key_pem(platform_key),
        WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH="",
        WECHAT_PAY_PLATFORM_SERIAL_NO=PLATFORM_SERIAL,
        WECHAT_PAY_NOTIFY_URL="https://payments.example.test/api/subscription/billing/webhook/wechat",
        WECHAT_PAY_API_BASE_URL="https://api.mch.weixin.qq.com",
        WECHAT_PAY_WEBHOOK_MAX_SKEW_SECONDS=300,
        WECHAT_PAY_ORDER_EXPIRE_MINUTES=120,
        BILLING_USD_CNY_RATE=7.0,
        PUBLIC_BASE_URL="https://payments.example.test",
        PAYMENT_BASE_URL="https://payments.example.test",
        BILLING_PROVIDER="wechat",
    )

    with patch("app.services.billing_provider.get_settings", return_value=settings):
        provider = WeChatBillingProvider()

    paid_order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    amount_order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    tenant_order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    order_binding = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    expired_order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    orders.extend((paid_order, amount_order, tenant_order, order_binding, expired_order))
    for order in orders:
        order.provider_session_id = order.id.hex

    states = {
        paid_order.id.hex: remote_state(paid_order),
        amount_order.id.hex: remote_state(amount_order, amount=99),
        tenant_order.id.hex: remote_state(tenant_order, tenant_id=uuid.uuid4()),
        order_binding.id.hex: remote_state(order_binding, out_trade_no=uuid.uuid4().hex),
        expired_order.id.hex: remote_state(expired_order, status="NOTPAY"),
    }
    barrier = TwoPartyBarrier()
    paid_queries = 0
    close_calls = 0

    async def fake_request(method: str, path: str, payload=None):
        nonlocal close_calls, paid_queries
        if method == "GET" and "/out-trade-no/" in path:
            out_trade_no = path.split("/out-trade-no/", 1)[1].split("?", 1)[0]
            if out_trade_no == paid_order.id.hex and paid_queries < 2:
                paid_queries += 1
                await barrier.wait()
            return states[out_trade_no]
        if method == "POST" and path.endswith("/close"):
            close_calls += 1
            return {}
        raise AssertionError(f"unexpected provider call: {method} {path} {payload!r}")

    provider._request = fake_request  # type: ignore[method-assign]

    async with async_session() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="WeChat Provider-Free PostgreSQL Smoke",
                slug=f"wechat-payment-smoke-{tenant_id.hex[:12]}",
                im_provider="web_only",
                is_active=True,
            )
        )
        await db.flush()
        db.add(CreditBalance(tenant_id=tenant_id, balance=0, reserved=0))
        db.add_all(orders)
        await db.commit()

    paid_payload = encrypted_event(event_ids["paid"], {"out_trade_no": paid_order.id.hex})
    paid_headers = signed_headers(paid_payload, platform_key)

    async def process_paid_once() -> dict[str, object]:
        async with async_session() as db:
            result = await process_billing_webhook_event(
                db,
                provider_name="wechat",
                payload=paid_payload,
                signature=None,
                signature_headers=paid_headers,
                provider=provider,
            )
            await db.commit()
            return result

    try:
        concurrent_results = await asyncio.gather(process_paid_once(), process_paid_once())
        statuses = sorted(str(result["status"]) for result in concurrent_results)
        assert statuses == ["duplicate", "processed"]

        forged_headers = {**paid_headers, "Wechatpay-Signature": base64.b64encode(b"forged").decode("ascii")}
        try:
            await provider.verify_webhook(paid_payload, None, headers=forged_headers)
        except ValueError as exc:
            assert "Invalid WeChat callback signature" in str(exc)
        else:
            raise AssertionError("forged signature was accepted")

        mismatch_cases = (
            ("amount", amount_order, "amount mismatch"),
            ("tenant", tenant_order, "tenant mismatch"),
            ("order", order_binding, "Payment order not found"),
        )
        for case, order, expected_error in mismatch_cases:
            payload = encrypted_event(event_ids[case], {"out_trade_no": order.id.hex})
            async with async_session() as db:
                try:
                    await process_billing_webhook_event(
                        db,
                        provider_name="wechat",
                        payload=payload,
                        signature=None,
                        signature_headers=signed_headers(payload, platform_key),
                        provider=provider,
                    )
                except ValueError as exc:
                    assert expected_error in str(exc)
                    await db.rollback()
                else:
                    raise AssertionError(f"{case} mismatch was accepted")

        async with async_session() as db:
            expired = await db.get(PaymentOrder, expired_order.id)
            assert expired is not None
            with patch(
                "app.services.billing_provider.get_billing_provider",
                return_value=provider,
            ):
                assert await sync_pending_order_from_provider(db, expired) is False
                assert await close_expired_pending_order(db, expired) is True
            await db.commit()

        states[paid_order.id.hex] = remote_state(paid_order, status="REFUND")
        refund_payload = encrypted_event(event_ids["refund"], {"out_trade_no": paid_order.id.hex})
        async with async_session() as db:
            result = await process_billing_webhook_event(
                db,
                provider_name="wechat",
                payload=refund_payload,
                signature=None,
                signature_headers=signed_headers(refund_payload, platform_key),
                provider=provider,
            )
            assert result["status"] == "processed"
            await db.commit()

        async with async_session() as db:
            paid = await db.get(PaymentOrder, paid_order.id)
            expired = await db.get(PaymentOrder, expired_order.id)
            balance = await db.get(CreditBalance, tenant_id)
            transaction_count = (
                await db.execute(
                    select(func.count(CreditTransaction.id)).where(
                        CreditTransaction.tenant_id == tenant_id
                    )
                )
            ).scalar_one()
            webhook_count = (
                await db.execute(
                    select(func.count(BillingWebhookEvent.id)).where(
                        BillingWebhookEvent.provider == "wechat",
                        BillingWebhookEvent.event_id.in_(event_ids.values()),
                    )
                )
            ).scalar_one()
            assert paid is not None and paid.status == "refunded"
            assert expired is not None and expired.status == "canceled"
            assert balance is not None and balance.balance == 0 and balance.reserved == 0
            assert transaction_count == 2
            assert webhook_count == 2
            assert close_calls == 1

        print(
            json.dumps(
                {
                    "provider_calls": "stubbed",
                    "real_order_created": False,
                    "real_charge_performed": False,
                    "concurrent_duplicate": "exactly_once",
                    "forged_signature": "rejected",
                    "amount_mismatch": "rejected",
                    "tenant_mismatch": "rejected",
                    "order_mismatch": "rejected",
                    "expired_order": "provider_closed",
                    "refund": "recorded_with_idempotent_credit_clawback",
                },
                sort_keys=True,
            )
        )
    finally:
        async with async_session() as db:
            await db.execute(
                delete(BillingWebhookEvent).where(
                    BillingWebhookEvent.provider == "wechat",
                    BillingWebhookEvent.event_id.in_(event_ids.values()),
                )
            )
            await db.execute(delete(CreditTransaction).where(CreditTransaction.tenant_id == tenant_id))
            await db.execute(delete(CreditBalance).where(CreditBalance.tenant_id == tenant_id))
            await db.execute(delete(PaymentOrder).where(PaymentOrder.tenant_id == tenant_id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
