"""Platform-wide service for URL resolution and host type detection."""

import os
import re
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_settings import SystemSetting
from app.services.system_setting_security import strict_system_setting_enabled


class PlatformService:
    """Service to handle platform-wide settings and URL resolution."""

    _EMAIL_DOMAIN_RE = re.compile(
        r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
    )

    @classmethod
    def sso_origin_candidates_for_email_domain(cls, value: str) -> tuple[str, ...]:
        """Return exact legacy/origin values eligible for email-domain matching.

        SSO origins and email domains are different concepts.  Compatibility
        matching is therefore deliberately narrow: an email domain may match
        only the exact origin hostname, never a substring, tenant name, path,
        credential, sibling subdomain, or lookalike hostname.
        """

        domain = str(value or "").strip().lower().rstrip(".")
        if not cls._EMAIL_DOMAIN_RE.fullmatch(domain):
            return ()
        return (domain, f"https://{domain}", f"http://{domain}")

    @staticmethod
    def normalize_public_base_url(value: str, *, source: str = "PUBLIC_BASE_URL") -> str:
        """Validate and normalize a user-facing HTTP(S) base URL."""
        normalized = (value or "").strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{source} must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError(f"{source} must not include URL credentials")
        return normalized

    @classmethod
    def normalize_tenant_sso_domain(cls, value: str) -> str:
        """Validate a tenant redirect as an origin, never an executable URL."""

        normalized = cls.normalize_public_base_url(
            value,
            source="sso_domain",
        )
        parsed = urlsplit(normalized)
        if parsed.query or parsed.fragment:
            raise ValueError("sso_domain must not include a query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("sso_domain must be an origin without a path")
        return f"{parsed.scheme}://{parsed.netloc}"

    def is_ip_address(self, host: str) -> bool:
        """Check if a host is an IP address (IPv4)."""
        # Strip protocol and port if present
        h = host.split("://")[-1].split(":")[0].split("/")[0]
        # Basic IPv4 regex
        ip_pattern = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
        return bool(ip_pattern.match(h))

    async def get_public_base_url(self, db: AsyncSession | None = None, request: Request | None = None) -> str:
        """Resolve the platform's public base URL with priority lookup.

        Priority:
        1. Environment variable (PUBLIC_BASE_URL) - from .env or docker
        2. Persisted platform setting (platform.public_base_url)
        3. Incoming request's base URL (browser address)

        Background jobs have no trustworthy request host. They must use an
        explicitly configured URL instead of silently emitting links for an
        unrelated deployment.
        """
        env_url = os.environ.get("PUBLIC_BASE_URL")
        if env_url and env_url.strip():
            return self.normalize_public_base_url(env_url, source="PUBLIC_BASE_URL")

        if db is not None:
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == "platform"))
            setting = result.scalar_one_or_none()
            stored_url = (setting.value or {}).get("public_base_url") if setting else None
            if stored_url and str(stored_url).strip():
                return self.normalize_public_base_url(
                    str(stored_url),
                    source="platform.public_base_url",
                )

        if request:
            return self.normalize_public_base_url(str(request.base_url), source="request.base_url")

        raise RuntimeError(
            "Public base URL is not configured. Set PUBLIC_BASE_URL or "
            "platform.public_base_url before generating public links."
        )

    async def get_tenant_sso_base_url(
        self,
        db: AsyncSession | None,
        tenant,
        request: Request | None = None,
        *,
        sso_redirect_enabled: bool | None = None,
    ) -> str:
        """Return an explicitly configured tenant SSO origin, or the public origin.

        Callers may pass a previously resolved global flag.  Omitted state is
        resolved from the same transaction and fails closed when the setting is
        absent or malformed, so secondary OAuth/SSO entry points cannot bypass
        the platform switch.
        """
        if sso_redirect_enabled is None:
            sso_redirect_enabled = await self.is_sso_custom_domain_redirect_enabled(db)

        if (
            sso_redirect_enabled
            and bool(getattr(tenant, "sso_enabled", False))
            and str(getattr(tenant, "sso_domain", "") or "").strip()
        ):
            try:
                return self.normalize_tenant_sso_domain(str(tenant.sso_domain))
            except ValueError:
                # Legacy rows may predate URL validation.  They must never be
                # projected into a browser redirect.
                pass

        # Never invent a tenant subdomain. Redirects are valid only when both
        # the platform flag and the tenant's explicitly configured SSO domain
        # are enabled. DNS, TLS and reverse-proxy provisioning are external
        # facts that cannot be inferred from a tenant slug.
        return await self.get_public_base_url(db, request)

    async def is_sso_custom_domain_redirect_enabled(
        self,
        db: AsyncSession | None,
    ) -> bool:
        """Return the one fail-closed platform SSO redirect decision."""

        if db is None:
            return False
        result = await db.execute(
            select(SystemSetting).where(
                SystemSetting.key == "sso_custom_domain_redirect_enabled"
            )
        )
        setting = result.scalar_one_or_none()
        return strict_system_setting_enabled(
            getattr(setting, "value", None),
            default=False,
        )


# Global instance
platform_service = PlatformService()
