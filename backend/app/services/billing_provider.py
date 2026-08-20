"""Payment provider abstraction for subscription and credit checkout."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.models.subscription import CreditPack, PaymentOrder, Plan


def resolved_payment_base_url(settings: object | None = None) -> str:
    """Public origin for checkout, success URLs, and payment webhooks.

    PAYMENT_BASE_URL wins so extra product hosts can serve the app while
    WeChat Pay callbacks stay on the merchant-registered domain.
    """
    cfg = settings or get_settings()
    return str(
        getattr(cfg, "PAYMENT_BASE_URL", "") or getattr(cfg, "PUBLIC_BASE_URL", "") or ""
    ).rstrip("/")


@dataclass(slots=True)
class CheckoutSessionResult:
    provider: str
    session_id: str | None
    session_url: str | None
    payment_id: str | None = None


@dataclass(frozen=True, slots=True)
class BillingProviderReadiness:
    """Secret-free provider readiness returned to API/UI and deployment checks."""

    provider: str
    status: str
    checkout_enabled: bool
    native_payment_enabled: bool
    webhook_ready: bool
    missing_config: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    next_action: str = ""


def _configured_pem(settings: object, inline_key: str, path_key: str) -> str:
    inline = str(getattr(settings, inline_key, "") or "").replace("\\n", "\n").strip()
    if inline:
        return inline
    path_value = str(getattr(settings, path_key, "") or "").strip()
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _public_https_url(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and bool(hostname) and hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def billing_provider_readiness(settings: object | None = None) -> BillingProviderReadiness:
    """Describe whether checkout is safe to expose without returning secrets."""
    cfg = settings or get_settings()
    provider = str(getattr(cfg, "BILLING_PROVIDER", "manual") or "manual").lower()
    if provider == "manual":
        return BillingProviderReadiness(
            provider="manual",
            status="manual",
            checkout_enabled=True,
            native_payment_enabled=False,
            webhook_ready=False,
            next_action="提交后由平台管理员线下处理；不会生成微信支付二维码。",
        )

    if provider == "stripe":
        required = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")
        missing = tuple(key for key in required if not str(getattr(cfg, key, "") or "").strip())
        payment_base = resolved_payment_base_url(cfg)
        issues = () if _public_https_url(payment_base) else ("payment_base_url_must_be_public_https",)
        ready = not missing and not issues
        return BillingProviderReadiness(
            provider="stripe",
            status="ready" if ready else "misconfigured",
            checkout_enabled=ready,
            native_payment_enabled=ready,
            webhook_ready=ready,
            missing_config=missing,
            issues=issues,
            next_action="" if ready else "请由平台管理员补齐 Stripe 凭据和公网 HTTPS 支付域名。",
        )

    if provider != "wechat":
        return BillingProviderReadiness(
            provider=provider,
            status="unsupported",
            checkout_enabled=False,
            native_payment_enabled=False,
            webhook_ready=False,
            issues=("unsupported_billing_provider",),
            next_action="请由平台管理员选择 manual、stripe 或 wechat 支付模式。",
        )

    scalar_required = (
        "WECHAT_PAY_APPID",
        "WECHAT_PAY_MCHID",
        "WECHAT_PAY_SERIAL_NO",
        "WECHAT_PAY_API_V3_KEY",
        "WECHAT_PAY_PLATFORM_SERIAL_NO",
    )
    missing = [key for key in scalar_required if not str(getattr(cfg, key, "") or "").strip()]
    merchant_private_pem = _configured_pem(cfg, "WECHAT_PAY_PRIVATE_KEY", "WECHAT_PAY_PRIVATE_KEY_PATH")
    if not merchant_private_pem:
        missing.append("WECHAT_PAY_PRIVATE_KEY|WECHAT_PAY_PRIVATE_KEY_PATH")
    platform_public_pem = _configured_pem(
        cfg,
        "WECHAT_PAY_PLATFORM_PUBLIC_KEY",
        "WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH",
    )
    if not platform_public_pem:
        missing.append("WECHAT_PAY_PLATFORM_PUBLIC_KEY|WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH")

    issues: list[str] = []
    if merchant_private_pem:
        try:
            merchant_key = serialization.load_pem_private_key(
                merchant_private_pem.encode("utf-8"),
                password=None,
            )
            if not isinstance(merchant_key, RSAPrivateKey):
                raise TypeError
        except (TypeError, ValueError):
            issues.append("wechat_merchant_private_key_invalid")
    if platform_public_pem:
        try:
            if "BEGIN CERTIFICATE" in platform_public_pem:
                platform_key = x509.load_pem_x509_certificate(
                    platform_public_pem.encode("utf-8")
                ).public_key()
            else:
                platform_key = serialization.load_pem_public_key(platform_public_pem.encode("utf-8"))
            if not isinstance(platform_key, RSAPublicKey):
                raise TypeError
        except (TypeError, ValueError):
            issues.append("wechat_platform_public_key_invalid")
    api_v3_key = str(getattr(cfg, "WECHAT_PAY_API_V3_KEY", "") or "")
    if api_v3_key and len(api_v3_key.encode("utf-8")) != 32:
        issues.append("wechat_api_v3_key_must_be_32_bytes")
    notify_url = str(getattr(cfg, "WECHAT_PAY_NOTIFY_URL", "") or "").strip()
    if not notify_url:
        payment_base = resolved_payment_base_url(cfg)
        notify_url = f"{payment_base}/api/subscription/billing/webhook/wechat" if payment_base else ""
    if not _public_https_url(notify_url):
        issues.append("wechat_notify_url_must_be_public_https")
    ready = not missing and not issues
    return BillingProviderReadiness(
        provider="wechat",
        status="ready" if ready else "misconfigured",
        checkout_enabled=ready,
        native_payment_enabled=ready,
        webhook_ready=ready,
        missing_config=tuple(missing),
        issues=tuple(issues),
        next_action="" if ready else "请由平台管理员完成微信支付商户凭据、验签公钥和公网 HTTPS 回调配置。",
    )


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

    async def verify_webhook(
        self,
        payload: bytes,
        signature: str | None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict:
        raise ValueError("Manual billing provider does not accept webhooks")

    async def load_remote_event_state(self, event: dict):
        raise ValueError("Manual billing provider has no remote event state")

    async def query_order_state(self, order: PaymentOrder):
        """Server-to-server order status query; None means unsupported."""
        return None

    def validate_event_state(self, order: PaymentOrder, state: object) -> None:
        """Provider-specific order binding checks before any business effects."""

    async def close_order(self, order: PaymentOrder) -> bool:
        """Close an expired pending provider order; False means unsupported."""
        return False


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

    async def verify_webhook(
        self,
        payload: bytes,
        signature: str | None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict:
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
        base = resolved_payment_base_url(self.settings)
        if not base:
            raise ValueError("PAYMENT_BASE_URL, PUBLIC_BASE_URL or STRIPE_SUCCESS_URL is required for Stripe checkout")
        return f"{base}/billing/success?order_id={order.id}"

    def _cancel_url(self, order: PaymentOrder) -> str:
        configured = self.settings.STRIPE_CANCEL_URL or self.settings.BILLING_CANCEL_URL
        if configured:
            return configured.replace("{order_id}", str(order.id))
        base = resolved_payment_base_url(self.settings)
        if not base:
            raise ValueError("PAYMENT_BASE_URL, PUBLIC_BASE_URL or STRIPE_CANCEL_URL is required for Stripe checkout")
        return f"{base}/account/subscription?order_id={order.id}&status=canceled"


class WeChatBillingProvider(BillingProvider):
    """WeChat Pay V3 Native (扫码支付) provider.

    - Checkout creates a Native order and returns the ``code_url`` for the
      frontend to render as a QR code (``session_url`` field).
    - WeChat Pay settles in CNY only, so non-CNY orders convert at
      ``BILLING_USD_CNY_RATE``.
    - Callback authenticity is verified from the Wechatpay-* HTTP signature
      headers before the AEAD_AES_256_GCM resource is decrypted.
      ``load_remote_event_state`` additionally re-queries the order state
      server-to-server, per SUBSCRIPTION_IMPLEMENTATION_DESIGN.md §4.4.
    - ``out_trade_no`` uses the order UUID's 32-char hex form: WeChat Pay caps
      ``out_trade_no`` at 32 characters, so the hyphenated 36-char UUID string
      is rejected with PARAM_ERROR.
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
                "WECHAT_PAY_PLATFORM_SERIAL_NO",
            )
            if not getattr(self.settings, key)
        ]
        if missing:
            raise ValueError(f"{', '.join(missing)} required when BILLING_PROVIDER=wechat")
        if len(self.settings.WECHAT_PAY_API_V3_KEY.encode("utf-8")) != 32:
            raise ValueError("WECHAT_PAY_API_V3_KEY must be exactly 32 bytes")
        self._private_key = self._load_private_key()
        self._platform_public_key = self._load_platform_public_key()

    async def create_subscription_checkout(
        self,
        *,
        order: PaymentOrder,
        plan: Plan,
        period: str,
        seats: int,
    ) -> CheckoutSessionResult:
        code_url = await self._create_native_order(order, f"{plan.name} 套餐订阅")
        return CheckoutSessionResult(provider=self.name, session_id=order.id.hex, session_url=code_url)

    async def create_topup_checkout(
        self,
        *,
        order: PaymentOrder,
        pack: CreditPack,
    ) -> CheckoutSessionResult:
        code_url = await self._create_native_order(order, f"{pack.name} 额度充值")
        return CheckoutSessionResult(provider=self.name, session_id=order.id.hex, session_url=code_url)

    async def verify_webhook(
        self,
        payload: bytes,
        signature: str | None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict:
        self._verify_http_signature(payload, signature=signature, headers=headers)
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

    def _verify_http_signature(
        self,
        payload: bytes,
        *,
        signature: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Verify WeChat callback/API response headers over the exact raw body."""
        normalized_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        callback_signature = normalized_headers.get("wechatpay-signature") or signature or ""
        timestamp = normalized_headers.get("wechatpay-timestamp", "")
        nonce = normalized_headers.get("wechatpay-nonce", "")
        serial = normalized_headers.get("wechatpay-serial", "")
        if not callback_signature or not timestamp or not nonce or not serial:
            raise ValueError("Missing WeChat callback signature headers")
        if callback_signature.startswith("WECHATPAY/SIGNTEST/"):
            raise ValueError("WeChat callback signature test value rejected")
        if not hmac.compare_digest(serial, self.settings.WECHAT_PAY_PLATFORM_SERIAL_NO):
            raise ValueError("Unknown WeChat callback signing serial")
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise ValueError("Invalid WeChat callback timestamp") from exc
        max_skew = max(int(self.settings.WECHAT_PAY_WEBHOOK_MAX_SKEW_SECONDS), 0)
        if abs(int(time.time()) - timestamp_value) > max_skew:
            raise ValueError("Stale WeChat callback timestamp")
        message = timestamp.encode("utf-8") + b"\n" + nonce.encode("utf-8") + b"\n" + payload + b"\n"
        try:
            decoded_signature = base64.b64decode(callback_signature, validate=True)
            self._platform_public_key.verify(
                decoded_signature,
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("Invalid WeChat callback signature") from exc

    @staticmethod
    def _tenant_id_from_attach(value: object) -> uuid.UUID | None:
        raw = str(value or "")
        if not raw.startswith("tenant:"):
            return None
        try:
            return uuid.UUID(raw.removeprefix("tenant:"))
        except ValueError:
            return None

    def _state_from_payload(self, *, event_id: str, event_type: str, payload: dict):
        from app.services.billing_events import PaymentProviderEventState

        out_trade_no = str(payload.get("out_trade_no") or "")
        try:
            order_id = uuid.UUID(out_trade_no)
        except ValueError as exc:
            raise ValueError("Invalid WeChat out_trade_no") from exc
        amount = payload.get("amount") if isinstance(payload.get("amount"), dict) else {}
        return PaymentProviderEventState(
            provider=self.name,
            event_id=event_id,
            event_type=event_type,
            order_id=order_id,
            status=self._order_status_from_trade_state(str(payload.get("trade_state") or "")),
            provider_session_id=order_id.hex,
            provider_payment_id=payload.get("transaction_id"),
            amount_cents=amount.get("total"),
            currency=amount.get("currency"),
            merchant_id=payload.get("mchid"),
            app_id=payload.get("appid"),
            trade_type=payload.get("trade_type"),
            tenant_id=self._tenant_id_from_attach(payload.get("attach")),
        )

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
                state = {**remote, "out_trade_no": remote.get("out_trade_no") or out_trade_no}
        except ValueError:
            pass  # fall back to the AEAD-authenticated webhook body

        return self._state_from_payload(
            event_id=str(event.get("id")),
            event_type=str(event.get("type") or ""),
            payload=state,
        )

    async def query_order_state(self, order: PaymentOrder):
        """Query WeChat for the order's current trade state (missed-webhook recovery)."""
        try:
            remote = await self._request(
                "GET",
                f"/v3/pay/transactions/out-trade-no/{order.id.hex}?mchid={self.settings.WECHAT_PAY_MCHID}",
            )
        except ValueError:
            # ORDERNOTEXIST / network failure: leave the order pending.
            return None
        if not remote:
            return None
        return self._state_from_payload(
            event_id=f"query:{order.id}",
            event_type="QUERY",
            payload={**remote, "out_trade_no": remote.get("out_trade_no") or order.id.hex},
        )

    @staticmethod
    def _order_status_from_trade_state(trade_state: str) -> str:
        return {
            "SUCCESS": "paid",
            "NOTPAY": "pending",
            "USERPAYING": "pending",
            "CLOSED": "canceled",
            "REVOKED": "canceled",
            "PAYERROR": "failed",
            "REFUND": "refunded",
        }.get(trade_state, "pending")

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

    def _load_platform_public_key(self) -> RSAPublicKey:
        pem = _configured_pem(
            self.settings,
            "WECHAT_PAY_PLATFORM_PUBLIC_KEY",
            "WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH",
        )
        if not pem:
            raise ValueError(
                "WECHAT_PAY_PLATFORM_PUBLIC_KEY or WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH required "
                "when BILLING_PROVIDER=wechat"
            )
        try:
            if "BEGIN CERTIFICATE" in pem:
                key = x509.load_pem_x509_certificate(pem.encode("utf-8")).public_key()
            else:
                key = serialization.load_pem_public_key(pem.encode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid WeChat platform public key: {exc}") from exc
        if not isinstance(key, RSAPublicKey):
            raise ValueError("WeChat platform public key must be an RSA key")
        return key

    def validate_event_state(self, order: PaymentOrder, state: object) -> None:
        """Fail closed before a WeChat success/refund mutates entitlements."""
        if getattr(state, "status", None) not in {"paid", "refunded"}:
            return
        if getattr(state, "order_id", None) != order.id:
            raise ValueError("WeChat payment order mismatch")
        if getattr(state, "provider_session_id", None) != order.id.hex:
            raise ValueError("WeChat payment provider session mismatch")
        if getattr(state, "merchant_id", None) != self.settings.WECHAT_PAY_MCHID:
            raise ValueError("WeChat payment merchant mismatch")
        if getattr(state, "app_id", None) != self.settings.WECHAT_PAY_APPID:
            raise ValueError("WeChat payment appid mismatch")
        if getattr(state, "trade_type", None) != "NATIVE":
            raise ValueError("WeChat payment trade type mismatch")
        if getattr(state, "currency", None) != "CNY":
            raise ValueError("WeChat payment currency mismatch")
        if getattr(state, "amount_cents", None) != self._cny_total(order):
            raise ValueError("WeChat payment amount mismatch")
        if getattr(state, "tenant_id", None) != order.tenant_id:
            raise ValueError("WeChat payment tenant mismatch")

    async def close_order(self, order: PaymentOrder) -> bool:
        await self._request(
            "POST",
            f"/v3/pay/transactions/out-trade-no/{order.id.hex}/close",
            {"mchid": self.settings.WECHAT_PAY_MCHID},
        )
        return True

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
        self._verify_http_signature(response.content, headers=response.headers)
        if response.status_code >= 400:
            raise ValueError("WeChat Pay API returned a verified error response")
        return response.json() if response.content else {}

    def _notify_url(self) -> str:
        configured = self.settings.WECHAT_PAY_NOTIFY_URL
        if configured:
            return configured
        base = resolved_payment_base_url(self.settings)
        if not base:
            raise ValueError(
                "WECHAT_PAY_NOTIFY_URL, PAYMENT_BASE_URL, or PUBLIC_BASE_URL is required for WeChat Pay"
            )
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
            "out_trade_no": order.id.hex,
            "notify_url": self._notify_url(),
            "amount": {"total": self._cny_total(order), "currency": "CNY"},
            "attach": f"tenant:{order.tenant_id.hex}",
        }
        expire_minutes = int(self.settings.WECHAT_PAY_ORDER_EXPIRE_MINUTES)
        if expire_minutes > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
            payload["time_expire"] = expires_at.isoformat()
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
