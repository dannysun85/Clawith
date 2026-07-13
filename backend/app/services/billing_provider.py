"""Payment provider abstraction for subscription and credit checkout."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import get_settings
from app.models.subscription import CreditPack, PaymentOrder, Plan


@dataclass(slots=True)
class CheckoutSessionResult:
    provider: str
    session_id: str | None
    session_url: str | None
    payment_id: str | None = None


class BillingProvider:
    name = "manual"

    async def create_subscription_checkout(
        self,
        *,
        order: PaymentOrder,
        plan: Plan,
        period: str,
        seats: int,
    ) -> CheckoutSessionResult:
        return CheckoutSessionResult(provider=self.name, session_id=None, session_url=None)

    async def create_topup_checkout(
        self,
        *,
        order: PaymentOrder,
        pack: CreditPack,
    ) -> CheckoutSessionResult:
        return CheckoutSessionResult(provider=self.name, session_id=None, session_url=None)

    async def verify_webhook(self, payload: bytes, signature: str | None) -> dict:
        raise ValueError("Manual billing provider does not accept webhooks")

    async def load_remote_event_state(self, event: dict):
        raise ValueError("Manual billing provider has no remote event state")


class StripeBillingProvider(BillingProvider):
    name = "stripe"

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.STRIPE_SECRET_KEY:
            raise ValueError("STRIPE_SECRET_KEY is required when BILLING_PROVIDER=stripe")

    async def create_subscription_checkout(
        self,
        *,
        order: PaymentOrder,
        plan: Plan,
        period: str,
        seats: int,
    ) -> CheckoutSessionResult:
        if not plan.stripe_price_id:
            raise ValueError(f"Plan {plan.code} is missing stripe_price_id")
        payload = {
            "mode": "subscription",
            "success_url": self._success_url(order),
            "cancel_url": self._cancel_url(order),
            "line_items[0][price]": plan.stripe_price_id,
            "line_items[0][quantity]": str(max(seats, 1)),
            "metadata[order_id]": str(order.id),
            "metadata[tenant_id]": str(order.tenant_id),
            "metadata[type]": "subscribe",
            "metadata[period]": period,
        }
        session = await self._post_checkout_session(payload)
        return CheckoutSessionResult(
            provider=self.name,
            session_id=session.get("id"),
            session_url=session.get("url"),
            payment_id=session.get("subscription"),
        )

    async def create_topup_checkout(
        self,
        *,
        order: PaymentOrder,
        pack: CreditPack,
    ) -> CheckoutSessionResult:
        payload = {
            "mode": "payment",
            "success_url": self._success_url(order),
            "cancel_url": self._cancel_url(order),
            "line_items[0][price_data][currency]": pack.currency.lower(),
            "line_items[0][price_data][unit_amount]": str(pack.price_cents),
            "line_items[0][price_data][product_data][name]": pack.name,
            "line_items[0][quantity]": "1",
            "metadata[order_id]": str(order.id),
            "metadata[tenant_id]": str(order.tenant_id),
            "metadata[type]": "topup",
            "metadata[credit_pack_id]": str(pack.id),
        }
        session = await self._post_checkout_session(payload)
        return CheckoutSessionResult(
            provider=self.name,
            session_id=session.get("id"),
            session_url=session.get("url"),
            payment_id=session.get("payment_intent"),
        )

    async def verify_webhook(self, payload: bytes, signature: str | None) -> dict:
        secret = self.settings.STRIPE_WEBHOOK_SECRET
        if not secret:
            raise ValueError("STRIPE_WEBHOOK_SECRET is required for Stripe webhooks")
        if not signature:
            raise ValueError("Missing Stripe-Signature header")

        timestamp = None
        signatures: list[str] = []
        for part in signature.split(","):
            key, _, value = part.partition("=")
            if key == "t":
                timestamp = value
            elif key == "v1":
                signatures.append(value)
        if not timestamp or not signatures:
            raise ValueError("Invalid Stripe-Signature header")

        signed_payload = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, item) for item in signatures):
            raise ValueError("Invalid Stripe webhook signature")
        return json.loads(payload.decode("utf-8"))

    async def load_remote_event_state(self, event: dict):
        from app.services.billing_events import PaymentProviderEventState

        event_type = str(event.get("type") or "")
        obj = event.get("data", {}).get("object", {}) if isinstance(event.get("data"), dict) else {}
        metadata = obj.get("metadata") or {}
        order_id_raw = metadata.get("order_id")
        if not order_id_raw:
            return PaymentProviderEventState(
                provider=self.name,
                event_id=str(event.get("id")),
                event_type=event_type,
                order_id=None,
                status="ignored",
            )

        status = "pending"
        if event_type == "checkout.session.completed":
            status = "paid" if obj.get("payment_status") in (None, "paid") or obj.get("mode") == "subscription" else "pending"
        elif event_type in {"checkout.session.expired", "checkout.session.async_payment_failed"}:
            status = "failed"

        provider_payment_id = obj.get("payment_intent") or obj.get("subscription")
        return PaymentProviderEventState(
            provider=self.name,
            event_id=str(event.get("id")),
            event_type=event_type,
            order_id=uuid.UUID(order_id_raw),
            status=status,
            provider_session_id=obj.get("id"),
            provider_payment_id=provider_payment_id,
        )

    async def _post_checkout_session(self, payload: dict[str, str]) -> dict:
        url = f"{self.settings.STRIPE_API_BASE_URL.rstrip('/')}/v1/checkout/sessions"
        headers = {
            "Authorization": f"Bearer {self.settings.STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers=headers, content=urlencode(payload))
        if response.status_code >= 400:
            raise ValueError(f"Stripe checkout session failed: {response.text[:400]}")
        return response.json()

    def _success_url(self, order: PaymentOrder) -> str:
        configured = self.settings.STRIPE_SUCCESS_URL or self.settings.BILLING_SUCCESS_URL
        if configured:
            return configured.replace("{order_id}", str(order.id))
        base = (self.settings.PUBLIC_BASE_URL or "").rstrip("/")
        if not base:
            raise ValueError("PUBLIC_BASE_URL or STRIPE_SUCCESS_URL is required for Stripe checkout")
        return f"{base}/billing/success?order_id={order.id}"

    def _cancel_url(self, order: PaymentOrder) -> str:
        configured = self.settings.STRIPE_CANCEL_URL or self.settings.BILLING_CANCEL_URL
        if configured:
            return configured.replace("{order_id}", str(order.id))
        base = (self.settings.PUBLIC_BASE_URL or "").rstrip("/")
        if not base:
            raise ValueError("PUBLIC_BASE_URL or STRIPE_CANCEL_URL is required for Stripe checkout")
        return f"{base}/account/subscription?order_id={order.id}&status=canceled"


def get_billing_provider(provider_name: str | None = None) -> BillingProvider:
    settings = get_settings()
    name = (provider_name or settings.BILLING_PROVIDER or "manual").lower()
    if name == "stripe":
        return StripeBillingProvider()
    if name == "manual":
        return BillingProvider()
    raise ValueError(f"Unsupported billing provider: {name}")
