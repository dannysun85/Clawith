#!/usr/bin/env python3
"""Destructive G11 acceptance against one disposable loopback PostgreSQL DB.

The wrapper creates and drops the database.  This process verifies lifecycle
guards, holds, schema/cross-tenant fail-closed behavior, partial storage
recovery, exact physical deletion, idempotency, and global Identity retention.
No real tenant, provider, mail system, or production service is touched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import uuid

from sqlalchemy import delete, select, text

from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.tenant_deletion import (
    TenantDeletionHold,
    TenantDeletionJob,
    TenantDeletionTombstone,
)
from app.models.user import Identity, User
from app.services.storage import LocalStorageBackend, tenant_storage_prefix
from app.services.tenant_purge import (
    TenantPurgeError,
    create_tenant_purge_hold,
    dry_run_tenant_purge,
    execute_tenant_purge,
    list_tenant_purge_states,
    release_tenant_purge_hold,
)


class SmokeFailure(RuntimeError):
    """Sanitized local acceptance failure."""


class FailOnceStorage(LocalStorageBackend):
    def __init__(self, root: str):
        super().__init__(root)
        self.failed = False

    async def delete_tree(self, key: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected local storage failure")
        await super().delete_tree(key)


@dataclass(frozen=True)
class Fixture:
    purge_tenant_id: uuid.UUID
    other_tenant_id: uuid.UUID
    not_due_tenant_id: uuid.UUID
    restored_tenant_id: uuid.UUID
    shared_identity_id: uuid.UUID
    purge_user_id: uuid.UUID
    other_user_id: uuid.UUID
    purge_name: str


def require(condition: object, label: str) -> None:
    if not condition:
        raise SmokeFailure(label)


async def expect_error(awaitable, code: str) -> TenantPurgeError:
    try:
        await awaitable
    except TenantPurgeError as exc:
        require(exc.code == code, f"expected_{code}_got_{exc.code}")
        return exc
    raise SmokeFailure(f"expected_error_{code}")


async def seed_fixture() -> Fixture:
    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    shared_identity = Identity(
        email=f"g11-shared-{suffix}@local.clawith.test",
        username=f"g11shared{suffix}",
        password_login_enabled=False,
        email_verified=True,
        is_active=True,
    )
    other_identities = [
        Identity(
            email=f"g11-{label}-{suffix}@local.clawith.test",
            username=f"g11{label}{suffix}",
            password_login_enabled=False,
            email_verified=True,
            is_active=True,
        )
        for label in ("notdue", "restored")
    ]
    purge = Tenant(
        name=f"G11 Purge Sensitive Name {suffix}",
        slug=f"g11-purge-main-{suffix}",
        im_provider="web_only",
        is_active=False,
        deletion_requested_at=now - timedelta(days=31),
        deletion_scheduled_for=now - timedelta(days=1),
    )
    other = Tenant(
        name=f"G11 Isolation {suffix}",
        slug=f"g11-isolation-{suffix}",
        im_provider="web_only",
        is_active=True,
    )
    not_due = Tenant(
        name=f"G11 Not Due {suffix}",
        slug=f"g11-purge-not-due-{suffix}",
        im_provider="web_only",
        is_active=False,
        deletion_requested_at=now - timedelta(days=1),
        deletion_scheduled_for=now + timedelta(days=29),
    )
    restored = Tenant(
        name=f"G11 Restore Race {suffix}",
        slug=f"g11-purge-restored-{suffix}",
        im_provider="web_only",
        is_active=False,
        deletion_requested_at=now - timedelta(days=31),
        deletion_scheduled_for=now - timedelta(days=1),
    )

    async with async_session() as db:
        db.add_all([shared_identity, *other_identities, purge, other, not_due, restored])
        await db.flush()
        purge_user = User(
            identity_id=shared_identity.id,
            tenant_id=purge.id,
            display_name="G11 Purge Owner",
            role="org_owner",
            is_active=True,
        )
        other_user = User(
            identity_id=shared_identity.id,
            tenant_id=other.id,
            display_name="G11 Other Owner",
            role="org_owner",
            is_active=True,
        )
        not_due_user = User(
            identity_id=other_identities[0].id,
            tenant_id=not_due.id,
            display_name="G11 Not Due Owner",
            role="org_owner",
            is_active=True,
        )
        restored_user = User(
            identity_id=other_identities[1].id,
            tenant_id=restored.id,
            display_name="G11 Restored Owner",
            role="org_owner",
            is_active=True,
        )
        db.add_all([purge_user, other_user, not_due_user, restored_user])
        await db.flush()
        purge.owner_user_id = purge_user.id
        other.owner_user_id = other_user.id
        not_due.owner_user_id = not_due_user.id
        restored.owner_user_id = restored_user.id
        db.add_all(
            [
                TenantDeletionJob(
                    tenant_id=tenant.id,
                    status="scheduled",
                    eligible_at=tenant.deletion_scheduled_for,
                )
                for tenant in (purge, not_due, restored)
            ]
        )
        await db.commit()

    return Fixture(
        purge_tenant_id=purge.id,
        other_tenant_id=other.id,
        not_due_tenant_id=not_due.id,
        restored_tenant_id=restored.id,
        shared_identity_id=shared_identity.id,
        purge_user_id=purge_user.id,
        other_user_id=other_user.id,
        purge_name=purge.name,
    )


async def exercise(fixture: Fixture, storage_root: str) -> dict[str, object]:
    storage = LocalStorageBackend(storage_root)
    tenant_prefix = tenant_storage_prefix(str(fixture.purge_tenant_id))
    logo_key = f"_tenant_logos/{fixture.purge_tenant_id}.png"
    await storage.write_bytes(f"{tenant_prefix}/company.txt", b"fixture-only")
    await storage.write_bytes(logo_key, b"fixture-logo")

    async with async_session() as db:
        await expect_error(
            dry_run_tenant_purge(db, fixture.not_due_tenant_id, storage=storage),
            "tenant_not_due",
        )
        await db.rollback()

    hold_ids: list[uuid.UUID] = []
    for hold_type in ("operations", "legal"):
        async with async_session() as db:
            result = await create_tenant_purge_hold(
                db,
                fixture.purge_tenant_id,
                hold_type=hold_type,
                reason_code=f"case.{hold_type}.123",
                actor_user_id=None,
                actor_identity_id=None,
            )
            hold_ids.append(uuid.UUID(result["id"]))
    async with async_session() as db:
        await expect_error(
            dry_run_tenant_purge(db, fixture.purge_tenant_id, storage=storage),
            "tenant_purge_held",
        )
    for hold_id in hold_ids:
        async with async_session() as db:
            await release_tenant_purge_hold(
                db,
                fixture.purge_tenant_id,
                hold_id,
                reason_code="case.review.complete",
                actor_user_id=None,
                actor_identity_id=None,
            )
            repeated = await release_tenant_purge_hold(
                db,
                fixture.purge_tenant_id,
                hold_id,
                reason_code="case.review.complete",
                actor_user_id=None,
                actor_identity_id=None,
            )
            require(repeated["status"] == "released", "hold_release_not_idempotent")

    async with async_session() as db:
        await db.execute(
            text(
                "CREATE TABLE g11_cross_tenant_probe ("
                "id uuid PRIMARY KEY, tenant_id uuid NOT NULL, target_user_id uuid NOT NULL "
                "REFERENCES users(id))"
            )
        )
        await db.execute(
            text(
                "INSERT INTO g11_cross_tenant_probe (id, tenant_id, target_user_id) "
                "VALUES (:id, :tenant_id, :target_user_id)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": fixture.other_tenant_id,
                "target_user_id": fixture.purge_user_id,
            },
        )
        await db.commit()
    async with async_session() as db:
        await expect_error(
            dry_run_tenant_purge(db, fixture.purge_tenant_id, storage=storage),
            "cross_tenant_reference_detected",
        )
        await db.rollback()
        await db.execute(text("DROP TABLE g11_cross_tenant_probe"))
        await db.commit()

    async with async_session() as db:
        await db.execute(text("CREATE TABLE g11_no_pk_probe (tenant_id uuid NOT NULL)"))
        await db.commit()
    async with async_session() as db:
        await expect_error(
            dry_run_tenant_purge(db, fixture.purge_tenant_id, storage=storage),
            "tenant_table_without_primary_key",
        )
        await db.rollback()
        await db.execute(text("DROP TABLE g11_no_pk_probe"))
        await db.commit()

    async with async_session() as db:
        first_plan = await dry_run_tenant_purge(db, fixture.purge_tenant_id, storage=storage)
    async with async_session() as db:
        second_plan = await dry_run_tenant_purge(db, fixture.purge_tenant_id, storage=storage)
    require(first_plan["plan_digest"] == second_plan["plan_digest"], "dry_run_not_reentrant")

    async with async_session() as db:
        await db.execute(
            text("CREATE TABLE g11_schema_drift_probe (id uuid PRIMARY KEY, tenant_id uuid NOT NULL)")
        )
        await db.commit()
    await expect_error(
        execute_tenant_purge(fixture.purge_tenant_id, storage=storage),
        "purge_plan_changed",
    )
    async with async_session() as db:
        require(await db.get(Tenant, fixture.purge_tenant_id) is not None, "schema_drift_deleted_tenant")
        await db.execute(text("DROP TABLE g11_schema_drift_probe"))
        await db.commit()

    async with async_session() as db:
        await dry_run_tenant_purge(db, fixture.purge_tenant_id, storage=storage)
    failing_storage = FailOnceStorage(storage_root)
    await expect_error(
        execute_tenant_purge(fixture.purge_tenant_id, storage=failing_storage),
        "storage_cleanup_failed",
    )
    async with async_session() as db:
        require(await db.get(Tenant, fixture.purge_tenant_id) is not None, "partial_failure_deleted_tenant")
        job = (
            await db.execute(
                select(TenantDeletionJob).where(
                    TenantDeletionJob.tenant_id == fixture.purge_tenant_id
                )
            )
        ).scalar_one()
        require(job.status == "failed", "partial_failure_job_not_failed")
    require(await storage.is_dir(tenant_prefix), "partial_failure_did_not_preserve_retryable_storage")
    require(not await storage.exists(logo_key), "partial_failure_did_not_exercise_partial_cleanup")

    async with async_session() as db:
        await dry_run_tenant_purge(db, fixture.purge_tenant_id, storage=storage)
    receipt = await execute_tenant_purge(fixture.purge_tenant_id, storage=storage)
    require(receipt["status"] == "purged", "physical_purge_not_completed")
    require(receipt["storage_summary"]["prefixes_verified_absent"] == 2, "storage_not_verified_absent")
    repeated_receipt = await execute_tenant_purge(fixture.purge_tenant_id, storage=storage)
    require(repeated_receipt["status"] == "already_purged", "physical_purge_not_idempotent")
    require(repeated_receipt["receipt_hash"] == receipt["receipt_hash"], "receipt_hash_changed")

    async with async_session() as db:
        require(await db.get(Tenant, fixture.purge_tenant_id) is None, "target_tenant_survived")
        require(await db.get(User, fixture.purge_user_id) is None, "target_membership_survived")
        require(await db.get(Identity, fixture.shared_identity_id) is not None, "global_identity_deleted")
        require(await db.get(Tenant, fixture.other_tenant_id) is not None, "other_tenant_deleted")
        require(await db.get(User, fixture.other_user_id) is not None, "other_membership_deleted")
        tombstone = await db.get(TenantDeletionTombstone, fixture.purge_tenant_id)
        require(tombstone is not None, "tombstone_missing")
        serialized = json.dumps(
            {
                "tenant_id": str(tombstone.tenant_id),
                "name_digest": tombstone.name_digest,
                "table_counts": tombstone.table_counts,
                "storage_summary": tombstone.storage_summary,
                "receipt_hash": tombstone.receipt_hash,
            },
            sort_keys=True,
        )
        require(fixture.purge_name not in serialized, "tombstone_leaked_company_name")

    async with async_session() as db:
        await dry_run_tenant_purge(db, fixture.restored_tenant_id, storage=storage)
        restored = await db.get(Tenant, fixture.restored_tenant_id, with_for_update=True)
        restored.is_active = True
        restored.deletion_requested_at = None
        restored.deletion_scheduled_for = None
        restored.deletion_requested_by_user_id = None
        await db.execute(
            delete(TenantDeletionHold).where(
                TenantDeletionHold.tenant_id == fixture.restored_tenant_id
            )
        )
        await db.execute(
            delete(TenantDeletionJob).where(
                TenantDeletionJob.tenant_id == fixture.restored_tenant_id
            )
        )
        await db.commit()
    await expect_error(
        execute_tenant_purge(fixture.restored_tenant_id, storage=storage),
        "tenant_restored_or_not_scheduled",
    )
    async with async_session() as db:
        restored = await db.get(Tenant, fixture.restored_tenant_id)
        require(restored is not None and restored.is_active, "restore_race_lost")
        restored_job = (
            await db.execute(
                select(TenantDeletionJob).where(
                    TenantDeletionJob.tenant_id == fixture.restored_tenant_id
                )
            )
        ).scalar_one_or_none()
        require(restored_job is None, "restore_race_recreated_purge_job")
        states = await list_tenant_purge_states(db)
        require(
            all(item["tenant_id"] != str(fixture.purge_tenant_id) for item in states["items"]),
            "purged_tenant_remained_in_queue",
        )
        require(
            any(item["tenant_id"] == str(fixture.purge_tenant_id) for item in states["tombstones"]),
            "purge_receipt_not_listed",
        )

    return {
        "status": "passed",
        "assertions": 32,
        "receipt_hash": receipt["receipt_hash"],
        "rows_total": receipt["rows_total"],
    }


async def main_async(storage_root: str) -> None:
    fixture = await seed_fixture()
    result = await exercise(fixture, storage_root)
    print(json.dumps({"tenant_purge_postgres_smoke": result}, sort_keys=True))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-root", required=True)
    args = parser.parse_args()

    async def run() -> None:
        try:
            await main_async(args.storage_root)
        finally:
            await engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
