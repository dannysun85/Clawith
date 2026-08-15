"""Fail-closed orchestration for physical deletion of expired tenants.

The public company lifecycle remains a 30-day recoverable suspension.  This
module is the separate operator lane that runs only after that window, keeps
global Identities, discovers the live PostgreSQL FK graph, and refuses unknown
or cross-tenant dependencies before deleting customer rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import re
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.tenant_deletion import (
    TenantDeletionHold,
    TenantDeletionJob,
    TenantDeletionTombstone,
)
from app.services.agent_manager import agent_manager
from app.services.storage import (
    get_storage_backend,
    normalize_storage_key,
    tenant_storage_prefix,
)


_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_REASON_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,99}$")
_TOMBSTONE_SCHEMA_VERSION = 1
_RECENT_PURGE_LEASE = timedelta(hours=2)
_PROTECTED_TABLES = frozenset(
    {
        "alembic_version",
        "identities",
        "identity_mfa_recovery_codes",
        "system_settings",
        "tenant_deletion_tombstones",
    }
)
_DIGEST_VOLATILE_TABLES = frozenset(
    {"audit_logs", "tenant_deletion_holds", "tenant_deletion_jobs"}
)
_ON_DELETE = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


class TenantPurgeError(RuntimeError):
    """A safe, stable purge failure that can be shown to operators."""

    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: frozenset[str]
    nullable_columns: frozenset[str]
    primary_key: tuple[str, ...]


@dataclass(frozen=True)
class ForeignKeySpec:
    name: str
    child_table: str
    parent_table: str
    child_columns: tuple[str, ...]
    parent_columns: tuple[str, ...]
    on_delete: str


@dataclass
class PurgePlan:
    planner: "TenantRowPlanner"
    tenant_id: uuid.UUID
    table_counts: dict[str, int]
    agent_ids: tuple[uuid.UUID, ...]
    storage_prefixes: tuple[str, ...]
    storage_summary: dict[str, int]
    schema_digest: str
    plan_digest: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "status": "dry_run_passed",
            "plan_digest": self.plan_digest,
            "table_counts": self.table_counts,
            "rows_total": sum(self.table_counts.values()),
            "storage_summary": self.storage_summary,
            "agent_count": len(self.agent_ids),
        }


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise TenantPurgeError(
            "unsupported_schema_identifier",
            "The database schema contains an identifier that the purge planner cannot safely quote",
        )
    return f'"{value}"'


def _safe_reason_code(value: str) -> str:
    normalized = (value or "").strip().lower()
    if not _REASON_CODE_RE.fullmatch(normalized):
        raise TenantPurgeError(
            "invalid_reason_code",
            "Reason codes must be 3-100 lowercase letters, numbers, dots, dashes, or underscores",
            status_code=400,
        )
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hmac_hex(secret: str, purpose: str, payload: Any) -> str:
    body = f"{purpose}:{_canonical_json(payload)}".encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TenantRowPlanner:
    """Materialize the tenant-owned row closure before any destructive SQL."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.tables: dict[str, TableSpec] = {}
        self.foreign_keys: tuple[ForeignKeySpec, ...] = ()
        self.markers: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.schema_digest = ""
        self._token = uuid.uuid4().hex[:8]

    async def build(self) -> None:
        await self._load_catalog()
        await self._create_markers()
        await self._seed_direct_tenant_rows()
        await self._propagate_owned_rows()
        await self._assert_no_cross_tenant_markers()
        await self._load_counts()

    async def _load_catalog(self) -> None:
        column_rows = (
            await self.db.execute(
                text(
                    """
                    SELECT c.table_name, c.column_name, c.is_nullable
                      FROM information_schema.columns c
                      JOIN information_schema.tables t
                        ON t.table_schema = c.table_schema
                       AND t.table_name = c.table_name
                     WHERE c.table_schema = 'public'
                       AND t.table_type = 'BASE TABLE'
                     ORDER BY c.table_name, c.ordinal_position
                    """
                )
            )
        ).all()
        columns: dict[str, set[str]] = {}
        nullable: dict[str, set[str]] = {}
        for table_name, column_name, is_nullable in column_rows:
            _quote_identifier(table_name)
            _quote_identifier(column_name)
            columns.setdefault(table_name, set()).add(column_name)
            if is_nullable == "YES":
                nullable.setdefault(table_name, set()).add(column_name)

        pk_rows = (
            await self.db.execute(
                text(
                    """
                    SELECT tc.table_name,
                           array_agg(kcu.column_name ORDER BY kcu.ordinal_position)
                      FROM information_schema.table_constraints tc
                      JOIN information_schema.key_column_usage kcu
                        ON kcu.constraint_schema = tc.constraint_schema
                       AND kcu.constraint_name = tc.constraint_name
                       AND kcu.table_name = tc.table_name
                     WHERE tc.table_schema = 'public'
                       AND tc.constraint_type = 'PRIMARY KEY'
                     GROUP BY tc.table_name
                     ORDER BY tc.table_name
                    """
                )
            )
        ).all()
        primary_keys = {table_name: tuple(pk_columns) for table_name, pk_columns in pk_rows}
        self.tables = {
            name: TableSpec(
                name=name,
                columns=frozenset(table_columns),
                nullable_columns=frozenset(nullable.get(name, set())),
                primary_key=primary_keys.get(name, ()),
            )
            for name, table_columns in columns.items()
        }
        for spec in self.tables.values():
            if "tenant_id" in spec.columns and spec.name not in _PROTECTED_TABLES and not spec.primary_key:
                raise TenantPurgeError(
                    "tenant_table_without_primary_key",
                    f"Table {spec.name} has tenant_id but no primary key; purge is blocked",
                )

        fk_rows = (
            await self.db.execute(
                text(
                    """
                    SELECT constraint_row.conname,
                           constraint_row.child_table,
                           constraint_row.parent_table,
                           array_agg(child_attribute.attname ORDER BY child_key.ord),
                           array_agg(parent_attribute.attname ORDER BY child_key.ord),
                           constraint_row.confdeltype
                      FROM (
                            SELECT c.oid, c.conname, c.conkey, c.confkey,
                                   c.confdeltype,
                                   child.relname AS child_table,
                                   parent.relname AS parent_table,
                                   child.oid AS child_oid,
                                   parent.oid AS parent_oid
                              FROM pg_constraint c
                              JOIN pg_class child ON child.oid = c.conrelid
                              JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
                              JOIN pg_class parent ON parent.oid = c.confrelid
                              JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
                             WHERE c.contype = 'f'
                               AND child_ns.nspname = 'public'
                               AND parent_ns.nspname = 'public'
                           ) AS constraint_row
                      JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
                           AS child_key(attnum, ord) ON TRUE
                      JOIN LATERAL unnest(constraint_row.confkey) WITH ORDINALITY
                           AS parent_key(attnum, ord) ON parent_key.ord = child_key.ord
                      JOIN pg_attribute child_attribute
                        ON child_attribute.attrelid = constraint_row.child_oid
                       AND child_attribute.attnum = child_key.attnum
                      JOIN pg_attribute parent_attribute
                        ON parent_attribute.attrelid = constraint_row.parent_oid
                       AND parent_attribute.attnum = parent_key.attnum
                     GROUP BY constraint_row.conname,
                              constraint_row.child_table,
                              constraint_row.parent_table,
                              constraint_row.confdeltype
                     ORDER BY constraint_row.child_table,
                              constraint_row.parent_table,
                              constraint_row.conname
                    """
                )
            )
        ).all()
        foreign_keys: list[ForeignKeySpec] = []
        for name, child, parent, child_columns, parent_columns, confdeltype in fk_rows:
            for identifier in (name, child, parent, *child_columns, *parent_columns):
                _quote_identifier(identifier)
            foreign_keys.append(
                ForeignKeySpec(
                    name=name,
                    child_table=child,
                    parent_table=parent,
                    child_columns=tuple(child_columns),
                    parent_columns=tuple(parent_columns),
                    on_delete=_ON_DELETE.get(confdeltype, "UNKNOWN"),
                )
            )
        self.foreign_keys = tuple(foreign_keys)
        self.schema_digest = hashlib.sha256(
            _canonical_json(
                {
                    "tables": {
                        name: {
                            "columns": sorted(spec.columns),
                            "nullable": sorted(spec.nullable_columns),
                            "pk": spec.primary_key,
                        }
                        for name, spec in sorted(self.tables.items())
                    },
                    "foreign_keys": [
                        {
                            "name": fk.name,
                            "child": fk.child_table,
                            "parent": fk.parent_table,
                            "child_columns": fk.child_columns,
                            "parent_columns": fk.parent_columns,
                            "on_delete": fk.on_delete,
                        }
                        for fk in self.foreign_keys
                    ],
                }
            ).encode()
        ).hexdigest()

    async def _create_markers(self) -> None:
        for table_name, spec in sorted(self.tables.items()):
            if table_name in _PROTECTED_TABLES or not spec.primary_key:
                continue
            marker = f"tp_{self._token}_{hashlib.sha256(table_name.encode()).hexdigest()[:10]}"
            self.markers[table_name] = marker
            pk_sql = ", ".join(_quote_identifier(column) for column in spec.primary_key)
            await self.db.execute(
                text(
                    f"CREATE TEMP TABLE {_quote_identifier(marker)} ON COMMIT DROP "
                    f"AS SELECT {pk_sql} FROM {_quote_identifier(table_name)} WITH NO DATA"
                )
            )
            await self.db.execute(
                text(
                    f"ALTER TABLE {_quote_identifier(marker)} "
                    f"ADD PRIMARY KEY ({pk_sql})"
                )
            )

    async def _insert_count(self, insert_sql: str, params: dict[str, Any] | None = None) -> int:
        result = await self.db.execute(
            text(
                "WITH inserted_rows AS ("
                + insert_sql
                + " RETURNING 1) SELECT count(*) FROM inserted_rows"
            ),
            params or {},
        )
        return int(result.scalar_one())

    async def _seed_direct_tenant_rows(self) -> None:
        tenant_marker = self.markers.get("tenants")
        if not tenant_marker:
            raise TenantPurgeError("tenant_table_unavailable", "Tenant table is unavailable to the purge planner")
        await self._insert_count(
            f"INSERT INTO {_quote_identifier(tenant_marker)} (id) "
            f"SELECT id FROM tenants WHERE id = :tenant_id ON CONFLICT DO NOTHING",
            {"tenant_id": self.tenant_id},
        )
        for table_name, spec in sorted(self.tables.items()):
            marker = self.markers.get(table_name)
            if not marker or table_name == "tenants" or "tenant_id" not in spec.columns:
                continue
            pk_sql = ", ".join(_quote_identifier(column) for column in spec.primary_key)
            await self._insert_count(
                f"INSERT INTO {_quote_identifier(marker)} ({pk_sql}) "
                f"SELECT {pk_sql} FROM {_quote_identifier(table_name)} "
                f"WHERE tenant_id = :tenant_id ON CONFLICT DO NOTHING",
                {"tenant_id": self.tenant_id},
            )

    def _join_condition(
        self,
        left_alias: str,
        left_columns: tuple[str, ...],
        right_alias: str,
        right_columns: tuple[str, ...],
    ) -> str:
        return " AND ".join(
            f"{left_alias}.{_quote_identifier(left)} = {right_alias}.{_quote_identifier(right)}"
            for left, right in zip(left_columns, right_columns, strict=True)
        )

    def _marker_join(self, table_alias: str, marker_alias: str, table_name: str) -> str:
        spec = self.tables[table_name]
        return self._join_condition(
            table_alias,
            spec.primary_key,
            marker_alias,
            spec.primary_key,
        )

    async def _seed_participants(self) -> int:
        if not {"participants", "users", "agents"}.issubset(self.markers):
            return 0
        participants = self.tables["participants"]
        pk_sql = ", ".join(f"p.{_quote_identifier(column)}" for column in participants.primary_key)
        marker_columns = ", ".join(_quote_identifier(column) for column in participants.primary_key)
        return await self._insert_count(
            f"INSERT INTO {_quote_identifier(self.markers['participants'])} ({marker_columns}) "
            f"SELECT {pk_sql} FROM participants p "
            f"WHERE (p.type = 'user' AND EXISTS ("
            f"SELECT 1 FROM {_quote_identifier(self.markers['users'])} u WHERE u.id = p.ref_id"
            f")) OR (p.type = 'agent' AND EXISTS ("
            f"SELECT 1 FROM {_quote_identifier(self.markers['agents'])} a WHERE a.id = p.ref_id"
            f")) ON CONFLICT DO NOTHING"
        )

    async def _matching_fk_exists(self, fk: ForeignKeySpec) -> bool:
        parent_marker = self.markers.get(fk.parent_table)
        if not parent_marker:
            return False
        child_parent = self._join_condition("c", fk.child_columns, "p", fk.parent_columns)
        parent_marker_join = self._marker_join("p", "pm", fk.parent_table)
        result = await self.db.execute(
            text(
                f"SELECT EXISTS (SELECT 1 FROM {_quote_identifier(fk.child_table)} c "
                f"JOIN {_quote_identifier(fk.parent_table)} p ON {child_parent} "
                f"JOIN {_quote_identifier(parent_marker)} pm ON {parent_marker_join})"
            )
        )
        return bool(result.scalar_one())

    async def _propagate_fk(self, fk: ForeignKeySpec) -> int:
        parent_marker = self.markers.get(fk.parent_table)
        child_marker = self.markers.get(fk.child_table)
        if not parent_marker:
            return 0
        if not child_marker:
            if fk.child_table in _PROTECTED_TABLES and await self._matching_fk_exists(fk):
                raise TenantPurgeError(
                    "protected_global_dependency",
                    f"Protected global table {fk.child_table} references tenant-owned data",
                )
            child_spec = self.tables.get(fk.child_table)
            if child_spec and not child_spec.primary_key and await self._matching_fk_exists(fk):
                raise TenantPurgeError(
                    "dependent_table_without_primary_key",
                    f"Dependent table {fk.child_table} has no primary key; purge is blocked",
                )
            return 0

        child_spec = self.tables[fk.child_table]
        child_parent = self._join_condition("c", fk.child_columns, "p", fk.parent_columns)
        parent_marker_join = self._marker_join("p", "pm", fk.parent_table)
        if "tenant_id" in child_spec.columns:
            mismatch = await self.db.execute(
                text(
                    f"SELECT EXISTS (SELECT 1 FROM {_quote_identifier(fk.child_table)} c "
                    f"JOIN {_quote_identifier(fk.parent_table)} p ON {child_parent} "
                    f"JOIN {_quote_identifier(parent_marker)} pm ON {parent_marker_join} "
                    f"WHERE c.tenant_id IS NOT NULL AND c.tenant_id <> :tenant_id)"
                ),
                {"tenant_id": self.tenant_id},
            )
            if bool(mismatch.scalar_one()):
                raise TenantPurgeError(
                    "cross_tenant_reference_detected",
                    f"Table {fk.child_table} contains a cross-tenant reference through {fk.name}",
                )

        marker_columns = ", ".join(_quote_identifier(column) for column in child_spec.primary_key)
        select_columns = ", ".join(f"c.{_quote_identifier(column)}" for column in child_spec.primary_key)
        return await self._insert_count(
            f"INSERT INTO {_quote_identifier(child_marker)} ({marker_columns}) "
            f"SELECT DISTINCT {select_columns} FROM {_quote_identifier(fk.child_table)} c "
            f"JOIN {_quote_identifier(fk.parent_table)} p ON {child_parent} "
            f"JOIN {_quote_identifier(parent_marker)} pm ON {parent_marker_join} "
            f"ON CONFLICT DO NOTHING"
        )

    async def _propagate_owned_rows(self) -> None:
        for _iteration in range(len(self.tables) + 3):
            inserted = await self._seed_participants()
            for fk in self.foreign_keys:
                inserted += await self._propagate_fk(fk)
            if inserted == 0:
                return
        raise TenantPurgeError(
            "ownership_graph_did_not_converge",
            "The tenant ownership graph did not converge during dry-run",
        )

    async def _assert_no_cross_tenant_markers(self) -> None:
        """Reject a marked shared row that also points into another tenant.

        Some historical join tables carry no tenant_id and inherit ownership
        through their foreign keys.  They are safe to delete only when every
        tenant-bearing parent belongs to the target tenant (or is global).
        """
        for fk in self.foreign_keys:
            marker = self.markers.get(fk.child_table)
            parent_spec = self.tables.get(fk.parent_table)
            if not marker or parent_spec is None or "tenant_id" not in parent_spec.columns:
                continue
            child_marker_join = self._marker_join("c", "cm", fk.child_table)
            child_parent = self._join_condition(
                "c",
                fk.child_columns,
                "p",
                fk.parent_columns,
            )
            result = await self.db.execute(
                text(
                    f"SELECT EXISTS (SELECT 1 FROM {_quote_identifier(fk.child_table)} c "
                    f"JOIN {_quote_identifier(marker)} cm ON {child_marker_join} "
                    f"JOIN {_quote_identifier(fk.parent_table)} p ON {child_parent} "
                    f"WHERE p.tenant_id IS NOT NULL AND p.tenant_id <> :tenant_id)"
                ),
                {"tenant_id": self.tenant_id},
            )
            if bool(result.scalar_one()):
                raise TenantPurgeError(
                    "cross_tenant_reference_detected",
                    f"Marked rows in {fk.child_table} also reference another tenant through {fk.name}",
                )

    async def _load_counts(self) -> None:
        counts: dict[str, int] = {}
        for table_name, marker in sorted(self.markers.items()):
            count = int(
                (
                    await self.db.execute(
                        text(f"SELECT count(*) FROM {_quote_identifier(marker)}")
                    )
                ).scalar_one()
            )
            if count:
                counts[table_name] = count
        if counts.get("tenants") != 1:
            raise TenantPurgeError(
                "tenant_not_planned",
                "The target tenant could not be materialized into the purge plan",
                status_code=404,
            )
        self.counts = counts

    async def agent_ids(self) -> tuple[uuid.UUID, ...]:
        marker = self.markers.get("agents")
        if not marker or not self.counts.get("agents"):
            return ()
        rows = await self.db.execute(
            text(f"SELECT id FROM {_quote_identifier(marker)} ORDER BY id")
        )
        return tuple(row[0] for row in rows.all())

    async def _null_nullable_edges(self) -> set[str]:
        removed_edges: set[str] = set()
        nonempty = set(self.counts)
        for fk in self.foreign_keys:
            if fk.child_table not in nonempty or fk.parent_table not in nonempty:
                continue
            child_spec = self.tables[fk.child_table]
            if not set(fk.child_columns).issubset(child_spec.nullable_columns):
                continue
            child_marker = self.markers[fk.child_table]
            parent_marker = self.markers[fk.parent_table]
            assignments = ", ".join(
                f"{_quote_identifier(column)} = NULL" for column in fk.child_columns
            )
            child_marker_join = self._marker_join("c", "cm", fk.child_table)
            child_parent = self._join_condition("c", fk.child_columns, "p", fk.parent_columns)
            parent_marker_join = self._marker_join("p", "pm", fk.parent_table)
            await self.db.execute(
                text(
                    f"UPDATE {_quote_identifier(fk.child_table)} c SET {assignments} "
                    f"FROM {_quote_identifier(child_marker)} cm, "
                    f"{_quote_identifier(fk.parent_table)} p, "
                    f"{_quote_identifier(parent_marker)} pm "
                    f"WHERE {child_marker_join} AND {child_parent} AND {parent_marker_join}"
                )
            )
            removed_edges.add(fk.name)
        return removed_edges

    def _delete_order(self, removed_edges: set[str]) -> list[str]:
        nodes = set(self.counts)
        outgoing: dict[str, set[str]] = {node: set() for node in nodes}
        indegree: dict[str, int] = {node: 0 for node in nodes}
        for fk in self.foreign_keys:
            if fk.name in removed_edges:
                continue
            child = fk.child_table
            parent = fk.parent_table
            if child not in nodes or parent not in nodes:
                continue
            if parent not in outgoing[child]:
                outgoing[child].add(parent)
                indegree[parent] += 1

        ready = sorted(node for node, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for parent in sorted(outgoing[node]):
                indegree[parent] -= 1
                if indegree[parent] == 0:
                    ready.append(parent)
                    ready.sort()
        if len(order) != len(nodes):
            cyclic = sorted(node for node, degree in indegree.items() if degree > 0)
            raise TenantPurgeError(
                "unsupported_non_nullable_fk_cycle",
                "Non-nullable FK cycle blocks purge: " + ", ".join(cyclic),
            )
        if "tenants" not in order:
            raise TenantPurgeError(
                "tenant_delete_order_invalid",
                "The purge plan does not include the tenant row",
            )
        # Nullable tenant-owned references are cleared before deletion.  Once
        # those edges are removed, PostgreSQL no longer requires a particular
        # relative position for the tenant row, but deleting it last remains an
        # important fail-safe and makes the physical lifecycle unambiguous.
        order.remove("tenants")
        order.append("tenants")
        return order

    async def delete_planned_rows(self) -> dict[str, int]:
        removed_edges = await self._null_nullable_edges()
        order = self._delete_order(removed_edges)
        deleted: dict[str, int] = {}
        for table_name in order:
            marker = self.markers[table_name]
            marker_join = self._marker_join("target", "planned", table_name)
            result = await self.db.execute(
                text(
                    f"DELETE FROM {_quote_identifier(table_name)} target "
                    f"USING {_quote_identifier(marker)} planned WHERE {marker_join}"
                )
            )
            rowcount = int(result.rowcount or 0)
            expected = self.counts[table_name]
            if rowcount != expected:
                raise TenantPurgeError(
                    "purge_row_count_mismatch",
                    f"Table {table_name} deleted {rowcount} rows but dry-run planned {expected}",
                )
            deleted[table_name] = rowcount
        return deleted


async def _active_holds(db: AsyncSession, tenant_id: uuid.UUID) -> list[TenantDeletionHold]:
    result = await db.execute(
        select(TenantDeletionHold)
        .where(
            TenantDeletionHold.tenant_id == tenant_id,
            TenantDeletionHold.released_at.is_(None),
        )
        .order_by(TenantDeletionHold.created_at, TenantDeletionHold.id)
    )
    return list(result.scalars().all())


async def _ensure_job(db: AsyncSession, tenant: Tenant) -> TenantDeletionJob:
    result = await db.execute(
        select(TenantDeletionJob)
        .where(TenantDeletionJob.tenant_id == tenant.id)
        .with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is None:
        if tenant.deletion_scheduled_for is None:
            raise TenantPurgeError("tenant_not_scheduled", "Tenant deletion has not been scheduled")
        job = TenantDeletionJob(
            tenant_id=tenant.id,
            status="scheduled",
            eligible_at=tenant.deletion_scheduled_for,
        )
        db.add(job)
        await db.flush()
    elif tenant.deletion_scheduled_for is not None:
        job.eligible_at = tenant.deletion_scheduled_for
    return job


def _assert_eligible(tenant: Tenant, now: datetime) -> None:
    if tenant.is_active or tenant.deletion_requested_at is None or tenant.deletion_scheduled_for is None:
        raise TenantPurgeError(
            "tenant_restored_or_not_scheduled",
            "Only an inactive tenant with an explicit deletion schedule can be purged",
        )
    if tenant.deletion_scheduled_for > now:
        raise TenantPurgeError(
            "tenant_not_due",
            "The recoverable deletion window has not elapsed",
        )


async def _lock_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        tombstone = await db.get(TenantDeletionTombstone, tenant_id)
        if tombstone is not None:
            raise TenantPurgeError(
                "tenant_already_purged",
                "The tenant has already been physically purged",
                status_code=200,
            )
        raise TenantPurgeError("tenant_not_found", "Tenant not found", status_code=404)
    return tenant


async def _assert_no_external_blockers(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    unresolved_agentbay = int(
        (
            await db.execute(
                text(
                    """
                    SELECT count(*)
                      FROM agentbay_session_ledger
                     WHERE tenant_id = :tenant_id
                       AND status IN ('active', 'cleanup_required', 'provider_identity_collision')
                    """
                ),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()
    )
    if unresolved_agentbay:
        raise TenantPurgeError(
            "agentbay_cleanup_unconfirmed",
            "Provider sandbox cleanup must be proven before tenant purge",
        )

    inflight_runs = int(
        (
            await db.execute(
                text(
                    """
                    SELECT count(*)
                      FROM agent_runs run
                     WHERE run.tenant_id = :tenant_id
                       AND NOT EXISTS (
                           SELECT 1
                             FROM agent_run_events event
                            WHERE event.run_id = run.id
                              AND event.event_type IN ('run_completed', 'run_failed', 'run_cancelled')
                       )
                    """
                ),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()
    )
    if inflight_runs:
        raise TenantPurgeError(
            "inflight_agent_runs",
            "In-flight Agent runs must be reconciled before tenant purge",
        )

    pending_email = int(
        (
            await db.execute(
                text(
                    """
                    SELECT count(*)
                      FROM outbound_email_deliveries
                     WHERE tenant_id = :tenant_id
                       AND status IN ('queued', 'sending', 'retry_wait')
                    """
                ),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()
    )
    if pending_email:
        raise TenantPurgeError(
            "pending_email_delivery",
            "Pending tenant email deliveries must reach a terminal state before purge",
        )


def _storage_prefixes(tenant_id: uuid.UUID, agent_ids: tuple[uuid.UUID, ...]) -> tuple[str, ...]:
    prefixes = [normalize_storage_key(str(agent_id)) for agent_id in agent_ids]
    prefixes.extend(
        (
            tenant_storage_prefix(str(tenant_id)),
            normalize_storage_key(f"_tenant_logos/{tenant_id}.png"),
        )
    )
    return tuple(sorted(set(prefixes)))


async def _inspect_storage(prefixes: tuple[str, ...], storage=None) -> dict[str, int]:
    backend = storage or get_storage_backend()
    existing = 0
    directories = 0
    files = 0
    try:
        for prefix in prefixes:
            is_directory = await backend.is_dir(prefix)
            exists = is_directory or await backend.exists(prefix)
            if not exists:
                continue
            existing += 1
            if is_directory:
                directories += 1
            else:
                files += 1
    except Exception as exc:
        raise TenantPurgeError(
            "storage_inspection_failed",
            "Tenant storage could not be inspected safely",
        ) from exc
    return {
        "prefixes_total": len(prefixes),
        "prefixes_existing": existing,
        "directories_existing": directories,
        "files_existing": files,
    }


def _plan_digest(
    tenant_id: uuid.UUID,
    planner: TenantRowPlanner,
    agent_ids: tuple[uuid.UUID, ...],
    prefixes: tuple[str, ...],
) -> str:
    stable_counts = {
        table_name: count
        for table_name, count in planner.counts.items()
        if table_name not in _DIGEST_VOLATILE_TABLES
    }
    return hashlib.sha256(
        _canonical_json(
            {
                "tenant_id": str(tenant_id),
                "schema_digest": planner.schema_digest,
                "table_counts": stable_counts,
                "agent_ids": [str(value) for value in agent_ids],
                "storage_prefixes": list(prefixes),
            }
        ).encode()
    ).hexdigest()


async def _build_plan(db: AsyncSession, tenant_id: uuid.UUID, *, storage=None) -> PurgePlan:
    await _assert_no_external_blockers(db, tenant_id)
    planner = TenantRowPlanner(db, tenant_id)
    await planner.build()
    agent_ids = await planner.agent_ids()
    prefixes = _storage_prefixes(tenant_id, agent_ids)
    storage_summary = await _inspect_storage(prefixes, storage=storage)
    return PurgePlan(
        planner=planner,
        tenant_id=tenant_id,
        table_counts=planner.counts,
        agent_ids=agent_ids,
        storage_prefixes=prefixes,
        storage_summary=storage_summary,
        schema_digest=planner.schema_digest,
        plan_digest=_plan_digest(tenant_id, planner, agent_ids, prefixes),
    )


async def dry_run_tenant_purge(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    storage=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Lock, validate, and persist a count-only purge plan without deleting."""

    current_time = now or datetime.now(UTC)
    tenant = await _lock_tenant(db, tenant_id)
    _assert_eligible(tenant, current_time)
    job = await _ensure_job(db, tenant)
    if job.status == "purging" and job.started_at and job.started_at > current_time - _RECENT_PURGE_LEASE:
        raise TenantPurgeError("purge_in_progress", "A tenant purge attempt is already in progress")
    holds = await _active_holds(db, tenant_id)
    if holds:
        job.status = "held"
        await db.commit()
        raise TenantPurgeError("tenant_purge_held", "An active legal or operational hold blocks purge")

    plan = await _build_plan(db, tenant_id, storage=storage)
    job.status = "dry_run_passed"
    job.plan_digest = plan.plan_digest
    job.table_counts = plan.table_counts
    job.storage_summary = plan.storage_summary
    job.last_error_code = None
    job.last_error_at = None
    job.started_at = None
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="tenant_purge_dry_run_passed",
            details={
                "tenant_id": str(tenant_id),
                "plan_digest": plan.plan_digest,
                "tables": len(plan.table_counts),
                "rows_total": sum(plan.table_counts.values()),
                "storage_prefixes": plan.storage_summary["prefixes_total"],
            },
        )
    )
    await db.commit()
    return plan.public_payload()


async def create_tenant_purge_hold(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    hold_type: str,
    reason_code: str,
    actor_user_id: uuid.UUID | None,
    actor_identity_id: uuid.UUID | None,
) -> dict[str, Any]:
    normalized_type = (hold_type or "").strip().lower()
    if normalized_type not in {"legal", "operations"}:
        raise TenantPurgeError("invalid_hold_type", "Hold type must be legal or operations", status_code=400)
    normalized_reason = _safe_reason_code(reason_code)
    tenant = await _lock_tenant(db, tenant_id)
    if tenant.deletion_requested_at is None or tenant.deletion_scheduled_for is None:
        raise TenantPurgeError("tenant_not_scheduled", "Only a scheduled tenant can be placed on purge hold")
    job = await _ensure_job(db, tenant)
    if job.status == "purging":
        raise TenantPurgeError("purge_in_progress", "A hold cannot be added after purge has started")
    existing_result = await db.execute(
        select(TenantDeletionHold)
        .where(
            TenantDeletionHold.tenant_id == tenant_id,
            TenantDeletionHold.hold_type == normalized_type,
            TenantDeletionHold.released_at.is_(None),
        )
        .with_for_update()
    )
    hold = existing_result.scalar_one_or_none()
    if hold is None:
        hold = TenantDeletionHold(
            tenant_id=tenant_id,
            hold_type=normalized_type,
            reason_code=normalized_reason,
            created_by_identity_id=actor_identity_id,
        )
        db.add(hold)
        await db.flush()
    elif hold.reason_code != normalized_reason:
        raise TenantPurgeError(
            "active_hold_conflict",
            "An active hold of this type already exists with another reason code",
        )
    job.status = "held"
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="tenant_purge_hold_created",
            details={
                "tenant_id": str(tenant_id),
                "hold_id": str(hold.id),
                "hold_type": hold.hold_type,
                "reason_code": hold.reason_code,
            },
        )
    )
    await db.commit()
    return {
        "id": str(hold.id),
        "tenant_id": str(tenant_id),
        "hold_type": hold.hold_type,
        "reason_code": hold.reason_code,
        "status": "active",
    }


async def release_tenant_purge_hold(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    hold_id: uuid.UUID,
    *,
    reason_code: str,
    actor_user_id: uuid.UUID | None,
    actor_identity_id: uuid.UUID | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(UTC)
    normalized_reason = _safe_reason_code(reason_code)
    tenant = await _lock_tenant(db, tenant_id)
    job = await _ensure_job(db, tenant)
    if job.status == "purging":
        raise TenantPurgeError("purge_in_progress", "A hold cannot be released during purge")
    result = await db.execute(
        select(TenantDeletionHold)
        .where(
            TenantDeletionHold.id == hold_id,
            TenantDeletionHold.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    hold = result.scalar_one_or_none()
    if hold is None:
        raise TenantPurgeError("tenant_purge_hold_not_found", "Purge hold not found", status_code=404)
    if hold.released_at is None:
        hold.released_at = current_time
        hold.released_by_identity_id = actor_identity_id
        hold.release_reason_code = normalized_reason
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                user_id=actor_user_id,
                action="tenant_purge_hold_released",
                details={
                    "tenant_id": str(tenant_id),
                    "hold_id": str(hold.id),
                    "hold_type": hold.hold_type,
                    "reason_code": normalized_reason,
                },
            )
        )
    remaining_holds = [item for item in await _active_holds(db, tenant_id) if item.id != hold.id]
    job.status = "held" if remaining_holds else "scheduled"
    job.plan_digest = None
    await db.commit()
    return {"id": str(hold.id), "tenant_id": str(tenant_id), "status": "released"}


async def list_tenant_purge_states(db: AsyncSession) -> dict[str, Any]:
    tenant_result = await db.execute(
        select(Tenant)
        .where(Tenant.deletion_requested_at.is_not(None))
        .order_by(Tenant.deletion_scheduled_for, Tenant.id)
    )
    items: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for tenant in tenant_result.scalars().all():
        job_result = await db.execute(
            select(TenantDeletionJob).where(TenantDeletionJob.tenant_id == tenant.id)
        )
        job = job_result.scalar_one_or_none()
        holds = await _active_holds(db, tenant.id)
        items.append(
            {
                "tenant_id": str(tenant.id),
                "tenant_name": tenant.name,
                "is_active": tenant.is_active,
                "deletion_requested_at": tenant.deletion_requested_at,
                "eligible_at": tenant.deletion_scheduled_for,
                "is_due": bool(tenant.deletion_scheduled_for and tenant.deletion_scheduled_for <= now),
                "job_status": job.status if job else "scheduled",
                "attempt_count": job.attempt_count if job else 0,
                "last_error_code": job.last_error_code if job else None,
                "plan_digest": job.plan_digest if job else None,
                "holds": [
                    {
                        "id": str(hold.id),
                        "hold_type": hold.hold_type,
                        "reason_code": hold.reason_code,
                        "created_at": hold.created_at,
                    }
                    for hold in holds
                ],
            }
        )
    tombstone_result = await db.execute(
        select(TenantDeletionTombstone)
        .order_by(TenantDeletionTombstone.purged_at.desc())
        .limit(100)
    )
    tombstones = [
        {
            "tenant_id": str(row.tenant_id),
            "purged_at": row.purged_at,
            "reason_code": row.reason_code,
            "receipt_hash": row.receipt_hash,
            "rows_total": sum(int(value) for value in row.table_counts.values()),
            "schema_version": row.schema_version,
        }
        for row in tombstone_result.scalars().all()
    ]
    return {"items": items, "tombstones": tombstones}


async def list_due_tenant_ids(db: AsyncSession, *, batch_size: int = 10) -> tuple[uuid.UUID, ...]:
    size = max(1, min(int(batch_size), 100))
    result = await db.execute(
        select(Tenant.id)
        .where(
            Tenant.is_active.is_(False),
            Tenant.deletion_requested_at.is_not(None),
            Tenant.deletion_scheduled_for.is_not(None),
            Tenant.deletion_scheduled_for <= datetime.now(UTC),
        )
        .order_by(Tenant.deletion_scheduled_for, Tenant.id)
        .with_for_update(skip_locked=True)
        .limit(size)
    )
    return tuple(result.scalars().all())


async def _remove_agent_containers(agent_ids: tuple[uuid.UUID, ...]) -> int:
    if not agent_ids:
        return 0
    async with async_session() as db:
        result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)).order_by(Agent.id))
        agents = list(result.scalars().all())
    removed = 0
    for agent in agents:
        if not await agent_manager.remove_container(agent):
            raise TenantPurgeError(
                "container_cleanup_unconfirmed",
                "Agent container removal could not be proven",
            )
        removed += 1
    return removed


async def _delete_storage_prefixes(prefixes: tuple[str, ...], *, storage=None) -> dict[str, int]:
    backend = storage or get_storage_backend()
    deleted = 0
    try:
        for prefix in prefixes:
            is_directory = await backend.is_dir(prefix)
            exists = is_directory or await backend.exists(prefix)
            if not exists:
                continue
            if is_directory:
                for _attempt in range(1000):
                    await backend.delete_tree(prefix)
                    if not await backend.is_dir(prefix):
                        break
                else:
                    raise TenantPurgeError(
                        "storage_cleanup_incomplete",
                        "Tenant storage remained after repeated delete attempts",
                    )
            else:
                await backend.delete(prefix)
            if await backend.is_dir(prefix) or await backend.exists(prefix):
                raise TenantPurgeError(
                    "storage_cleanup_unconfirmed",
                    "Tenant storage deletion could not be verified",
                )
            deleted += 1
    except TenantPurgeError:
        raise
    except Exception as exc:
        raise TenantPurgeError(
            "storage_cleanup_failed",
            "Tenant storage cleanup failed and database deletion was not started",
        ) from exc
    return {
        "prefixes_planned": len(prefixes),
        "prefixes_deleted": deleted,
        "prefixes_verified_absent": len(prefixes),
    }


def validate_local_purge_execution_target(tenant: Tenant) -> None:
    settings = get_settings()
    try:
        url = make_url(settings.DATABASE_URL)
    except Exception as exc:
        raise TenantPurgeError("purge_database_url_invalid", "Purge database URL is invalid") from exc
    environment = settings.ENVIRONMENT.strip().lower()
    host = (url.host or "").strip("[]").lower()
    database = (url.database or "").lower()
    if not settings.ALLOW_LOCAL_TENANT_PURGE:
        raise TenantPurgeError(
            "local_purge_disabled",
            "Physical tenant purge is disabled for this process",
            status_code=403,
        )
    if environment not in {"development", "test"}:
        raise TenantPurgeError(
            "local_purge_environment_required",
            "Physical tenant purge is restricted to development or test",
            status_code=403,
        )
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise TenantPurgeError(
            "loopback_database_required",
            "Physical tenant purge requires a loopback fixture database",
            status_code=403,
        )
    if not database.startswith("clawith_g11_purge_"):
        raise TenantPurgeError(
            "fixture_database_required",
            "Physical tenant purge requires a dedicated G11 fixture database",
            status_code=403,
        )
    if not tenant.slug.startswith("g11-purge-"):
        raise TenantPurgeError(
            "fixture_tenant_required",
            "Physical tenant purge requires an explicitly named G11 fixture tenant",
            status_code=403,
        )


async def _mark_failed(tenant_id: uuid.UUID, code: str) -> None:
    safe_code = _safe_reason_code(code.replace("_", "."))[:100].replace(".", "_")
    async with async_session() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        ).scalar_one_or_none()
        if tenant is None:
            await db.rollback()
            return
        if tenant.deletion_requested_at is None or tenant.deletion_scheduled_for is None:
            # A concurrent restore intentionally removes the purge job.  The
            # cancelled lifecycle wins; never recreate a failed job for it.
            await db.rollback()
            return
        job = (
            await db.execute(
                select(TenantDeletionJob)
                .where(TenantDeletionJob.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            await db.rollback()
            return
        job.status = "failed"
        job.last_error_code = safe_code
        job.last_error_at = datetime.now(UTC)
        job.started_at = None
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                action="tenant_purge_failed",
                details={"tenant_id": str(tenant_id), "error_code": safe_code},
            )
        )
        await db.commit()


async def execute_tenant_purge(tenant_id: uuid.UUID, *, storage=None) -> dict[str, Any]:
    """Execute one guarded local fixture purge after a persisted dry-run."""

    try:
        async with async_session() as db:
            tenant = await _lock_tenant(db, tenant_id)
            validate_local_purge_execution_target(tenant)
            _assert_eligible(tenant, datetime.now(UTC))
            job = await _ensure_job(db, tenant)
            if job.status == "purging" and job.started_at and job.started_at > datetime.now(UTC) - _RECENT_PURGE_LEASE:
                raise TenantPurgeError("purge_in_progress", "A tenant purge attempt is already in progress")
            if await _active_holds(db, tenant_id):
                job.status = "held"
                await db.commit()
                raise TenantPurgeError("tenant_purge_held", "An active hold blocks purge")
            if job.status != "dry_run_passed" or not job.plan_digest:
                raise TenantPurgeError(
                    "fresh_dry_run_required",
                    "A fresh successful dry-run is required before physical purge",
                )
            plan = await _build_plan(db, tenant_id, storage=storage)
            if plan.plan_digest != job.plan_digest:
                job.status = "failed"
                job.last_error_code = "purge_plan_changed"
                job.last_error_at = datetime.now(UTC)
                await db.commit()
                raise TenantPurgeError(
                    "purge_plan_changed",
                    "Tenant data or schema changed after dry-run; run dry-run again",
                )
            original_plan_digest = plan.plan_digest
            original_table_counts = dict(plan.table_counts)
            original_storage_summary = dict(plan.storage_summary)
            agent_ids = plan.agent_ids
            storage_prefixes = plan.storage_prefixes
            tenant_name = tenant.name
            deletion_requested_at = tenant.deletion_requested_at
            eligible_at = tenant.deletion_scheduled_for
            job.status = "purging"
            job.attempt_count += 1
            job.started_at = datetime.now(UTC)
            job.last_error_code = None
            job.last_error_at = None
            db.add(
                AuditLog(
                    tenant_id=tenant_id,
                    action="tenant_purge_started",
                    details={
                        "tenant_id": str(tenant_id),
                        "plan_digest": original_plan_digest,
                        "attempt": job.attempt_count,
                    },
                )
            )
            await db.commit()

        containers_removed = await _remove_agent_containers(agent_ids)
        storage_result = await _delete_storage_prefixes(storage_prefixes, storage=storage)

        async with async_session() as db:
            tenant = await _lock_tenant(db, tenant_id)
            validate_local_purge_execution_target(tenant)
            _assert_eligible(tenant, datetime.now(UTC))
            job = await _ensure_job(db, tenant)
            if job.status != "purging" or job.plan_digest != original_plan_digest:
                raise TenantPurgeError(
                    "purge_execution_lease_lost",
                    "The purge execution state changed before database deletion",
                )
            if await _active_holds(db, tenant_id):
                raise TenantPurgeError("tenant_purge_held", "A hold blocks database deletion")
            final_plan = await _build_plan(db, tenant_id, storage=storage)
            if final_plan.plan_digest != original_plan_digest:
                raise TenantPurgeError(
                    "purge_plan_changed",
                    "Tenant data or schema changed during external cleanup",
                )
            if {
                key: value
                for key, value in final_plan.table_counts.items()
                if key not in _DIGEST_VOLATILE_TABLES
            } != {
                key: value
                for key, value in original_table_counts.items()
                if key not in _DIGEST_VOLATILE_TABLES
            }:
                raise TenantPurgeError(
                    "purge_count_drift",
                    "Tenant row counts changed after dry-run",
                )

            purged_at = datetime.now(UTC)
            settings = get_settings()
            name_digest = _hmac_hex(
                settings.SECRET_KEY,
                "tenant-name-v1",
                {"tenant_id": str(tenant_id), "name": tenant_name.strip()},
            )
            tombstone_storage = {
                **original_storage_summary,
                **storage_result,
                "containers_removed": containers_removed,
            }
            receipt_payload = {
                "tenant_id": str(tenant_id),
                "name_digest": name_digest,
                "deletion_requested_at": deletion_requested_at.isoformat(),
                "eligible_at": eligible_at.isoformat(),
                "purged_at": purged_at.isoformat(),
                "reason_code": "retention_window_elapsed",
                "table_counts": final_plan.table_counts,
                "storage_summary": tombstone_storage,
                "plan_digest": original_plan_digest,
                "schema_version": _TOMBSTONE_SCHEMA_VERSION,
            }
            receipt_hash = _hmac_hex(settings.SECRET_KEY, "tenant-purge-receipt-v1", receipt_payload)

            deleted_counts = await final_plan.planner.delete_planned_rows()
            tombstone = TenantDeletionTombstone(
                tenant_id=tenant_id,
                name_digest=name_digest,
                deletion_requested_at=deletion_requested_at,
                eligible_at=eligible_at,
                purged_at=purged_at,
                reason_code="retention_window_elapsed",
                table_counts=deleted_counts,
                storage_summary=tombstone_storage,
                plan_digest=original_plan_digest,
                receipt_hash=receipt_hash,
                schema_version=_TOMBSTONE_SCHEMA_VERSION,
            )
            db.add(tombstone)
            db.add(
                AuditLog(
                    tenant_id=None,
                    user_id=None,
                    action="tenant_purge_completed",
                    details={
                        "tenant_id": str(tenant_id),
                        "receipt_hash": receipt_hash,
                        "rows_total": sum(deleted_counts.values()),
                        "tables": len(deleted_counts),
                        "storage_prefixes_deleted": storage_result["prefixes_deleted"],
                    },
                )
            )
            await db.commit()
            return {
                "tenant_id": str(tenant_id),
                "status": "purged",
                "receipt_hash": receipt_hash,
                "rows_total": sum(deleted_counts.values()),
                "tables": len(deleted_counts),
                "storage_summary": tombstone_storage,
            }
    except TenantPurgeError as exc:
        if exc.code == "tenant_already_purged":
            async with async_session() as db:
                tombstone = await db.get(TenantDeletionTombstone, tenant_id)
                if tombstone is not None:
                    return {
                        "tenant_id": str(tenant_id),
                        "status": "already_purged",
                        "receipt_hash": tombstone.receipt_hash,
                    }
        await _mark_failed(tenant_id, exc.code)
        raise
    except Exception as exc:
        await _mark_failed(tenant_id, "unexpected_purge_failure")
        raise TenantPurgeError(
            "unexpected_purge_failure",
            "Tenant purge failed safely; inspect server diagnostics before retrying",
        ) from exc
