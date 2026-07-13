#!/usr/bin/env python3
"""Verify plan update compare-and-swap semantics against PostgreSQL."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.api.subscription import update_plan
from app.database import async_session, engine
from app.models.subscription import Plan
from app.schemas.subscription import PlanUpdateIn


async def main() -> None:
    try:
        PlanUpdateIn(sort_order=1)
    except ValidationError:
        pass
    else:
        raise AssertionError("plan PATCH accepted a missing expected_updated_at precondition")

    actor = SimpleNamespace(id=None)
    async with async_session() as db:
        plan = (await db.execute(select(Plan).order_by(Plan.code))).scalars().first()
        if plan is None:
            raise AssertionError("migration smoke database has no plan")
        plan_id = plan.id
        original_sort_order = plan.sort_order
        original_updated_at = plan.updated_at
        updated = await update_plan(
            plan_id,
            PlanUpdateIn(
                sort_order=original_sort_order + 1000,
                expected_updated_at=original_updated_at,
            ),
            current_user=actor,
            db=db,
        )
        if updated.updated_at == original_updated_at:
            raise AssertionError("PostgreSQL did not advance plans.updated_at")

    async with async_session() as db:
        try:
            await update_plan(
                plan_id,
                PlanUpdateIn(
                    sort_order=original_sort_order + 2000,
                    expected_updated_at=original_updated_at,
                ),
                current_user=actor,
                db=db,
            )
        except HTTPException as exc:
            if exc.status_code != 409:
                raise AssertionError(f"stale plan PATCH returned {exc.status_code}, expected 409") from exc
        else:
            raise AssertionError("stale plan PATCH was not rejected")

    async with async_session() as db:
        current = await db.get(Plan, plan_id)
        if current is None:
            raise AssertionError("plan disappeared during CAS smoke")
        await update_plan(
            plan_id,
            PlanUpdateIn(
                sort_order=original_sort_order,
                expected_updated_at=current.updated_at,
            ),
            current_user=actor,
            db=db,
        )

    print("plan update PostgreSQL CAS smoke passed")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
