"""Isolate A2A owners and serialize trigger execution per Agent.

Revision ID: trigger_privacy_serial
Revises: durable_media_completion
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "trigger_privacy_serial"
down_revision: str | Sequence[str] | None = "durable_media_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_names(table_name: str) -> set[str]:
    return {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name) if index.get("name")}


def _column_names(table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _foreign_key_name(
    table_name: str,
    columns: list[str],
    referred_table: str,
) -> str | None:
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name):
        if foreign_key.get("constrained_columns") == columns and foreign_key.get("referred_table") == referred_table:
            name = foreign_key.get("name")
            return str(name) if name else None
    return None


def _column_is_nullable(table_name: str, column_name: str) -> bool:
    for column in sa.inspect(op.get_bind()).get_columns(table_name):
        if column.get("name") == column_name:
            return bool(column.get("nullable"))
    raise RuntimeError(f"Missing expected column {table_name}.{column_name}")


def _ensure_agent_fk_set_null(
    table_name: str,
    column_name: str,
    constraint_name: str,
) -> None:
    """Preserve durable audit rows when an Agent is physically removed."""

    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name):
        if foreign_key.get("constrained_columns") == [column_name] and foreign_key.get("referred_table") == "agents":
            ondelete = str((foreign_key.get("options") or {}).get("ondelete") or "").upper()
            if ondelete == "SET NULL":
                return
            existing_name = foreign_key.get("name")
            if not existing_name:
                raise RuntimeError(f"Cannot safely replace unnamed foreign key {table_name}.{column_name}")
            op.drop_constraint(
                str(existing_name),
                table_name,
                type_="foreignkey",
            )
            break
    op.create_foreign_key(
        constraint_name,
        table_name,
        "agents",
        [column_name],
        ["id"],
        ondelete="SET NULL",
    )


def upgrade() -> None:
    # The historical bootstrap revision reflects current ORM metadata. A fresh
    # database can therefore already contain every object introduced here,
    # while an upgraded production database contains none of them. Inspect all
    # 099 DDL so both paths converge without duplicate columns, constraints, or
    # indexes.
    if "deletion_requested_at" not in _column_names("agents"):
        op.add_column(
            "agents",
            sa.Column(
                "deletion_requested_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if "ix_agents_deletion_requested_at" not in _index_names("agents"):
        op.create_index(
            "ix_agents_deletion_requested_at",
            "agents",
            ["deletion_requested_at"],
            unique=False,
        )

    # Agent deletion must never erase financial/external audit history.  The
    # production schema predates these SET NULL policies, while a fresh
    # bootstrap is built from current ORM metadata and may already have them.
    if not _column_is_nullable("douyin_publish_jobs", "agent_id"):
        op.alter_column(
            "douyin_publish_jobs",
            "agent_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
    if not _column_is_nullable("approval_requests", "agent_id"):
        op.alter_column(
            "approval_requests",
            "agent_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
    if not _column_is_nullable("media_generation_tasks", "agent_id"):
        op.alter_column(
            "media_generation_tasks",
            "agent_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
    for table_name, column_name, constraint_name in (
        (
            "audit_logs",
            "agent_id",
            "fk_audit_logs_agent_id_set_null",
        ),
        (
            "approval_requests",
            "agent_id",
            "fk_approval_requests_agent_id_set_null",
        ),
        (
            "credit_transactions",
            "agent_id",
            "fk_credit_transactions_agent_id_set_null",
        ),
        (
            "credit_reservations",
            "agent_id",
            "fk_credit_reservations_agent_id_set_null",
        ),
        (
            "douyin_accounts",
            "primary_agent_id",
            "fk_douyin_accounts_primary_agent_id_set_null",
        ),
        (
            "douyin_publish_jobs",
            "agent_id",
            "fk_douyin_publish_jobs_agent_id_set_null",
        ),
        (
            "douyin_operations",
            "agent_id",
            "fk_douyin_operations_agent_id_set_null",
        ),
        (
            "media_generation_tasks",
            "agent_id",
            "fk_media_generation_tasks_agent_id_set_null",
        ),
    ):
        _ensure_agent_fk_set_null(
            table_name,
            column_name,
            constraint_name,
        )
    if "fire_recorded_at" not in _column_names("trigger_executions"):
        op.add_column(
            "trigger_executions",
            sa.Column(
                "fire_recorded_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    # Pre-upgrade queueing recorded fire_count only when a worker claimed an
    # execution.  Pending rows are accepted work but were therefore absent
    # from the parent counter.  Reserve their capacity before marking every
    # historical execution as accounted for; keep once/max triggers enabled
    # until those accepted rows actually complete.
    op.execute(
        """
        WITH pending_counts AS (
            SELECT trigger_id,
                   count(*)::integer AS pending_count,
                   max(COALESCE(scheduled_at, created_at, now())) AS last_at
            FROM trigger_executions
            WHERE status = 'pending'
              AND fire_recorded_at IS NULL
            GROUP BY trigger_id
        )
        UPDATE agent_triggers AS trigger
        SET fire_count = COALESCE(trigger.fire_count, 0) + pending.pending_count,
            last_fired_at = GREATEST(trigger.last_fired_at, pending.last_at)
        FROM pending_counts AS pending
        WHERE trigger.id = pending.trigger_id
        """
    )
    op.execute("UPDATE trigger_executions SET fire_recorded_at = COALESCE(scheduled_at, created_at, now())")
    gateway_columns = _column_names("gateway_messages")
    if "delivery_lease_expires_at" not in gateway_columns:
        op.add_column(
            "gateway_messages",
            sa.Column(
                "delivery_lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if "delivery_attempts" not in gateway_columns:
        op.add_column(
            "gateway_messages",
            sa.Column(
                "delivery_attempts",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )
    if "authorization_source_agent_id" not in gateway_columns:
        op.add_column(
            "gateway_messages",
            sa.Column(
                "authorization_source_agent_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
    if "ix_gateway_messages_delivery_claim" not in _index_names("gateway_messages"):
        op.create_index(
            "ix_gateway_messages_delivery_claim",
            "gateway_messages",
            ["agent_id", "status", "delivery_lease_expires_at", "created_at"],
            unique=False,
            postgresql_where=sa.text("status IN ('pending', 'delivered')"),
        )
    if not _foreign_key_name("gateway_messages", ["authorization_source_agent_id"], "agents"):
        op.create_foreign_key(
            "fk_gateway_messages_authorization_source_agent",
            "gateway_messages",
            "agents",
            ["authorization_source_agent_id"],
            ["id"],
        )

    if "owner_user_id" not in _column_names("agent_credentials"):
        op.add_column(
            "agent_credentials",
            sa.Column(
                "owner_user_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
    if not _foreign_key_name("agent_credentials", ["owner_user_id"], "users"):
        op.create_foreign_key(
            "fk_agent_credentials_owner_user",
            "agent_credentials",
            "users",
            ["owner_user_id"],
            ["id"],
            ondelete="CASCADE",
        )
    credential_indexes = _index_names("agent_credentials")
    if "ix_agent_credentials_owner_user_id" not in credential_indexes:
        op.create_index(
            "ix_agent_credentials_owner_user_id",
            "agent_credentials",
            ["owner_user_id"],
            unique=False,
        )
    if "uq_agent_credentials_owned_platform" not in credential_indexes:
        op.create_index(
            "uq_agent_credentials_owned_platform",
            "agent_credentials",
            ["agent_id", "owner_user_id", "platform"],
            unique=True,
            postgresql_where=sa.text("owner_user_id IS NOT NULL"),
        )
    # Legacy shared cookies cannot be assigned to an owner safely. Purge the
    # encrypted payload, quarantine the row, and notify the Agent creator to
    # establish a new owner-scoped credential explicitly.
    op.execute(
        """
        WITH affected AS (
            UPDATE agent_credentials
            SET status = 'needs_relogin',
                cookies_json = NULL,
                cookies_updated_at = NULL,
                last_injected_at = NULL,
                updated_at = now()
            WHERE owner_user_id IS NULL
            RETURNING agent_id
        ), affected_agents AS (
            SELECT DISTINCT agent_id FROM affected
        )
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5('legacy-credential-quarantined:' || agent.id::text)::uuid,
            agent.creator_id,
            agent.id,
            'system',
            'Browser login needs to be re-established',
            'A legacy shared browser credential had no provable user owner. Its stored cookies were removed during the security upgrade; create a new owner-scoped credential before using automatic login.',
            '/agents/' || agent.id::text || '#credentials',
            agent.id,
            false
        FROM affected_agents AS affected
        JOIN agents AS agent ON agent.id = affected.agent_id
        ON CONFLICT (id) DO NOTHING
        """
    )

    # Relationship authorization is intentionally fail-closed when duplicate
    # rows exist. Collapse historical duplicates deterministically before
    # enforcing the same uniqueness that current replace-all writers expect.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                relationship.id,
                relationship.agent_id,
                relationship.member_id,
                row_number() OVER (
                    PARTITION BY relationship.agent_id, relationship.member_id
                    ORDER BY relationship.updated_at DESC NULLS LAST,
                             relationship.created_at DESC NULLS LAST,
                             relationship.id DESC
                ) AS row_number
            FROM agent_relationships AS relationship
        ), duplicate_pairs AS (
            SELECT DISTINCT agent_id, member_id
            FROM ranked
            WHERE row_number > 1
        )
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5(
                'dedup-human-relationship:' || pair.agent_id::text || ':' ||
                pair.member_id::text
            )::uuid,
            agent.creator_id,
            pair.agent_id,
            'system',
            'Duplicate relationship repaired',
            'Historical duplicate human relationship rows were consolidated during the security upgrade. Review this Agent relationship if its role or description matters.',
            '/agents/' || pair.agent_id::text || '#relationships',
            pair.member_id,
            false
        FROM duplicate_pairs AS pair
        JOIN agents AS agent ON agent.id = pair.agent_id
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                relationship.id,
                row_number() OVER (
                    PARTITION BY relationship.agent_id, relationship.member_id
                    ORDER BY relationship.updated_at DESC NULLS LAST,
                             relationship.created_at DESC NULLS LAST,
                             relationship.id DESC
                ) AS row_number
            FROM agent_relationships AS relationship
        )
        DELETE FROM agent_relationships AS relationship
        USING ranked
        WHERE relationship.id = ranked.id
          AND ranked.row_number > 1
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                relationship.id,
                relationship.agent_id,
                relationship.target_agent_id,
                row_number() OVER (
                    PARTITION BY relationship.agent_id,
                                 relationship.target_agent_id
                    ORDER BY relationship.updated_at DESC NULLS LAST,
                             relationship.created_at DESC NULLS LAST,
                             relationship.id DESC
                ) AS row_number
            FROM agent_agent_relationships AS relationship
        ), duplicate_pairs AS (
            SELECT DISTINCT agent_id, target_agent_id
            FROM ranked
            WHERE row_number > 1
        )
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5(
                'dedup-agent-relationship:' || pair.agent_id::text || ':' ||
                pair.target_agent_id::text
            )::uuid,
            agent.creator_id,
            pair.agent_id,
            'system',
            'Duplicate Agent relationship repaired',
            'Historical duplicate Agent relationship rows were consolidated during the security upgrade. Review this directed relationship if its role or description matters.',
            '/agents/' || pair.agent_id::text || '#relationships',
            pair.target_agent_id,
            false
        FROM duplicate_pairs AS pair
        JOIN agents AS agent ON agent.id = pair.agent_id
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                relationship.id,
                row_number() OVER (
                    PARTITION BY relationship.agent_id,
                                 relationship.target_agent_id
                    ORDER BY relationship.updated_at DESC NULLS LAST,
                             relationship.created_at DESC NULLS LAST,
                             relationship.id DESC
                ) AS row_number
            FROM agent_agent_relationships AS relationship
        )
        DELETE FROM agent_agent_relationships AS relationship
        USING ranked
        WHERE relationship.id = ranked.id
          AND ranked.row_number > 1
        """
    )
    if "uq_agent_relationship_agent_member" not in _index_names("agent_relationships"):
        op.create_index(
            "uq_agent_relationship_agent_member",
            "agent_relationships",
            ["agent_id", "member_id"],
            unique=True,
        )
    if "uq_agent_agent_relationship_pair" not in _index_names("agent_agent_relationships"):
        op.create_index(
            "uq_agent_agent_relationship_pair",
            "agent_agent_relationships",
            ["agent_id", "target_agent_id"],
            unique=True,
        )

    # Legacy code reused one A2A conversation for every company user and also
    # allowed the same pair in both directions. Drop a bootstrap-created copy
    # of the future index, canonicalize the pair, then create one private lane
    # for every owner actually recorded on a message.
    if "uq_chat_sessions_a2a_owner" in _index_names("chat_sessions"):
        op.drop_index("uq_chat_sessions_a2a_owner", table_name="chat_sessions")

    op.execute(
        """
        UPDATE chat_sessions
        SET agent_id = CASE
                WHEN agent_id::text <= peer_agent_id::text THEN agent_id
                ELSE peer_agent_id
            END,
            peer_agent_id = CASE
                WHEN agent_id::text <= peer_agent_id::text THEN peer_agent_id
                ELSE agent_id
            END
        WHERE source_channel = 'agent'
          AND peer_agent_id IS NOT NULL
        """
    )
    if "tenant_id" in _column_names("chat_sessions"):
        unified_insert_columns = ", tenant_id, session_type, updated_at"
        unified_insert_values = (
            ", owner_agent.tenant_id, 'a2a', COALESCE(needed.last_message_at, needed.created_at, now())"
        )
        unified_insert_join = "JOIN agents AS owner_agent ON owner_agent.id = needed.agent_id"
    else:
        unified_insert_columns = ""
        unified_insert_values = ""
        unified_insert_join = ""
    op.execute(
        f"""
        WITH durable_owners AS (
            SELECT
                session.agent_id,
                session.peer_agent_id,
                message.user_id,
                session.created_at,
                session.last_message_at
            FROM chat_sessions AS session
            JOIN chat_messages AS message
              ON message.conversation_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND message.user_id IS NOT NULL
            UNION ALL
            SELECT session.agent_id, session.peer_agent_id, task.user_id,
                   session.created_at, session.last_message_at
            FROM chat_sessions AS session
            JOIN media_generation_tasks AS task
              ON task.origin_session_id = session.id
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND task.user_id IS NOT NULL
            UNION ALL
            SELECT session.agent_id, session.peer_agent_id,
                   message.sender_user_id, session.created_at,
                   session.last_message_at
            FROM chat_sessions AS session
            JOIN gateway_messages AS message
              ON message.conversation_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND message.sender_user_id IS NOT NULL
            UNION ALL
            SELECT session.agent_id, session.peer_agent_id, lock.user_id,
                   session.created_at, session.last_message_at
            FROM chat_sessions AS session
            JOIN workspace_edit_locks AS lock
              ON lock.session_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND lock.user_id IS NOT NULL
            UNION ALL
            SELECT session.agent_id, session.peer_agent_id, revision.actor_id,
                   session.created_at, session.last_message_at
            FROM chat_sessions AS session
            JOIN workspace_file_revisions AS revision
              ON revision.session_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND revision.actor_type = 'user'
              AND revision.actor_id IS NOT NULL
            UNION ALL
            SELECT session.agent_id, session.peer_agent_id, owner.id,
                   session.created_at, session.last_message_at
            FROM agent_triggers AS trigger
            JOIN users AS owner
              ON owner.id::text = trigger.config ->> '_origin_user_id'
            JOIN LATERAL (
                VALUES
                    (trigger.config ->> 'expected_conversation_id'),
                    (trigger.config ->> '_origin_session_id')
            ) AS reference(session_id) ON true
            JOIN chat_sessions AS session
              ON session.id::text = reference.session_id
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
            UNION ALL
            SELECT session.agent_id, session.peer_agent_id, owner.id,
                   session.created_at, session.last_message_at
            FROM trigger_executions AS execution
            JOIN users AS owner
              ON owner.id::text = execution.payload ->> '_origin_user_id'
            JOIN LATERAL (
                VALUES
                    (execution.payload ->> '_a2a_session_id'),
                    (execution.payload ->> '_origin_session_id'),
                    (execution.payload ->> '_matched_conversation_id')
            ) AS reference(session_id) ON true
            JOIN chat_sessions AS session
              ON session.id::text = reference.session_id
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
            UNION ALL
            SELECT session.agent_id, session.peer_agent_id,
                   source_message.user_id, session.created_at,
                   session.last_message_at
            FROM trigger_executions AS execution
            JOIN chat_messages AS source_message
              ON source_message.id::text =
                    execution.payload ->> '_source_message_id'
            JOIN chat_sessions AS session
              ON session.id::text = execution.payload ->> '_a2a_session_id'
             AND source_message.conversation_id = session.id::text
            WHERE execution.source = 'a2a'
              AND source_message.user_id IS NOT NULL
              AND session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
        ), needed AS (
            SELECT agent_id, peer_agent_id, user_id,
                   min(created_at) AS created_at,
                   max(last_message_at) AS last_message_at
            FROM durable_owners
            GROUP BY agent_id, peer_agent_id, user_id
        )
        INSERT INTO chat_sessions (
            id, agent_id, peer_agent_id, user_id, title, source_channel,
            is_group, is_primary, created_at, last_message_at
            {unified_insert_columns}
        )
        SELECT
            md5(
                'a2a-owner-v1:' || needed.agent_id::text || ':' ||
                needed.peer_agent_id::text || ':' || needed.user_id::text
            )::uuid,
            needed.agent_id,
            needed.peer_agent_id,
            needed.user_id,
            'Recovered private A2A conversation',
            'agent',
            false,
            false,
            needed.created_at,
            needed.last_message_at
            {unified_insert_values}
        FROM needed
        {unified_insert_join}
        WHERE NOT EXISTS (
            SELECT 1
            FROM chat_sessions AS existing
            WHERE existing.source_channel = 'agent'
              AND existing.agent_id = needed.agent_id
              AND existing.peer_agent_id = needed.peer_agent_id
              AND existing.user_id = needed.user_id
        )
        ON CONFLICT DO NOTHING
        """
    )

    # Route every durable object with an explicit user to that user's lane
    # before the remaining duplicate sessions are consolidated.
    owner_session_cte = """
        WITH owner_sessions AS (
            SELECT DISTINCT ON (agent_id, peer_agent_id, user_id)
                id, agent_id, peer_agent_id, user_id
            FROM chat_sessions
            WHERE source_channel = 'agent'
              AND peer_agent_id IS NOT NULL
            ORDER BY agent_id, peer_agent_id, user_id,
                     created_at NULLS LAST, id
        )
    """
    op.execute(
        owner_session_cte
        + """
        UPDATE chat_messages AS message
        SET conversation_id = owner.id::text,
            agent_id = owner.agent_id
        FROM chat_sessions AS source, owner_sessions AS owner
        WHERE message.conversation_id = source.id::text
          AND source.source_channel = 'agent'
          AND source.peer_agent_id IS NOT NULL
          AND owner.agent_id = source.agent_id
          AND owner.peer_agent_id = source.peer_agent_id
          AND owner.user_id = message.user_id
        """
    )
    op.execute(
        owner_session_cte
        + """
        UPDATE media_generation_tasks AS task
        SET origin_session_id = owner.id
        FROM chat_sessions AS source, owner_sessions AS owner
        WHERE task.origin_session_id = source.id
          AND task.user_id IS NOT NULL
          AND source.source_channel = 'agent'
          AND source.peer_agent_id IS NOT NULL
          AND owner.agent_id = source.agent_id
          AND owner.peer_agent_id = source.peer_agent_id
          AND owner.user_id = task.user_id
        """
    )
    op.execute(
        owner_session_cte
        + """
        UPDATE gateway_messages AS message
        SET conversation_id = owner.id::text
        FROM chat_sessions AS source, owner_sessions AS owner
        WHERE message.conversation_id = source.id::text
          AND message.sender_user_id IS NOT NULL
          AND source.source_channel = 'agent'
          AND source.peer_agent_id IS NOT NULL
          AND owner.agent_id = source.agent_id
          AND owner.peer_agent_id = source.peer_agent_id
          AND owner.user_id = message.sender_user_id
        """
    )
    op.execute(
        owner_session_cte
        + """
        UPDATE workspace_edit_locks AS lock
        SET session_id = owner.id::text
        FROM chat_sessions AS source, owner_sessions AS owner
        WHERE lock.session_id = source.id::text
          AND source.source_channel = 'agent'
          AND source.peer_agent_id IS NOT NULL
          AND owner.agent_id = source.agent_id
          AND owner.peer_agent_id = source.peer_agent_id
          AND owner.user_id = lock.user_id
        """
    )
    op.execute(
        owner_session_cte
        + """
        UPDATE workspace_file_revisions AS revision
        SET session_id = owner.id::text
        FROM chat_sessions AS source, owner_sessions AS owner
        WHERE revision.session_id = source.id::text
          AND revision.actor_type = 'user'
          AND revision.actor_id IS NOT NULL
          AND source.source_channel = 'agent'
          AND source.peer_agent_id IS NOT NULL
          AND owner.agent_id = source.agent_id
          AND owner.peer_agent_id = source.peer_agent_id
          AND owner.user_id = revision.actor_id
        """
    )
    for key in ("expected_conversation_id", "_origin_session_id"):
        op.execute(
            owner_session_cte
            + f"""
            UPDATE agent_triggers AS trigger
            SET config = jsonb_set(
                trigger.config,
                '{{{key}}}',
                to_jsonb(owner.id::text),
                false
            )
            FROM chat_sessions AS source, owner_sessions AS owner
            WHERE trigger.config ->> '{key}' = source.id::text
              AND trigger.config ->> '_origin_user_id' = owner.user_id::text
              AND source.source_channel = 'agent'
              AND source.peer_agent_id IS NOT NULL
              AND owner.agent_id = source.agent_id
              AND owner.peer_agent_id = source.peer_agent_id
            """
        )
    for key in (
        "_a2a_session_id",
        "_origin_session_id",
        "_matched_conversation_id",
    ):
        op.execute(
            owner_session_cte
            + f"""
            UPDATE trigger_executions AS execution
            SET payload = jsonb_set(
                execution.payload,
                '{{{key}}}',
                to_jsonb(owner.id::text),
                false
            )
            FROM chat_sessions AS source, owner_sessions AS owner
            WHERE execution.payload ->> '{key}' = source.id::text
              AND execution.payload ->> '_origin_user_id' = owner.user_id::text
              AND source.source_channel = 'agent'
              AND source.peer_agent_id IS NOT NULL
              AND owner.agent_id = source.agent_id
              AND owner.peer_agent_id = source.peer_agent_id
            """
        )
    for key in (
        "_a2a_session_id",
        "_origin_session_id",
        "_matched_conversation_id",
    ):
        op.execute(
            owner_session_cte
            + f"""
            UPDATE trigger_executions AS execution
            SET payload = jsonb_set(
                execution.payload,
                '{{{key}}}',
                to_jsonb(owner.id::text),
                false
            )
            FROM chat_messages AS source_message,
                 chat_sessions AS source,
                 owner_sessions AS owner
            WHERE execution.source = 'a2a'
              AND execution.payload ->> '_source_message_id' =
                    source_message.id::text
              AND execution.payload ->> '{key}' = source.id::text
              AND source.source_channel = 'agent'
              AND source.peer_agent_id IS NOT NULL
              AND owner.agent_id = source.agent_id
              AND owner.peer_agent_id = source.peer_agent_id
              AND owner.user_id = source_message.user_id
            """
        )

    # Materialize the final old->keeper map once so every remaining reference
    # moves atomically before any duplicate row is deleted.
    op.execute(
        """
        CREATE TEMP TABLE _a2a_session_remap ON COMMIT DROP AS
        SELECT id AS old_id,
               first_value(id) OVER (
                   PARTITION BY agent_id, peer_agent_id, user_id
                   ORDER BY created_at NULLS LAST, id
               ) AS keep_id
        FROM chat_sessions
        WHERE source_channel = 'agent'
          AND peer_agent_id IS NOT NULL
        """
    )
    # Approval payload v3 signs the exact origin session. Rewriting that field
    # would invalidate the HMAC while leaving an apparently executable row.
    # Resolve pending approvals and terminalize approved work instead, then ask
    # the requester to resubmit from the recovered owner lane.
    op.execute(
        """
        CREATE TEMP TABLE _migrated_approval_origins ON COMMIT DROP AS
        SELECT approval.id,
               approval.agent_id,
               COALESCE(requester.id, agent.creator_id) AS notify_user_id,
               approval.status,
               approval.execution_status
        FROM approval_requests AS approval
        JOIN _a2a_session_remap AS remap
          ON approval.details ->> 'origin_session_id' = remap.old_id::text
         AND remap.old_id <> remap.keep_id
        JOIN agents AS agent ON agent.id = approval.agent_id
        LEFT JOIN users AS requester
          ON requester.id::text = approval.details ->> 'requested_by'
        WHERE approval.status = 'pending'
           OR (
               approval.status = 'approved'
               AND approval.execution_status IN (
                   'legacy', 'pending', 'executing'
               )
           )
        """
    )
    op.execute(
        """
        UPDATE approval_requests AS approval
        SET status = 'rejected',
            resolved_at = now(),
            execution_status = 'not_required',
            execution_error_code = 'OriginSessionMigrated',
            execution_result_summary = '{"reason":"origin_session_migrated"}'::json
        FROM _migrated_approval_origins AS affected
        WHERE approval.id = affected.id
          AND affected.status = 'pending'
        """
    )
    op.execute(
        """
        UPDATE approval_requests AS approval
        SET execution_status = CASE
                WHEN affected.execution_status = 'executing'
                    THEN 'ambiguous'
                ELSE 'failed'
            END,
            execution_claim_token = COALESCE(
                approval.execution_claim_token,
                md5('approval-migration-claim:' || approval.id::text)::uuid
            ),
            execution_claimed_at = COALESCE(
                approval.execution_claimed_at,
                now()
            ),
            execution_finished_at = now(),
            execution_attempts = 1,
            execution_error_code = 'OriginSessionMigrated',
            execution_result_summary = '{"reason":"origin_session_migrated"}'::json
        FROM _migrated_approval_origins AS affected
        WHERE approval.id = affected.id
          AND affected.status = 'approved'
        """
    )
    op.execute(
        """
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5('approval-origin-migrated:' || affected.id::text)::uuid,
            affected.notify_user_id,
            affected.agent_id,
            'system',
            'Approval cancelled after conversation recovery',
            'The original private conversation was recovered during a security upgrade. Review the action and submit a new approval from the intended conversation.',
            '/agents/' || affected.agent_id::text || '#approvals',
            affected.id,
            false
        FROM _migrated_approval_origins AS affected
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE chat_messages AS message
        SET conversation_id = remap.keep_id::text
        FROM _a2a_session_remap AS remap
        WHERE remap.old_id <> remap.keep_id
          AND message.conversation_id = remap.old_id::text
        """
    )
    op.execute(
        """
        UPDATE media_generation_tasks AS task
        SET origin_session_id = remap.keep_id
        FROM _a2a_session_remap AS remap
        WHERE remap.old_id <> remap.keep_id
          AND task.origin_session_id = remap.old_id
        """
    )
    for table_name, column_name in (
        ("gateway_messages", "conversation_id"),
        ("workspace_file_revisions", "session_id"),
        ("workspace_edit_locks", "session_id"),
    ):
        op.execute(
            f"""
            UPDATE {table_name} AS target
            SET {column_name} = remap.keep_id::text
            FROM _a2a_session_remap AS remap
            WHERE remap.old_id <> remap.keep_id
              AND target.{column_name} = remap.old_id::text
            """
        )
    for table_name, json_column, keys in (
        (
            "agent_triggers",
            "config",
            ("expected_conversation_id", "_origin_session_id"),
        ),
        (
            "trigger_executions",
            "payload",
            (
                "_a2a_session_id",
                "_origin_session_id",
                "_matched_conversation_id",
            ),
        ),
    ):
        for key in keys:
            op.execute(
                f"""
                UPDATE {table_name} AS target
                SET {json_column} = jsonb_set(
                    target.{json_column},
                    '{{{key}}}',
                    to_jsonb(remap.keep_id::text),
                    false
                )
                FROM _a2a_session_remap AS remap
                WHERE remap.old_id <> remap.keep_id
                  AND target.{json_column} ->> '{key}' = remap.old_id::text
                """
            )
    op.execute(
        """
        DELETE FROM chat_sessions AS session
        USING _a2a_session_remap AS remap
        WHERE remap.old_id <> remap.keep_id
          AND session.id = remap.old_id
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM chat_messages AS message
            JOIN chat_sessions AS session
              ON message.conversation_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND message.user_id <> session.user_id
          ) THEN
            RAISE EXCEPTION 'A2A message owner partition failed';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM media_generation_tasks AS task
            JOIN chat_sessions AS session ON session.id = task.origin_session_id
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND task.user_id IS DISTINCT FROM session.user_id
          ) THEN
            RAISE EXCEPTION 'A2A media task owner partition failed';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM gateway_messages AS message
            JOIN chat_sessions AS session
              ON message.conversation_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND message.sender_user_id IS NOT NULL
              AND message.sender_user_id IS DISTINCT FROM session.user_id
          ) THEN
            RAISE EXCEPTION 'A2A gateway owner partition failed';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM workspace_edit_locks AS lock
            JOIN chat_sessions AS session ON lock.session_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND lock.user_id IS DISTINCT FROM session.user_id
          ) THEN
            RAISE EXCEPTION 'A2A workspace lock owner partition failed';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM workspace_file_revisions AS revision
            JOIN chat_sessions AS session
              ON revision.session_id = session.id::text
            WHERE session.source_channel = 'agent'
              AND session.peer_agent_id IS NOT NULL
              AND revision.actor_type = 'user'
              AND revision.actor_id IS DISTINCT FROM session.user_id
          ) THEN
            RAISE EXCEPTION 'A2A workspace revision owner partition failed';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM agent_triggers AS trigger
            JOIN LATERAL (
                VALUES
                    (trigger.config ->> 'expected_conversation_id'),
                    (trigger.config ->> '_origin_session_id')
            ) AS reference(session_id) ON true
            JOIN chat_sessions AS session
              ON session.id::text = reference.session_id
            WHERE session.source_channel = 'agent'
              AND trigger.config ? '_origin_user_id'
              AND trigger.config ->> '_origin_user_id' <>
                    session.user_id::text
          ) THEN
            RAISE EXCEPTION 'A2A trigger owner partition failed';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM trigger_executions AS execution
            JOIN LATERAL (
                VALUES
                    (execution.payload ->> '_a2a_session_id'),
                    (execution.payload ->> '_origin_session_id'),
                    (execution.payload ->> '_matched_conversation_id')
            ) AS reference(session_id) ON true
            JOIN chat_sessions AS session
              ON session.id::text = reference.session_id
            WHERE session.source_channel = 'agent'
              AND execution.payload ? '_origin_user_id'
              AND execution.payload ->> '_origin_user_id' <>
                    session.user_id::text
          ) THEN
            RAISE EXCEPTION 'A2A execution owner partition failed';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM trigger_executions AS execution
            JOIN chat_messages AS source_message
              ON source_message.id::text =
                    execution.payload ->> '_source_message_id'
            JOIN chat_sessions AS session
              ON session.id::text = execution.payload ->> '_a2a_session_id'
            WHERE execution.source = 'a2a'
              AND source_message.user_id IS DISTINCT FROM session.user_id
          ) THEN
            RAISE EXCEPTION 'A2A source-message owner partition failed';
          END IF;
        END $$
        """
    )

    # Legacy active Gateway rows cannot prove either the owner or the original
    # directed relationship. Never guess: quarantine them and require resend.
    op.execute(
        """
        WITH quarantined AS (
            UPDATE gateway_messages AS message
            SET status = 'revoked',
                result = 'Legacy Gateway authorization quarantined by migration',
                completed_at = now()
            WHERE message.status IN ('pending', 'delivered')
              AND (
                  message.sender_user_id IS NULL
                  OR message.sender_agent_id IS NOT NULL
              )
            RETURNING message.id, message.agent_id
        )
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5('gateway-auth-quarantined:' || quarantined.id::text)::uuid,
            agent.creator_id,
            quarantined.agent_id,
            'system',
            'Queued Agent message cancelled after security upgrade',
            'A queued message did not contain a provable owner and relationship binding. It was not delivered; resend it from the intended conversation.',
            '/agents/' || quarantined.agent_id::text,
            quarantined.id,
            false
        FROM quarantined
        JOIN agents AS agent ON agent.id = quarantined.agent_id
        ON CONFLICT (id) DO NOTHING
        """
    )

    if "uq_chat_sessions_a2a_owner" not in _index_names("chat_sessions"):
        op.create_index(
            "uq_chat_sessions_a2a_owner",
            "chat_sessions",
            ["agent_id", "peer_agent_id", "user_id"],
            unique=True,
            postgresql_where=sa.text("source_channel = 'agent' AND peer_agent_id IS NOT NULL"),
        )

    # The ledger existed before it became a runtime source of truth, so legacy
    # rows have no server-attested binding. Never attach them. Keep an explicit
    # cleanup-required state so operators can delete the remote provider UUIDs.
    op.execute(
        """
        UPDATE agentbay_session_ledger
        SET status = 'cleanup_required',
            close_reason = 'untrusted_legacy_binding',
            error_message = 'Operator provider cleanup required',
            updated_at = now()
        WHERE status = 'active'
        """
    )
    # New runtime rows use a canonical UUID string and binding_version=2. The
    # unique index prevents concurrent API/worker creators after cutover.
    op.execute(
        """
        UPDATE agentbay_session_ledger
        SET chat_session_id = trim(both '{}' from chat_session_id)::uuid::text,
            updated_at = now()
        WHERE chat_session_id IS NOT NULL
          AND trim(both '{}' from chat_session_id) ~*
              '^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$'
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY agent_id, user_id, chat_session_id, image_type
                    ORDER BY last_used_at DESC NULLS LAST,
                             started_at DESC NULLS LAST,
                             id
                ) AS row_number
            FROM agentbay_session_ledger
            WHERE status = 'active'
              AND agent_id IS NOT NULL
              AND user_id IS NOT NULL
              AND chat_session_id IS NOT NULL
        )
        UPDATE agentbay_session_ledger AS ledger
        SET status = 'closed',
            close_reason = 'duplicate_active_lane_quarantined',
            closed_at = now(),
            updated_at = now()
        FROM ranked
        WHERE ledger.id = ranked.id
          AND ranked.row_number > 1
        """
    )
    if "uq_agentbay_active_user_chat_image" not in _index_names("agentbay_session_ledger"):
        op.create_index(
            "uq_agentbay_active_user_chat_image",
            "agentbay_session_ledger",
            ["agent_id", "user_id", "chat_session_id", "image_type"],
            unique=True,
            postgresql_where=sa.text(
                "status = 'active' AND agent_id IS NOT NULL AND user_id IS NOT NULL AND chat_session_id IS NOT NULL"
            ),
        )

    # A processing row may be newer than an already-pending row. Keep a lease
    # only when that row is the global unfinished head for its Agent.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                status,
                row_number() OVER (
                    PARTITION BY agent_id
                    ORDER BY scheduled_at, id
                ) AS row_number
            FROM trigger_executions
            WHERE status IN ('pending', 'processing')
        )
        UPDATE trigger_executions AS execution
        SET status = 'pending',
            started_at = NULL,
            finished_at = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error = 'Requeued by per-Agent serialization migration'
        FROM ranked
        WHERE execution.id = ranked.id
          AND execution.status = 'processing'
          AND ranked.row_number > 1
        """
    )

    if "uq_trigger_executions_processing_agent" not in _index_names("trigger_executions"):
        op.create_index(
            "uq_trigger_executions_processing_agent",
            "trigger_executions",
            ["agent_id"],
            unique=True,
            postgresql_where=sa.text("status = 'processing'"),
        )

    # An enabled legacy trigger with routing or matched-message metadata would
    # continue spending Credits after that untrusted destination is removed.
    # Disable it and notify the Agent creator so it can be recreated in a
    # current, server-attested session.
    op.execute(
        """
        WITH disabled AS (
            UPDATE agent_triggers AS trigger
            SET is_enabled = false
            WHERE trigger.is_enabled = true
              AND trigger.type <> 'a2a'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_object_keys(trigger.config) AS reserved(key)
                  WHERE reserved.key LIKE '\\_%' ESCAPE '\\'
                    AND reserved.key NOT IN ('_since_ts', '_last_value')
              )
            RETURNING trigger.id, trigger.agent_id, trigger.name
        )
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5('disabled-legacy-trigger:' || disabled.id::text)::uuid,
            agent.creator_id,
            disabled.agent_id,
            'system',
            'Automation paused after security upgrade',
            'The automation "' || disabled.name ||
                '" used legacy delivery metadata and was paused to prevent hidden runs. Open the intended conversation and recreate the automation.',
            '/agents/' || disabled.agent_id::text || '#triggers',
            disabled.id,
            false
        FROM disabled
        JOIN agents AS agent ON agent.id = disabled.agent_id
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        WITH disabled AS (
            UPDATE agent_triggers AS trigger
            SET is_enabled = false
            WHERE trigger.is_enabled = true
              AND trigger.type = 'on_message'
              AND (
                  (
                      trigger.config ? 'from_agent_name'
                      AND (
                          NOT trigger.config ? 'from_agent_id'
                          OR NOT trigger.config ? 'expected_conversation_id'
                      )
                  )
                  OR (
                      trigger.config ? 'from_user_name'
                      AND (
                          NOT trigger.config ? '_watched_session_id'
                          OR NOT trigger.config ? '_watched_participant_id'
                      )
                  )
                  OR (
                      (trigger.config ? 'from_agent_name') =
                      (trigger.config ? 'from_user_name')
                  )
              )
            RETURNING trigger.id, trigger.agent_id, trigger.name
        )
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5('disabled-unbound-message-trigger:' || disabled.id::text)::uuid,
            agent.creator_id,
            disabled.agent_id,
            'system',
            'Message automation needs rebinding',
            'The automation "' || disabled.name ||
                '" did not contain a provable participant and conversation binding. It was paused; recreate it from the intended conversation.',
            '/agents/' || disabled.agent_id::text || '#triggers',
            disabled.id,
            false
        FROM disabled
        JOIN agents AS agent ON agent.id = disabled.agent_id
        ON CONFLICT (id) DO NOTHING
        """
    )

    # No legacy row has a provenance column that proves who supplied its
    # underscore-prefixed metadata. Structural validity or a guessed version
    # marker is insufficient. Keep only evaluator cursors, whose values cannot
    # route output or inject prompt content. Current server code mints fresh
    # delivery context only when a trigger is created from a live session.
    op.execute(
        """
        UPDATE agent_triggers AS trigger
        SET config = COALESCE(
            (
                SELECT jsonb_object_agg(item.key, item.value)
                FROM jsonb_each(trigger.config) AS item(key, value)
                WHERE item.key NOT LIKE '\\_%' ESCAPE '\\'
                   OR item.key IN ('_since_ts', '_last_value')
            ),
            '{}'::jsonb
        )
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_object_keys(trigger.config) AS reserved(key)
            WHERE reserved.key LIKE '\\_%' ESCAPE '\\'
        )
        """
    )

    # Pending non-A2A rows produced by older code may already contain copied
    # routing/message fields with no provenance marker. Fail them closed; the
    # base trigger remains available for a future clean evaluation.
    op.execute(
        """
        UPDATE trigger_executions AS execution
        SET status = 'failed',
            finished_at = now(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error = 'Untrusted legacy runtime payload quarantined by migration'
        WHERE execution.status IN ('pending', 'processing')
          AND execution.source <> 'a2a'
          AND EXISTS (
              SELECT 1
              FROM jsonb_object_keys(execution.payload) AS reserved(key)
              WHERE reserved.key LIKE '\\_%' ESCAPE '\\'
          )
        """
    )

    # RC5 keeps every automatic execution lane fail-closed at runtime until a
    # durable requester/session/generation intent worker is available.  The
    # release gate is deliberately separate from each user's ``is_enabled``
    # intent: never destroy which automations should resume in a future safe
    # release merely because this worker version is paused.
    op.execute(
        """
        WITH affected AS (
            SELECT DISTINCT agent_id
            FROM agent_triggers
            WHERE is_enabled = true
        )
        INSERT INTO notifications (
            id, user_id, agent_id, type, title, body, link, ref_id, is_read
        )
        SELECT
            md5('automatic-triggers-paused:' || affected.agent_id::text)::uuid,
            agent.creator_id,
            affected.agent_id,
            'system',
            'Automatic triggers paused for safety review',
            'Trigger configuration was retained, but automatic execution is paused in this release. No trigger will spend Credits until the secure execution worker is enabled in a later release.',
            '/agents/' || affected.agent_id::text || '#triggers',
            affected.agent_id,
            false
        FROM affected
        JOIN agents AS agent ON agent.id = affected.agent_id
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE trigger_executions
        SET status = 'failed',
            finished_at = now(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error = 'Automatic trigger execution paused by RC5 release policy'
        WHERE status IN ('pending', 'processing')
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET status = 'pending'
        WHERE status = 'doing'
          AND type IN ('todo', 'supervision')
        """
    )
    op.execute(
        """
        UPDATE approval_requests
        SET execution_status = 'ambiguous',
            execution_finished_at = now(),
            execution_error_code = 'ExecutionPausedDuringUpgrade',
            execution_result_summary = '{"reason":"execution_paused_during_upgrade"}'::json
        WHERE status = 'approved'
          AND execution_status = 'executing'
        """
    )
    op.execute(
        """
        UPDATE trigger_executions AS execution
        SET status = 'failed',
            finished_at = now(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error = 'Unprovable legacy A2A owner lane quarantined by migration'
        WHERE execution.status IN ('pending', 'processing')
          AND execution.source = 'a2a'
          AND NOT EXISTS (
              SELECT 1
              FROM chat_messages AS source_message
              JOIN chat_sessions AS session
                ON session.id::text = execution.payload ->> '_a2a_session_id'
              WHERE source_message.id::text =
                        execution.payload ->> '_source_message_id'
                AND source_message.conversation_id = session.id::text
                AND source_message.user_id = session.user_id
                AND session.source_channel = 'agent'
                AND session.peer_agent_id IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    # Data consolidation and untrusted-payload quarantine are intentionally
    # irreversible. Rolling back code must not restore unsafe routing values.
    orphaned_media_tasks = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM media_generation_tasks WHERE agent_id IS NULL"))
        .scalar_one()
    )
    if orphaned_media_tasks:
        raise RuntimeError("099 is forward-only after an Agent deletion preserved media audit rows")
    media_agent_fk = _foreign_key_name("media_generation_tasks", ["agent_id"], "agents")
    if media_agent_fk:
        op.drop_constraint(
            media_agent_fk,
            "media_generation_tasks",
            type_="foreignkey",
        )
    op.alter_column(
        "media_generation_tasks",
        "agent_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_media_generation_tasks_agent_id_cascade",
        "media_generation_tasks",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_column("trigger_executions", "fire_recorded_at")

    op.drop_index(
        "uq_agent_credentials_owned_platform",
        table_name="agent_credentials",
    )
    op.drop_index(
        "ix_agent_credentials_owner_user_id",
        table_name="agent_credentials",
    )
    credential_owner_fk = _foreign_key_name("agent_credentials", ["owner_user_id"], "users")
    if credential_owner_fk:
        op.drop_constraint(
            credential_owner_fk,
            "agent_credentials",
            type_="foreignkey",
        )
    op.drop_column("agent_credentials", "owner_user_id")

    index_names = _index_names("gateway_messages")
    if "ix_gateway_messages_delivery_claim" in index_names:
        op.drop_index(
            "ix_gateway_messages_delivery_claim",
            table_name="gateway_messages",
        )
    op.drop_column("gateway_messages", "delivery_attempts")
    op.drop_column("gateway_messages", "delivery_lease_expires_at")

    index_names = _index_names("agentbay_session_ledger")
    if "uq_agentbay_active_user_chat_image" in index_names:
        op.drop_index(
            "uq_agentbay_active_user_chat_image",
            table_name="agentbay_session_ledger",
        )

    index_names = _index_names("trigger_executions")
    if "uq_trigger_executions_processing_agent" in index_names:
        op.drop_index(
            "uq_trigger_executions_processing_agent",
            table_name="trigger_executions",
        )

    index_names = _index_names("chat_sessions")
    if "uq_chat_sessions_a2a_owner" in index_names:
        op.drop_index("uq_chat_sessions_a2a_owner", table_name="chat_sessions")

    index_names = _index_names("agent_agent_relationships")
    if "uq_agent_agent_relationship_pair" in index_names:
        op.drop_index(
            "uq_agent_agent_relationship_pair",
            table_name="agent_agent_relationships",
        )

    index_names = _index_names("agent_relationships")
    if "uq_agent_relationship_agent_member" in index_names:
        op.drop_index(
            "uq_agent_relationship_agent_member",
            table_name="agent_relationships",
        )

    gateway_source_fk = _foreign_key_name("gateway_messages", ["authorization_source_agent_id"], "agents")
    if gateway_source_fk:
        op.drop_constraint(
            gateway_source_fk,
            "gateway_messages",
            type_="foreignkey",
        )
    op.drop_column("gateway_messages", "authorization_source_agent_id")

    op.drop_index("ix_agents_deletion_requested_at", table_name="agents")
    op.drop_column("agents", "deletion_requested_at")
