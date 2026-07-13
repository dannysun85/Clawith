"""Add Douyin official OpenAPI account and operation tables.

Revision ID: add_douyin_agent_tables
Revises: reconcile_billable_agent_seats
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_douyin_agent_tables"
down_revision: Union[str, Sequence[str], None] = "reconcile_billable_agent_seats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    required_tables = {
        "douyin_oauth_states", "douyin_accounts", "douyin_tokens",
        "douyin_publish_jobs", "douyin_metric_snapshots", "douyin_comments",
        "douyin_operations",
    }
    if required_tables.issubset(set(sa.inspect(op.get_bind()).get_table_names())):
        return

    op.create_table(
        "douyin_oauth_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("state", sa.String(length=160), nullable=False),
        sa.Column("scopes", postgresql.JSON(), nullable=False),
        sa.Column("redirect_after", sa.String(length=500), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("state"),
    )
    op.create_index("ix_douyin_oauth_states_state", "douyin_oauth_states", ["state"])
    op.create_index("ix_douyin_oauth_states_tenant_id", "douyin_oauth_states", ["tenant_id"])
    op.create_index("ix_douyin_oauth_states_user_id", "douyin_oauth_states", ["user_id"])
    op.create_index("ix_douyin_oauth_states_expires_at", "douyin_oauth_states", ["expires_at"])

    op.create_table(
        "douyin_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("primary_agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column("union_id", sa.String(length=128), nullable=True),
        sa.Column("nickname", sa.String(length=200), nullable=True),
        sa.Column("avatar_url", sa.String(length=500), nullable=True),
        sa.Column("account_type", sa.String(length=50), nullable=True),
        sa.Column("scopes", postgresql.JSON(), nullable=False),
        sa.Column("permission_status", postgresql.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "open_id", name="uq_douyin_account_tenant_open_id"),
    )
    op.create_index("ix_douyin_accounts_tenant_id", "douyin_accounts", ["tenant_id"])
    op.create_index("ix_douyin_accounts_primary_agent_id", "douyin_accounts", ["primary_agent_id"])
    op.create_index("ix_douyin_accounts_open_id", "douyin_accounts", ["open_id"])
    op.create_index("ix_douyin_accounts_union_id", "douyin_accounts", ["union_id"])
    op.create_index("ix_douyin_accounts_status", "douyin_accounts", ["status"])
    op.create_index("ix_douyin_accounts_tenant_status", "douyin_accounts", ["tenant_id", "status"])

    op.create_table(
        "douyin_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("douyin_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_count", sa.Integer(), nullable=False),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id"),
    )
    op.create_index("ix_douyin_tokens_account_id", "douyin_tokens", ["account_id"])
    op.create_index("ix_douyin_tokens_access_token_expires_at", "douyin_tokens", ["access_token_expires_at"])
    op.create_index("ix_douyin_tokens_refresh_token_expires_at", "douyin_tokens", ["refresh_token_expires_at"])
    op.create_index("ix_douyin_tokens_status", "douyin_tokens", ["status"])

    op.create_table(
        "douyin_publish_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("douyin_accounts.id"), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("approval_requests.id"), nullable=True),
        sa.Column("content_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("hashtags", postgresql.JSON(), nullable=False),
        sa.Column("visibility", sa.String(length=40), nullable=False),
        sa.Column("asset_refs", postgresql.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("publish_mode", sa.String(length=40), nullable=False),
        sa.Column("approval_status", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=60), nullable=False),
        sa.Column("external_item_id", sa.String(length=200), nullable=True),
        sa.Column("external_video_id", sa.String(length=200), nullable=True),
        sa.Column("share_id", sa.String(length=200), nullable=True),
        sa.Column("share_state", sa.String(length=200), nullable=True),
        sa.Column("share_schema_url", sa.Text(), nullable=True),
        sa.Column("share_nonce", sa.String(length=80), nullable=True),
        sa.Column("share_signature", sa.String(length=160), nullable=True),
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("official_error_code", sa.String(length=80), nullable=True),
        sa.Column("official_log_id", sa.String(length=160), nullable=True),
        sa.Column("redacted_request_summary", postgresql.JSON(), nullable=False),
        sa.Column("response_summary", postgresql.JSON(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_douyin_publish_tenant_idempotency"),
    )
    op.create_index("ix_douyin_publish_jobs_tenant_id", "douyin_publish_jobs", ["tenant_id"])
    op.create_index("ix_douyin_publish_jobs_agent_id", "douyin_publish_jobs", ["agent_id"])
    op.create_index("ix_douyin_publish_jobs_account_id", "douyin_publish_jobs", ["account_id"])
    op.create_index("ix_douyin_publish_jobs_approval_id", "douyin_publish_jobs", ["approval_id"])
    op.create_index("ix_douyin_publish_jobs_status", "douyin_publish_jobs", ["status"])
    op.create_index("ix_douyin_publish_jobs_external_item_id", "douyin_publish_jobs", ["external_item_id"])
    op.create_index("ix_douyin_publish_jobs_external_video_id", "douyin_publish_jobs", ["external_video_id"])
    op.create_index("ix_douyin_publish_jobs_publish_mode", "douyin_publish_jobs", ["publish_mode"])
    op.create_index("ix_douyin_publish_jobs_share_id", "douyin_publish_jobs", ["share_id"])
    op.create_index("ix_douyin_publish_jobs_share_state", "douyin_publish_jobs", ["share_state"])
    op.create_index("ix_douyin_publish_jobs_share_expires_at", "douyin_publish_jobs", ["share_expires_at"])
    op.create_index("ix_douyin_publish_jobs_created_at", "douyin_publish_jobs", ["created_at"])
    op.create_index("ix_douyin_publish_jobs_tenant_status", "douyin_publish_jobs", ["tenant_id", "status"])
    op.create_index("ix_douyin_publish_jobs_agent_status", "douyin_publish_jobs", ["agent_id", "status"])

    op.create_table(
        "douyin_metric_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("douyin_accounts.id"), nullable=False),
        sa.Column("external_item_id", sa.String(length=200), nullable=True),
        sa.Column("metric_type", sa.String(length=40), nullable=False),
        sa.Column("source_api", sa.String(length=160), nullable=False),
        sa.Column("data_freshness", sa.String(length=40), nullable=False),
        sa.Column("metrics_json", postgresql.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_douyin_metric_snapshots_tenant_id", "douyin_metric_snapshots", ["tenant_id"])
    op.create_index("ix_douyin_metric_snapshots_account_id", "douyin_metric_snapshots", ["account_id"])
    op.create_index("ix_douyin_metric_snapshots_external_item_id", "douyin_metric_snapshots", ["external_item_id"])
    op.create_index("ix_douyin_metric_snapshots_captured_at", "douyin_metric_snapshots", ["captured_at"])
    op.create_index("ix_douyin_metric_account_captured", "douyin_metric_snapshots", ["account_id", "captured_at"])
    op.create_index("ix_douyin_metric_tenant_type", "douyin_metric_snapshots", ["tenant_id", "metric_type"])

    op.create_table(
        "douyin_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("douyin_accounts.id"), nullable=False),
        sa.Column("external_item_id", sa.String(length=200), nullable=True),
        sa.Column("comment_id", sa.String(length=200), nullable=False),
        sa.Column("parent_comment_id", sa.String(length=200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_display", sa.String(length=200), nullable=True),
        sa.Column("sentiment", sa.String(length=40), nullable=True),
        sa.Column("intent", sa.String(length=80), nullable=True),
        sa.Column("risk_level", sa.String(length=40), nullable=False),
        sa.Column("needs_reply", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", "comment_id", name="uq_douyin_comment_account_comment"),
    )
    op.create_index("ix_douyin_comments_tenant_id", "douyin_comments", ["tenant_id"])
    op.create_index("ix_douyin_comments_account_id", "douyin_comments", ["account_id"])
    op.create_index("ix_douyin_comments_external_item_id", "douyin_comments", ["external_item_id"])
    op.create_index("ix_douyin_comments_account_risk", "douyin_comments", ["account_id", "risk_level"])

    op.create_table(
        "douyin_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("douyin_accounts.id"), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("approval_requests.id"), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("operation_type", sa.String(length=80), nullable=False),
        sa.Column("target_id", sa.String(length=200), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("approval_status", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=60), nullable=False),
        sa.Column("request_summary", postgresql.JSON(), nullable=False),
        sa.Column("response_summary", postgresql.JSON(), nullable=False),
        sa.Column("official_error_code", sa.String(length=80), nullable=True),
        sa.Column("official_log_id", sa.String(length=160), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_douyin_operation_tenant_idempotency"),
    )
    op.create_index("ix_douyin_operations_tenant_id", "douyin_operations", ["tenant_id"])
    op.create_index("ix_douyin_operations_agent_id", "douyin_operations", ["agent_id"])
    op.create_index("ix_douyin_operations_account_id", "douyin_operations", ["account_id"])
    op.create_index("ix_douyin_operations_approval_id", "douyin_operations", ["approval_id"])
    op.create_index("ix_douyin_operations_operation_type", "douyin_operations", ["operation_type"])
    op.create_index("ix_douyin_operations_target_id", "douyin_operations", ["target_id"])
    op.create_index("ix_douyin_operations_created_at", "douyin_operations", ["created_at"])
    op.create_index("ix_douyin_operations_agent_status", "douyin_operations", ["agent_id", "status"])
    op.create_index("ix_douyin_operations_tenant_type", "douyin_operations", ["tenant_id", "operation_type"])


def downgrade() -> None:
    op.drop_table("douyin_operations")
    op.drop_table("douyin_comments")
    op.drop_table("douyin_metric_snapshots")
    op.drop_table("douyin_publish_jobs")
    op.drop_table("douyin_tokens")
    op.drop_table("douyin_accounts")
    op.drop_table("douyin_oauth_states")
