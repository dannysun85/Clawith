#!/usr/bin/env python3
"""Exercise Plaza counters, toggle serialization, and cascade deletion on PostgreSQL."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

from sqlalchemy import delete, func, select

from app.api.plaza import CommentCreate, create_comment, delete_post, like_post
from app.database import async_session, engine
from app.models.plaza import PlazaComment, PlazaLike, PlazaPost
from app.models.tenant import Tenant


async def main() -> None:
    tenant_id = uuid.uuid4()
    author_id = uuid.uuid4()
    post_id = uuid.uuid4()
    user = SimpleNamespace(
        id=author_id,
        tenant_id=tenant_id,
        role="member",
        display_name="Plaza PostgreSQL Smoke",
    )

    async with async_session() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Plaza PostgreSQL Smoke",
                slug=f"plaza-smoke-{tenant_id.hex[:12]}",
                im_provider="web_only",
                is_active=True,
            )
        )
        await db.flush()
        db.add(
            PlazaPost(
                id=post_id,
                author_id=author_id,
                author_type="human",
                author_name=user.display_name,
                tenant_id=tenant_id,
                content="Concurrent Plaza integrity smoke",
            )
        )
        await db.commit()

    try:
        # Both requests start together. Row serialization makes the second
        # observe the first toggle, rather than inserting a duplicate like.
        toggles = await asyncio.gather(
            like_post(post_id, author_id, "human", user),
            like_post(post_id, author_id, "human", user),
        )
        assert sorted(item["liked"] for item in toggles) == [False, True]

        # SQL counter increments must not lose one of two concurrent comments.
        await asyncio.gather(
            create_comment(
                post_id,
                CommentCreate(
                    content="first concurrent comment",
                    author_id=author_id,
                    author_name="ignored client value",
                ),
                user,
            ),
            create_comment(
                post_id,
                CommentCreate(
                    content="second concurrent comment",
                    author_id=author_id,
                    author_name="ignored client value",
                ),
                user,
            ),
        )

        async with async_session() as db:
            post = await db.get(PlazaPost, post_id)
            assert post is not None
            assert post.likes_count == 0
            assert post.comments_count == 2
            assert (
                await db.execute(
                    select(func.count(PlazaLike.id)).where(
                        PlazaLike.post_id == post_id
                    )
                )
            ).scalar_one() == 0
            assert (
                await db.execute(
                    select(func.count(PlazaComment.id)).where(
                        PlazaComment.post_id == post_id
                    )
                )
            ).scalar_one() == 2

        assert (await like_post(post_id, author_id, "human", user))["liked"] is True
        assert await delete_post(post_id, user) == {"deleted": True}

        async with async_session() as db:
            assert await db.get(PlazaPost, post_id) is None
            assert (
                await db.execute(
                    select(func.count(PlazaLike.id)).where(
                        PlazaLike.post_id == post_id
                    )
                )
            ).scalar_one() == 0
            assert (
                await db.execute(
                    select(func.count(PlazaComment.id)).where(
                        PlazaComment.post_id == post_id
                    )
                )
            ).scalar_one() == 0
        print("plaza_postgres_smoke=ok")
    finally:
        async with async_session() as db:
            await db.execute(delete(PlazaLike).where(PlazaLike.post_id == post_id))
            await db.execute(
                delete(PlazaComment).where(PlazaComment.post_id == post_id)
            )
            await db.execute(delete(PlazaPost).where(PlazaPost.id == post_id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
