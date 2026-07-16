"""Verify current ChannelConfig rows are encrypted and readable without leaking values."""

from __future__ import annotations

import asyncio

from sqlalchemy import select, text

from app.database import async_session, engine
from app.models.channel_config import ChannelConfig


CHANNEL_SECRET_STORAGE_QUERY = text(
    """
    SELECT count(*)
    FROM channel_configs
    WHERE (
        COALESCE(app_secret, '') <> ''
        AND app_secret NOT LIKE 'enc:channel:v1:%'
    ) OR (
        COALESCE(encrypt_key, '') <> ''
        AND encrypt_key NOT LIKE 'enc:channel:v1:%'
    ) OR (
        COALESCE(verification_token, '') <> ''
        AND verification_token NOT LIKE 'enc:channel:v1:%'
    ) OR extra_config IS NULL
      OR extra_config::text NOT LIKE 'enc:channel:v1:%'
    """
)


async def verify_channel_secret_envelopes() -> int:
    async with async_session() as db:
        invalid_storage_count = (
            await db.execute(CHANNEL_SECRET_STORAGE_QUERY)
        ).scalar_one()
        if invalid_storage_count:
            raise RuntimeError(
                "channel secret storage verification found unencrypted rows"
            )

        try:
            configs = list(
                (
                    await db.execute(
                        select(ChannelConfig).order_by(ChannelConfig.id)
                    )
                ).scalars().all()
            )
            if any(not isinstance(config.extra_config, dict) for config in configs):
                raise ValueError("invalid channel config payload")
        except Exception:
            raise RuntimeError(
                "channel secret envelope authentication or decoding failed"
            ) from None
        return len(configs)


async def main() -> None:
    try:
        verified_count = await verify_channel_secret_envelopes()
        print(f"channel_secret_envelopes_verified={verified_count}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
