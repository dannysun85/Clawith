"""Payment provider abstraction for subscription and credit checkout."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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


class WeChatBillingProvider(BillingProvider):
    """WeChat Pay V3 Native (扫码支付) provider.

    - Checkout creates a Native order and returns the ``code_url`` for the
      frontend to render as a QR code (``session_url`` field).
    - WeChat Pay settles in CNY only, so non-CNY orders convert at
      ``BILLING_USD_CNY_RATE``.
    - Webhook authenticity comes from AEAD_AES_256_GCM decryption with the
      merchant APIv3 key: only WeChat can produce a ciphertext that decrypts
      under it. ``load_remote_event_state`` additionally re-queries the order
      state server-to-server, per SUBSCRIPTION_IMPLEMENTATION_DESIGN.md §4.4.
    """

    name = "wechat"

    def __init__(self) -> None:
        self.settings = get_settings()
        missing = [
            key
            for key in (
                "WECHAT_PAY_APPID",
                "WECHAT_PAY_MCHID",
                "WECHAT_PAY_SERIAL_NO",
                "WECHAT_PAY_API_V3_KEY",
            )
            if not getattr(self.settings, key)
        ]
        if missing:
            raise ValueError(f"{', '.join(missing)} required when BILLING_PROVIDER=wechat")
        self._private_key = self._load_private_key()

    async def create_subscription_checkout(
        self,
        *,
        order: PaymentOrder,
        plan: Plan,
        period: str,
        seats: int,
    ) -> CheckoutSessionResult:
        code_url = await self._create_native_order(order, f"{plan.name} 套餐订阅")
        return CheckoutSessionResult(provider=self.name, session_id=str(order.id), session_url=code_url)

    async def create_topup_checkout(
        self,
        *,
        order: PaymentOrder,
        pack: CreditPack,
    ) -> CheckoutSessionResult:
        code_url = await self._create_native_order(order, f"{pack.name} 额度充值")
        return CheckoutSessionResult(provider=self.name, session_id=str(order.id), session_url=code_url)

    async def verify_webhook(self, payload: bytes, signature: str | None) -> dict:
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid WeChat webhook payload") from exc
        resource = body.get("resource") or {}
        decrypted = self._decrypt_resource(resource)
        return {
            "id": body.get("id"),
            "type": body.get("event_type"),
            "decrypted": decrypted,
            "wechat": body,
        }

    async def load_remote_event_state(self, event: dict):
        from app.services.billing_events import PaymentProviderEventState

        decrypted = event.get("decrypted") or {}
        out_trade_no = str(decrypted.get("out_trade_no") or "")
        if not out_trade_no:
            return PaymentProviderEventState(
                provider=self.name,
                event_id=str(event.get("id")),
                event_type=str(event.get("type") or ""),
                order_id=None,
                status="ignored",
            )

        state = decrypted
        try:
            remote = await self._request(
                "GET",
                f"/v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={self.settings.WECHAT_PAY_MCHID}",
            )
            if remote:
                state = remote
        except ValueError:
            pass  # fall back to the AEAD-authenticated webhook body

        trade_state = str(state.get("trade_state") or "")
        order_status = {
            "SUCCESS": "paid",
            "NOTPAY": "pending",
            "USERPAYING": "pending",
            "CLOSED": "canceled",
            "REVOKED": "canceled",
            "PAYERROR": "failed",
        }.get(trade_state, "pending")
        return PaymentProviderEventState(
            provider=self.name,
            event_id=str(event.get("id")),
            event_type=str(event.get("type") or ""),
            order_id=uuid.UUID(out_trade_no),
            status=order_status,
            provider_session_id=out_trade_no,
            provider_payment_id=state.get("transaction_id"),
        )

    def _load_private_key(self) -> RSAPrivateKey:
        pem = self.settings.WECHAT_PAY_PRIVATE_KEY.replace("\\n", "\n").strip()
        if not pem and self.settings.WECHAT_PAY_PRIVATE_KEY_PATH:
            pem = Path(self.settings.WECHAT_PAY_PRIVATE_KEY_PATH).read_text(encoding="utf-8").strip()
        if not pem:
            raise ValueError("WECHAT_PAY_PRIVATE_KEY or WECHAT_PAY_PRIVATE_KEY_PATH required when BILLING_PROVIDER=wechat")
        try:
            key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        except Exception as exc:
            raise ValueError(f"Invalid WeChat merchant private key: {exc}") from exc
        if not isinstance(key, RSAPrivateKey):
            raise ValueError("WeChat merchant private key must be an RSA key")
        return key

    def _authorization(self, method: str, path: str, body: str) -> str:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
        signature = base64.b64encode(
            self._private_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        ).decode("ascii")
        return (
            f'WECHATPAY2-SHA256-RSA2048 mchid="{self.settings.WECHAT_PAY_MCHID}",'
            f'nonce_str="{nonce}",signature="{signature}",'
            f'timestamp="{timestamp}",serial_no="{self.settings.WECHAT_PAY_SERIAL_NO}"'
        )

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
        url = f"{self.settings.WECHAT_PAY_API_BASE_URL.rstrip('/')}{path}"
        headers = {
            "Authorization": self._authorization(method, path, body),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, url, headers=headers, content=body or None)
        if response.status_code >= 400:
            raise ValueError(f"WeChat Pay API failed: {response.text[:400]}")
        return response.json() if response.content else {}

    def _notify_url(self) -> str:
        configured = self.settings.WECHAT_PAY_NOTIFY_URL
        if configured:
            return configured
        base = (self.settings.PUBLIC_BASE_URL or "").rstrip("/")
        if not base:
            raise ValueError("WECHAT_PAY_NOTIFY_URL or PUBLIC_BASE_URL is required for WeChat Pay")
        return f"{base}/api/subscription/billing/webhook/wechat"

    def _cny_total(self, order: PaymentOrder) -> int:
        total = order.amount_cents
        if order.currency != "CNY":
            total = round(order.amount_cents * self.settings.BILLING_USD_CNY_RATE)
        if total < 1:
            raise ValueError("Order amount is below the WeChat Pay minimum")
        return total

    async def _create_native_order(self, order: PaymentOrder, description: str) -> str:
        payload = {
            "appid": self.settings.WECHAT_PAY_APPID,
            "mchid": self.settings.WECHAT_PAY_MCHID,
            "description": description[:127],
            "out_trade_no": str(order.id),
            "notify_url": self._notify_url(),
            "amount": {"total": self._cny_total(order), "currency": "CNY"},
        }
        result = await self._request("POST", "/v3/pay/transactions/native", payload)
        code_url = str(result.get("code_url") or "")
        if not code_url:
            raise ValueError("WeChat Pay did not return code_url")
        return code_url

    def _decrypt_resource(self, resource: dict) -> dict:
        algorithm = resource.get("algorithm")
        if algorithm != "AEAD_AES_256_GCM":
            raise ValueError(f"Unsupported WeChat webhook algorithm: {algorithm}")
        try:
            ciphertext = base64.b64decode(resource.get("ciphertext") or "")
            nonce = (resource.get("nonce") or "").encode("utf-8")
            associated_data = (resource.get("associated_data") or "").encode("utf-8")
            plaintext = AESGCM(self.settings.WECHAT_PAY_API_V3_KEY.encode("utf-8")).decrypt(
                nonce, ciphertext, associated_data
            )
        except Exception as exc:
            raise ValueError("WeChat webhook decryption failed") from exc
        return json.loads(plaintext.decode("utf-8"))


def get_billing_provider(provider_name: str | None = None) -> BillingProvider:
    settings = get_settings()
    name = (provider_name or settings.BILLING_PROVIDER or "manual").lower()
    if name == "stripe":
        return StripeBillingProvider()
    if name == "wechat":
        return WeChatBillingProvider()
    if name == "manual":
        return BillingProvider()
    raise ValueError(f"Unsupported billing provider: {name}")
