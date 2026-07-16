#!/usr/bin/env python3
"""Prove ChannelConfig ORM plaintext never appears in PostgreSQL storage."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import delete, text

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.tenant import Tenant
from app.models.user import User


LEGACY_CONFIG_ID = uuid.UUID("07500000-0000-4000-8000-000000000190")
LEGACY_SECRETS = {
    "app_secret": "legacy-channel-app-secret",
    "encrypt_key": "legacy-channel-signing-secret",
    "verification_token": "legacy-channel-verification-token",
    "nested": "legacy-channel-nested-token",
}


async def _assert_legacy_fixture(*, required: bool) -> None:
    async with engine.connect() as connection:
        raw = (
            await connection.execute(
                text(
                    """
                    SELECT app_secret, encrypt_key, verification_token, extra_config
                    FROM channel_configs
                    WHERE id = :id
                    """
                ),
                {"id": LEGACY_CONFIG_ID},
            )
        ).one_or_none()
    if raw is None:
        if required:
            raise AssertionError("required legacy ChannelConfig fixture is missing")
        return

    raw_storage = "|".join(str(value or "") for value in raw)
    assert raw_storage.count("enc:channel:v1:") == 4
    assert all(secret not in raw_storage for secret in LEGACY_SECRETS.values())

    async with async_session() as db:
        config = await db.get(ChannelConfig, LEGACY_CONFIG_ID)
        assert config is not None
        assert config.app_secret == LEGACY_SECRETS["app_secret"]
        assert config.encrypt_key == LEGACY_SECRETS["encrypt_key"]
        assert config.verification_token == LEGACY_SECRETS["verification_token"]
        assert config.extra_config["future"]["token"] == LEGACY_SECRETS["nested"]


async def main(*, require_legacy_fixture: bool, legacy_only: bool) -> None:
    await _assert_legacy_fixture(required=require_legacy_fixture)
    if legacy_only:
        print("channel_config_legacy_migration_postgres_smoke=ok")
        await engine.dispose()
        return

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    config_id = uuid.uuid4()
    secrets = {
        "app_secret": "pg-channel-app-secret",
        "encrypt_key": "pg-channel-signing-secret",
        "verification_token": "pg-channel-verification-token",
        "nested": "pg-channel-nested-token",
    }

    async with async_session() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Channel Encryption PostgreSQL Smoke",
                slug=f"channel-encryption-{tenant_id.hex[:12]}",
                im_provider="web_only",
                is_active=True,
            )
        )
        await db.flush()
        db.add(
            User(
                id=user_id,
                display_name="Channel Encryption Smoke",
                role="member",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                name="Channel Encryption Smoke Agent",
                creator_id=user_id,
                tenant_id=tenant_id,
                status="idle",
            )
        )
        await db.flush()
        db.add(
            ChannelConfig(
                id=config_id,
                agent_id=agent_id,
                channel_type="slack",
                app_id="public-client-id",
                app_secret=secrets["app_secret"],
                encrypt_key=secrets["encrypt_key"],
                verification_token=secrets["verification_token"],
                extra_config={
                    "connection_mode": "webhook",
                    "future": {"token": secrets["nested"]},
                },
                is_configured=True,
            )
        )
        await db.commit()

    try:
        async with engine.connect() as connection:
            raw = (
                await connection.execute(
                    text(
                        """
                        SELECT app_secret, encrypt_key, verification_token, extra_config
                        FROM channel_configs
                        WHERE id = :id
                        """
                    ),
                    {"id": config_id},
                )
            ).one()
        raw_storage = "|".join(str(value or "") for value in raw)
        assert raw_storage.count("enc:channel:v1:") == 4
        assert all(secret not in raw_storage for secret in secrets.values())

        async with async_session() as db:
            config = await db.get(ChannelConfig, config_id)
            assert config is not None
            assert config.app_secret == secrets["app_secret"]
            assert config.encrypt_key == secrets["encrypt_key"]
            assert config.verification_token == secrets["verification_token"]
            assert config.extra_config["future"]["token"] == secrets["nested"]
            config.extra_config = {
                **config.extra_config,
                "future": {
                    **config.extra_config["future"],
                    "rotated": "pg-channel-rotated-token",
                },
            }
            await db.commit()

        async with engine.connect() as connection:
            raw_extra = (
                await connection.execute(
                    text("SELECT extra_config FROM channel_configs WHERE id = :id"),
                    {"id": config_id},
                )
            ).scalar_one()
        assert "pg-channel-rotated-token" not in raw_extra
        assert raw_extra.startswith("enc:channel:v1:")
        print("channel_config_encryption_postgres_smoke=ok")
    finally:
        async with async_session() as db:
            await db.execute(
                delete(ChannelConfig).where(ChannelConfig.id == config_id)
            )
            await db.execute(delete(Agent).where(Agent.id == agent_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-legacy-fixture", action="store_true")
    parser.add_argument("--legacy-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        main(
            require_legacy_fixture=args.require_legacy_fixture,
            legacy_only=args.legacy_only,
        )
    )
