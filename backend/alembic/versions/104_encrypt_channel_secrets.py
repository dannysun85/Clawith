"""Encrypt every ChannelConfig credential at rest.

Revision ID: channel_secret_envelopes
Revises: plaza_counter_integrity
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from app.core.channel_secrets import (
    is_channel_secret_envelope,
    open_channel_secret,
    seal_legacy_channel_json,
    seal_legacy_channel_secret,
)


revision: str = "channel_secret_envelopes"
down_revision: str | Sequence[str] | None = "plaza_counter_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TEXT_PURPOSES = {
    "app_secret": "app_secret",
    "encrypt_key": "encrypt_key",
    "verification_token": "verification_token",
}


def _rows(bind):
    return bind.execute(
        sa.text(
            """
            SELECT id, app_secret, encrypt_key, verification_token, extra_config
            FROM channel_configs
            ORDER BY id
            """
        )
    ).mappings()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for column_name in (*_TEXT_PURPOSES, "extra_config"):
            op.alter_column(
                "channel_configs",
                column_name,
                type_=sa.Text(),
                postgresql_using=f"{column_name}::text",
                existing_nullable=column_name != "extra_config",
            )

    for row in list(_rows(bind)):
        values = {
            column_name: seal_legacy_channel_secret(
                row[column_name],
                purpose=purpose,
            )
            for column_name, purpose in _TEXT_PURPOSES.items()
        }
        values["extra_config"] = seal_legacy_channel_json(
            row["extra_config"],
            purpose="extra_config",
        )
        bind.execute(
            sa.text(
                """
                UPDATE channel_configs
                SET app_secret = :app_secret,
                    encrypt_key = :encrypt_key,
                    verification_token = :verification_token,
                    extra_config = :extra_config
                WHERE id = :id
                """
            ),
            {"id": row["id"], **values},
        )


def downgrade() -> None:
    bind = op.get_bind()
    for row in list(_rows(bind)):
        values = {}
        for column_name, purpose in _TEXT_PURPOSES.items():
            value = row[column_name]
            values[column_name] = (
                open_channel_secret(value, purpose=purpose)
                if is_channel_secret_envelope(value)
                else value
            )
        extra_config = row["extra_config"]
        values["extra_config"] = (
            open_channel_secret(extra_config, purpose="extra_config")
            if is_channel_secret_envelope(extra_config)
            else extra_config
        ) or "{}"
        bind.execute(
            sa.text(
                """
                UPDATE channel_configs
                SET app_secret = :app_secret,
                    encrypt_key = :encrypt_key,
                    verification_token = :verification_token,
                    extra_config = :extra_config
                WHERE id = :id
                """
            ),
            {"id": row["id"], **values},
        )

    if bind.dialect.name == "postgresql":
        op.alter_column(
            "channel_configs",
            "extra_config",
            type_=sa.JSON(),
            postgresql_using="extra_config::json",
            existing_nullable=False,
        )
