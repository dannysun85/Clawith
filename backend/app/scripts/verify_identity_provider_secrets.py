"""Verify IdentityProvider configs are authenticated ciphertext at rest."""

from __future__ import annotations

import asyncio

from sqlalchemy import select, text

from app.database import async_session, engine
from app.models.identity import IdentityProvider


IDENTITY_PROVIDER_SECRET_STORAGE_QUERY = text(
    """
    SELECT count(*)
    FROM identity_providers
    WHERE config IS NOT NULL
      AND CAST(config AS TEXT) NOT LIKE 'enc:idp:v1:%'
    """
)


async def verify_identity_provider_secret_envelopes() -> int:
    async with async_session() as db:
        invalid_storage_count = (
            await db.execute(IDENTITY_PROVIDER_SECRET_STORAGE_QUERY)
        ).scalar_one()
        if invalid_storage_count:
            raise RuntimeError(
                "identity provider secret storage contains unencrypted rows"
            )
        try:
            providers = list(
                (
                    await db.execute(
                        select(IdentityProvider).order_by(IdentityProvider.id)
                    )
                ).scalars().all()
            )
            if any(
                provider.config is not None
                and not isinstance(provider.config, dict)
                for provider in providers
            ):
                raise ValueError("invalid identity provider config payload")
        except Exception:
            raise RuntimeError(
                "identity provider secret envelope authentication or decoding failed"
            ) from None
        return len(providers)


async def main() -> None:
    try:
        verified_count = await verify_identity_provider_secret_envelopes()
        print(f"identity_provider_secret_envelopes_verified={verified_count}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
