"""Platform-wide service for URL resolution and host type detection."""

import os
import re
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_settings import SystemSetting


class PlatformService:
    """Service to handle platform-wide settings and URL resolution."""

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
        db: AsyncSession,
        tenant,
        request: Request | None = None,
        *,
        sso_redirect_enabled: bool = True,
    ) -> str:
        """Generate the SSO base URL for a tenant based on IP/Domain logic.

        ``sso_redirect_enabled`` should be pre-resolved by the caller via
        ``system_setting_dao.is_sso_custom_domain_redirect_enabled()`` so this
        method never issues an extra DB round-trip for the setting.
        """
        if sso_redirect_enabled and tenant.sso_domain:
            return tenant.sso_domain.rstrip("/")

        if not sso_redirect_enabled:
            return await self.get_public_base_url(db, request)

        base_url = await self.get_public_base_url(db, request)

        # Parse protocol and host
        # Example: http://1.2.3.4:8000 or http://astra.ai
        parts = base_url.split("://")
        if len(parts) < 2:
            return base_url

        protocol = parts[0]
        host_port = parts[1]

        # Split host and port
        host_parts = host_port.split(":")
        host = host_parts[0]
        port = f":{host_parts[1]}" if len(host_parts) > 1 else ""

        if self.is_ip_address(host):
            # IP: No subdomain, just base URL
            return base_url
        else:
            # Domain: {tenant_slug}.{domain}
            # Special case for localhost: keep it as is or handle it
            if host == "localhost":
                return f"{protocol}://{host}{port}"

            # Generic logic: if host has a subdomain (e.g. try.astra.ai),
            # we strip the first component to form a base for tenant subdomains.
            h_parts = host.split(".")
            if len(h_parts) > 2:
                target_host = ".".join(h_parts[1:])
            else:
                target_host = host

            return f"{protocol}://{tenant.slug}.{target_host}{port}"


# Global instance
platform_service = PlatformService()
