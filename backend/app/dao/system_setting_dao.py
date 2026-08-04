"""DAO for the system_settings key-value table."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.dao.base import BaseDAO
from app.models.system_settings import SystemSetting


class SystemSettingDAO(BaseDAO[SystemSetting]):
    """Typed access layer for platform-level system settings."""

    def __init__(self) -> None:
        super().__init__(SystemSetting)

    async def get_by_key(self, key: str) -> SystemSetting | None:
        """Fetch a single SystemSetting row by its primary key."""
        async with self.session() as db:
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
            return result.scalar_one_or_none()

    async def get_value(self, key: str, default: Any = None) -> Any:
        """Return the JSON value for a key, or *default* when the row is absent."""
        setting = await self.get_by_key(key)
        if setting is None:
            return default
        from app.services.system_setting_security import (
            decrypt_system_setting_value,
        )

        return decrypt_system_setting_value(key, setting.value)

    async def is_invitation_code_enabled(self) -> bool:
        """Return whether invitation-code enforcement is active."""
        value = await self.get_value("invitation_code_enabled", {"enabled": True})
        return bool(value.get("enabled", True))

    async def is_sso_custom_domain_redirect_enabled(self) -> bool:
        """Return whether cross-domain SSO redirect is globally enabled."""
        from app.services.system_setting_security import strict_system_setting_enabled

        value = await self.get_value(
            "sso_custom_domain_redirect_enabled",
            {"enabled": False},
        )
        return strict_system_setting_enabled(value, default=False)


system_setting_dao = SystemSettingDAO()
