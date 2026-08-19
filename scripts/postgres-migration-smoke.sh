#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db_name="clawith_migration_smoke_${USER//[^a-zA-Z0-9_]/_}_$$"
fresh_db_name="${db_name}_fresh"
partial_db_name="${db_name}_partial"
db_user="${PGUSER:-$USER}"
db_host="${PGHOST:-127.0.0.1}"
db_port="${PGPORT:-5432}"
release_head="${MIGRATION_SMOKE_EXPECTED_HEAD:-payment_order_period}"

assert_at_release_head() {
  .venv/bin/alembic current | grep -F "${release_head} (head)"
}

restore_runtime_chat_foreign_key() {
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF to_regclass('public.agent_runs') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1
       FROM pg_constraint
       WHERE conrelid = 'agent_runs'::regclass
         AND conname = 'fk_agent_runs_tenant_session_chat_sessions'
     ) THEN
    ALTER TABLE agent_runs
      ADD CONSTRAINT fk_agent_runs_tenant_session_chat_sessions
      FOREIGN KEY (tenant_id, session_id)
      REFERENCES chat_sessions (tenant_id, id);
  END IF;
END $$;
SQL
}

assert_product_line_release_repairs() {
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM user_tenant_onboardings AS onboarding
    JOIN agents AS agent ON agent.id = onboarding.personal_assistant_agent_id
    WHERE onboarding.id = '07500000-0000-4000-8000-000000000240'
      AND agent.access_mode = 'private'
      AND agent.company_access_level = 'use'
  ) OR (
    SELECT count(*)
    FROM agent_permissions
    WHERE agent_id = '07500000-0000-4000-8000-000000000061'
      AND scope_type = 'user'
      AND scope_id = '07500000-0000-4000-8000-000000000060'
      AND access_level = 'manage'
  ) <> 1 OR (
    SELECT count(*)
    FROM agent_permissions
    WHERE agent_id = '07500000-0000-4000-8000-000000000061'
  ) <> 1 THEN
    RAISE EXCEPTION 'private assistant repair did not enforce exact owner-only access';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'media_generation_tasks'
      AND column_name = 'agent_id'
      AND is_nullable = 'YES'
  ) OR (
    SELECT count(*)
    FROM pg_constraint
    WHERE conrelid = 'media_generation_tasks'::regclass
      AND confrelid = 'agents'::regclass
      AND contype = 'f'
      AND confdeltype = 'n'
  ) <> 1 THEN
    RAISE EXCEPTION 'media task repair did not enforce nullable ON DELETE SET NULL retention';
  END IF;
  IF (
    SELECT count(*)
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND (
        (table_name = 'tenants' AND column_name IN (
          'company_size',
          'allow_member_private_agents',
          'default_approval_policy'
        ))
        OR (table_name = 'users' AND column_name IN (
          'timezone',
          'work_hours_start',
          'work_hours_end'
        ))
      )
  ) <> 6 OR EXISTS (
    SELECT 1 FROM tenants WHERE initialization_completed_at IS NULL
  ) THEN
    RAISE EXCEPTION 'onboarding product settings or historical-company compatibility backfill is incomplete';
  END IF;
  IF to_regclass('public.outbound_email_deliveries') IS NULL OR NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'organization_invitations'
      AND column_name = 'delivery_mode'
      AND is_nullable = 'NO'
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'outbound_email_deliveries'
      AND indexname = 'uq_outbound_email_deliveries_idempotency_key'
      AND indexdef LIKE 'CREATE UNIQUE INDEX%WHERE (idempotency_key IS NOT NULL)'
  ) THEN
    RAISE EXCEPTION 'durable outbound email ledger contract is incomplete';
  END IF;
  IF to_regclass('public.identity_mfa_recovery_codes') IS NULL
     OR to_regclass('public.identity_mfa_challenges') IS NULL
     OR (
       SELECT count(*)
       FROM information_schema.columns
       WHERE table_schema = 'public'
         AND table_name = 'identities'
         AND column_name IN (
           'mfa_secret_envelope',
           'mfa_enabled',
           'mfa_confirmed_at',
           'mfa_last_totp_step'
         )
     ) <> 4
     OR NOT EXISTS (
       SELECT 1
       FROM information_schema.columns
       WHERE table_schema = 'public'
         AND table_name = 'identities'
         AND column_name = 'mfa_enabled'
         AND is_nullable = 'NO'
         AND column_default = 'false'
     )
     OR NOT EXISTS (
       SELECT 1
       FROM pg_indexes
       WHERE schemaname = 'public'
         AND tablename = 'identity_mfa_recovery_codes'
         AND indexname = 'uq_identity_mfa_recovery_codes_active_hash'
         AND indexdef LIKE 'CREATE UNIQUE INDEX%WHERE (used_at IS NULL)'
     )
     OR NOT EXISTS (
       SELECT 1
       FROM pg_indexes
       WHERE schemaname = 'public'
         AND tablename = 'identity_mfa_challenges'
         AND indexname = 'ix_identity_mfa_challenges_active'
         AND indexdef LIKE 'CREATE INDEX%WHERE (consumed_at IS NULL)'
     ) THEN
    RAISE EXCEPTION 'Identity MFA schema contract is incomplete';
  END IF;
  IF to_regclass('public.tenant_deletion_jobs') IS NULL
     OR to_regclass('public.tenant_deletion_holds') IS NULL
     OR to_regclass('public.tenant_deletion_tombstones') IS NULL
     OR NOT EXISTS (
       SELECT 1
       FROM pg_indexes
       WHERE schemaname = 'public'
         AND tablename = 'tenant_deletion_holds'
         AND indexname = 'uq_tenant_deletion_holds_active_type'
         AND indexdef LIKE 'CREATE UNIQUE INDEX%WHERE (released_at IS NULL)'
     ) THEN
    RAISE EXCEPTION 'Tenant deletion purge schema contract is incomplete';
  END IF;
END $$;
SQL
}

cleanup() {
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$fresh_db_name"
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$partial_db_name"
}
trap cleanup EXIT

assert_legacy_channel_config_downgraded() {
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM channel_configs
    WHERE id = '07500000-0000-4000-8000-000000000190'
      AND app_secret = 'legacy-channel-app-secret'
      AND encrypt_key = 'legacy-channel-signing-secret'
      AND verification_token = 'legacy-channel-verification-token'
      AND extra_config::jsonb =
          '{"connection_mode":"webhook","future":{"token":"legacy-channel-nested-token"}}'::jsonb
      AND app_secret NOT LIKE 'enc:channel:v1:%'
      AND encrypt_key NOT LIKE 'enc:channel:v1:%'
      AND verification_token NOT LIKE 'enc:channel:v1:%'
      AND extra_config::text NOT LIKE 'enc:channel:v1:%'
  ) THEN
    RAISE EXCEPTION '104 downgrade did not restore the legacy ChannelConfig contract';
  END IF;
END $$;
SQL
}

assert_sso_password_security_downgraded() {
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'identities'
      AND column_name = 'auth_version'
  ) OR EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'users'
      AND indexname = 'uq_users_identity_tenantless'
  ) THEN
    RAISE EXCEPTION '106 downgrade left identity revocation state behind';
  END IF;
END $$;
SQL
}

assert_sso_password_security() {
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'identities'
      AND column_name = 'auth_version'
      AND is_nullable = 'NO'
      AND column_default IS NOT NULL
  ) OR EXISTS (
    SELECT 1
    FROM identities
    WHERE auth_version <> 0
  ) THEN
    RAISE EXCEPTION '106 did not add a fail-closed zeroed identity auth version';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_class AS index_relation
    JOIN pg_index AS index_metadata
      ON index_metadata.indexrelid = index_relation.oid
    JOIN pg_class AS table_relation
      ON table_relation.oid = index_metadata.indrelid
    JOIN pg_namespace AS table_namespace
      ON table_namespace.oid = table_relation.relnamespace
    WHERE table_namespace.nspname = 'public'
      AND table_relation.relname = 'users'
      AND index_relation.relname = 'uq_users_identity_tenantless'
      AND index_metadata.indisunique = true
      AND index_metadata.indpred IS NOT NULL
  ) THEN
    RAISE EXCEPTION '106 did not enforce one tenantless membership per identity';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'users'
      AND column_name = 'activation_pending_email_verification'
      AND is_nullable = 'NO'
  ) THEN
    RAISE EXCEPTION '106 did not add the explicit email-verification activation provenance';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM users
    WHERE id = '07500000-0000-4000-8000-000000000215'
      AND is_active = false
      AND activation_pending_email_verification = false
  ) THEN
    RAISE EXCEPTION '106 guessed that a historical disabled member was verification-pending';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM users
    WHERE id = '07500000-0000-4000-8000-000000000216'
      AND is_active = false
      AND activation_pending_email_verification = false
  ) THEN
    RAISE EXCEPTION '106 inferred a privileged disabled membership as verification-pending';
  END IF;
  IF EXISTS (
    SELECT 1 FROM users
    WHERE activation_pending_email_verification = true
  ) THEN
    RAISE EXCEPTION '106 marked an unrelated historical membership as verification-pending';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM identities
    WHERE id = '07500000-0000-4000-8000-000000000201'
      AND password_hash = '$2b$04$oIaUgIZ72SRhjByY./rb1ObilDkrdgd6nnAFu3rxTokIqqu5x8kRK'
      AND password_login_enabled = false
  ) THEN
    RAISE EXCEPTION '106 destructively changed or enabled an ambiguous SSO-origin password';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM identities
    WHERE id = '07500000-0000-4000-8000-000000000202'
      AND password_hash = '$2b$04$wou6LFMDWc07CNoJC4jzI.MJFWyZCOARm97B4syMJpisI2EwlNEpy'
      AND password_login_enabled = true
  ) THEN
    RAISE EXCEPTION '106 changed an unrelated Web password';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM identities
    WHERE id = '07500000-0000-4000-8000-000000000205'
      AND password_login_enabled = true
  ) THEN
    RAISE EXCEPTION '106 disabled an inactive Web password because of its Platform membership';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM identities
    WHERE id = '07500000-0000-4000-8000-000000000206'
      AND password_login_enabled = true
  ) THEN
    RAISE EXCEPTION '106 disabled an administrator-disabled Web password because of its Platform membership';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM identities
    WHERE id = '07500000-0000-4000-8000-000000000203'
      AND password_hash = '$2b$04$oIaUgIZ72SRhjByY./rb1ObilDkrdgd6nnAFu3rxTokIqqu5x8kRK'
      AND password_login_enabled = false
  ) THEN
    RAISE EXCEPTION '106 destructively changed or enabled a provider-linked password';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM identities
    WHERE id = '07500000-0000-4000-8000-000000000204'
      AND password_hash = '$2b$04$wou6LFMDWc07CNoJC4jzI.MJFWyZCOARm97B4syMJpisI2EwlNEpy'
      AND password_login_enabled = false
  ) THEN
    RAISE EXCEPTION '106 destructively changed or enabled an ambiguous linked Web password';
  END IF;
END $$;
SQL
}

createdb --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
createdb --host "$db_host" --port "$db_port" --username "$db_user" "$fresh_db_name"
createdb --host "$db_host" --port "$db_port" --username "$db_user" "$partial_db_name"
export DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${db_name}"

cd "$repo_root/backend"

# The bootstrap revision reflects current ORM metadata, so a true empty-db
# install exercises a different path from the production-era reconstruction
# below. Both must reach the one release head without duplicate DDL.
DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${fresh_db_name}" \
  .venv/bin/alembic upgrade head
DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${fresh_db_name}" \
  bash -c '.venv/bin/alembic current' | grep -F "${release_head} (head)"
DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${fresh_db_name}" \
  PYTHONPATH=. .venv/bin/python -m app.scripts.verify_identity_provider_secrets

# A failed historical rollout may leave the allowance table only partially
# created. The release must fail before serving traffic instead of silently
# adding indexes to an ORM-incompatible shape.
DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${partial_db_name}" \
  .venv/bin/alembic upgrade backfill_private_assistant_tpl
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$partial_db_name" --set ON_ERROR_STOP=1 <<'SQL'
DROP TABLE media_provider_daily_allowance_claims;
CREATE TABLE media_provider_daily_allowance_claims (
  id uuid PRIMARY KEY
);
SQL
set +e
partial_upgrade_output="$({
  DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${partial_db_name}" \
    .venv/bin/alembic upgrade head
} 2>&1)"
partial_upgrade_status=$?
set -e
if [[ "$partial_upgrade_status" -eq 0 ]]; then
  echo "partial allowance table unexpectedly passed migration" >&2
  exit 1
fi
grep -F "Incompatible pre-existing media_provider_daily_allowance_claims" \
  <<<"$partial_upgrade_output"

.venv/bin/alembic upgrade add_douyin_collab_publish_fields

# Simulate a deployment already stamped beyond revisions that were later
# inserted into historical migration order.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
-- Reproduce the exact gateway queue shape observed on the supported
-- production line before migration 099 added its privacy/lease columns.
DROP INDEX IF EXISTS ix_gateway_messages_delivery_claim;
ALTER TABLE gateway_messages
  DROP CONSTRAINT IF EXISTS gateway_messages_authorization_source_agent_id_fkey;
ALTER TABLE gateway_messages
  DROP CONSTRAINT IF EXISTS fk_gateway_messages_authorization_source_agent;
ALTER TABLE gateway_messages
  DROP COLUMN IF EXISTS authorization_source_agent_id;
ALTER TABLE gateway_messages
  DROP COLUMN IF EXISTS delivery_lease_expires_at;
ALTER TABLE gateway_messages
  DROP COLUMN IF EXISTS delivery_attempts;

-- Reproduce the corresponding pre-099 trigger queue shape. The repair must
-- preserve existing executions while adding accounting and serialization.
DROP INDEX IF EXISTS uq_trigger_executions_processing_agent;
ALTER TABLE trigger_executions
  DROP COLUMN IF EXISTS fire_recorded_at;

ALTER TABLE llm_models DROP COLUMN IF EXISTS verification_status;
ALTER TABLE llm_models DROP COLUMN IF EXISTS last_verified_at;
ALTER TABLE llm_models DROP COLUMN IF EXISTS last_error_code;
ALTER TABLE llm_models DROP COLUMN IF EXISTS last_error_message;
ALTER TABLE IF EXISTS company_social_account_capabilities DROP COLUMN IF EXISTS auth_type;
DROP TABLE IF EXISTS agentbay_session_ledger;

INSERT INTO tenants (
  id, name, slug, im_provider, is_active,
  default_message_limit, default_message_period, default_max_agents,
  default_agent_ttl_hours, default_max_llm_calls_per_day,
  min_heartbeat_interval_minutes, timezone, country_region, sso_enabled,
  default_max_triggers, min_poll_interval_floor, max_webhook_rate_ceiling,
  a2a_async_enabled
) VALUES (
  '07500000-0000-4000-8000-000000000002', 'Migration Smoke', 'migration-smoke', 'web_only', true,
  50, 'permanent', 1, 0, 1000, 240, 'UTC', '001', false, 20, 5, 5, true
);
INSERT INTO credit_balances (tenant_id, balance, reserved)
VALUES ('07500000-0000-4000-8000-000000000002', 1000, 0);
INSERT INTO credit_transactions (
  id, tenant_id, delta, balance_after, reason, ref_type, ref_id
) VALUES (
  '07500000-0000-4000-8000-000000000003',
  '07500000-0000-4000-8000-000000000002',
  250, 250, 'topup', 'migration-smoke', '07500000-0000-4000-8000-000000000004'
);

-- Reproduce the three platform routing rows used by production before 084.
-- Their encrypted key values are inert metadata: platform calls resolve the
-- actual provider key from llm_credentials.
INSERT INTO llm_models (
  id, provider, model, api_key_encrypted, label, enabled, supports_vision,
  modality, modalities, tier, capabilities, max_output_tokens
) VALUES
  (
    '07500000-0000-4000-8000-000000000020', 'minimax', 'MiniMax-M3',
    'migration-smoke-placeholder', 'MiniMax-M3 Lite (Platform)', true, false,
    'text', '["text"]'::json, 'lite', '{"stream":true,"tool_call":true}'::json, 2048
  ),
  (
    '07500000-0000-4000-8000-000000000021', 'minimax', 'MiniMax-M3',
    'migration-smoke-placeholder', 'MiniMax-M3 Pro (Platform)', true, false,
    'text', '["text"]'::json, 'pro', '{"stream":true,"tool_call":true}'::json, 2048
  ),
  (
    '07500000-0000-4000-8000-000000000022', 'minimax', 'MiniMax-M3',
    'migration-smoke-placeholder', 'MiniMax-M3 Ultra (Platform)', true, false,
    'text', '["text"]'::json, 'ultra', '{"stream":true,"tool_call":true}'::json, 2048
  );
INSERT INTO model_routes (
  id, saas_tier, modality, llm_model_id, priority, enabled
) VALUES
  (
    '07500000-0000-4000-8000-000000000030', 'lite', 'text',
    '07500000-0000-4000-8000-000000000020', 100, true
  ),
  (
    '07500000-0000-4000-8000-000000000031', 'pro', 'text',
    '07500000-0000-4000-8000-000000000021', 100, true
  ),
  (
    '07500000-0000-4000-8000-000000000032', 'ultra', 'text',
    '07500000-0000-4000-8000-000000000022', 100, true
  );

-- Reproduce the global MiniMax media tools and stale TTS billing row observed
-- in production before 088. No credential material is stored on these rows.
INSERT INTO tools (
  id, name, display_name, description, type, category, icon,
  parameters_schema, config, config_schema, enabled, is_default, source, tenant_id
) VALUES
  (
    '07500000-0000-4000-8000-000000000040', 'generate_image_minimax', 'MiniMax Image', '',
    'builtin', 'media', 'I', '{}'::json, '{"model":"image-01"}'::json, '{}'::json,
    true, true, 'builtin', NULL
  ),
  (
    '07500000-0000-4000-8000-000000000041', 'generate_speech_minimax', 'MiniMax Speech', '',
    'builtin', 'media', 'A', '{}'::json, '{"model":"speech-2.8-turbo"}'::json, '{}'::json,
    true, true, 'builtin', NULL
  ),
  (
    '07500000-0000-4000-8000-000000000042', 'generate_music_minimax', 'MiniMax Music', '',
    'builtin', 'media', 'M', '{}'::json, '{"model":"music-2.6"}'::json, '{}'::json,
    true, true, 'builtin', NULL
  ),
  (
    '07500000-0000-4000-8000-000000000043', 'generate_video_minimax', 'MiniMax Video', '',
    'builtin', 'media', 'V', '{}'::json,
    '{"model":"MiniMax-Hailuo-2.3","duration":6,"resolution":"1080P"}'::json,
    '{}'::json, true, true, 'builtin', NULL
  );
INSERT INTO billing_rules (
  id, action, modality, tier, unit, credit_cost, enabled, priority
) VALUES (
  '07500000-0000-4000-8000-000000000044',
  'tts', 'tts', 'pro', 'call', 5, true, 0
);
SQL

.venv/bin/alembic upgrade add_user_chat_tier_preference

# Reproduce administrator-owned catalog and billing data created immediately
# before 093. The M3 migration must neither adopt nor delete these rows, even
# when a label or priority happens to match its historical seed convention.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO llm_models (
  id, provider, model, api_key_encrypted, label, enabled, supports_vision,
  modality, modalities, tier, capabilities, max_output_tokens
) VALUES (
  '07500000-0000-4000-8000-000000000023', 'minimax', 'MiniMax-M3',
  'administrator-placeholder', 'MiniMax-M3 Lite (Platform)', true, false,
  'text', '["text"]'::json, 'lite', '{"administrator_owned":true}'::json, 777
);
INSERT INTO billing_rules (
  id, action, modality, tier, unit, credit_cost, enabled, priority
) VALUES (
  '07500000-0000-4000-8000-000000000024',
  'chat', 'video', 'ultra', 'call', 77, true, 93
);
SQL

.venv/bin/alembic upgrade disable_system_okr_automation
# The bootstrap migration creates tables from current ORM metadata. This lane
# deliberately reconstructs the v1.10.13 production schema so the upstream
# unified-chat revision must perform its real backfill instead of taking the
# metadata-precreated shortcut.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
-- The bootstrap imports current ORM metadata, so this future Runtime foreign
-- key exists before the historical chat fixture is reconstructed. Remove only
-- that dependency here; restore_runtime_chat_foreign_key recreates it after
-- the unified-chat migration has restored the referenced unique constraint.
ALTER TABLE agent_runs
DROP CONSTRAINT IF EXISTS fk_agent_runs_tenant_session_chat_sessions;
ALTER TABLE chat_sessions
DROP CONSTRAINT IF EXISTS fk_chat_sessions_tenant_id_tenants,
DROP CONSTRAINT IF EXISTS fk_chat_sessions_group_id_groups,
DROP CONSTRAINT IF EXISTS fk_chat_sessions_created_by_participant_id_participants,
DROP CONSTRAINT IF EXISTS uq_chat_sessions_tenant_id_id,
DROP CONSTRAINT IF EXISTS ck_chat_sessions_session_type;
DROP INDEX IF EXISTS ix_chat_sessions_tenant_id;
DROP INDEX IF EXISTS ix_chat_sessions_group_id;
DROP INDEX IF EXISTS uq_chat_sessions_primary_direct;
DROP INDEX IF EXISTS uq_chat_sessions_primary_group;
ALTER TABLE chat_sessions
ALTER COLUMN agent_id SET NOT NULL,
ALTER COLUMN user_id SET NOT NULL,
DROP COLUMN IF EXISTS tenant_id,
DROP COLUMN IF EXISTS session_type,
DROP COLUMN IF EXISTS group_id,
DROP COLUMN IF EXISTS created_by_participant_id,
DROP COLUMN IF EXISTS deleted_at,
DROP COLUMN IF EXISTS updated_at;
ALTER TABLE chat_messages
ALTER COLUMN agent_id SET NOT NULL,
ALTER COLUMN user_id SET NOT NULL,
DROP COLUMN IF EXISTS mentions;
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_sessions_primary_platform
ON chat_sessions (agent_id, user_id)
WHERE is_primary = true AND source_channel = 'web' AND is_group = false;

-- Remove post-095 objects so duplicate historical grants can be seeded and
-- 096 is proven to quarantine them before recreating uniqueness.
ALTER TABLE agent_tools
DROP CONSTRAINT IF EXISTS uq_agent_tools_agent_tool;
ALTER TABLE approval_requests
DROP CONSTRAINT IF EXISTS ck_approval_execution_state_consistency,
DROP CONSTRAINT IF EXISTS ck_approval_execution_single_attempt,
DROP CONSTRAINT IF EXISTS ck_approval_execution_status;
DROP INDEX IF EXISTS uq_active_approval_request_fingerprint;
DROP INDEX IF EXISTS ix_approval_execution_claimable;
DROP INDEX IF EXISTS ix_approval_requests_execution_status;
DROP INDEX IF EXISTS ix_approval_requests_execution_not_before;
DROP INDEX IF EXISTS ix_approval_requests_request_fingerprint;
DROP TABLE IF EXISTS production_issue_alert_deliveries CASCADE;
ALTER TABLE approval_requests
DROP COLUMN IF EXISTS execution_error_code,
DROP COLUMN IF EXISTS execution_result_summary,
DROP COLUMN IF EXISTS execution_attempts,
DROP COLUMN IF EXISTS execution_finished_at,
DROP COLUMN IF EXISTS execution_claimed_at,
DROP COLUMN IF EXISTS execution_not_before,
DROP COLUMN IF EXISTS execution_claim_token,
DROP COLUMN IF EXISTS execution_status,
DROP COLUMN IF EXISTS request_fingerprint;
ALTER TABLE production_issues
DROP CONSTRAINT IF EXISTS ck_production_issue_alert_attempts_nonnegative,
DROP CONSTRAINT IF EXISTS ck_production_issue_alert_epoch_positive;
DROP INDEX IF EXISTS ix_production_issues_alert_retry;
ALTER TABLE production_issues
DROP COLUMN IF EXISTS alert_notification_sent_at,
DROP COLUMN IF EXISTS alert_last_error_code,
DROP COLUMN IF EXISTS alert_next_attempt_at,
DROP COLUMN IF EXISTS alert_attempts,
DROP COLUMN IF EXISTS alert_epoch;
SQL
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py seed

# Reproduce a production-era plaintext ChannelConfig. Revision 104 must encrypt
# both the known credential columns and the complete extensible config object,
# then keep the row readable across every downgrade/re-upgrade sequence below.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO channel_configs (
  id, agent_id, channel_type, app_id,
  app_secret, encrypt_key, verification_token,
  is_configured, is_connected, extra_config
) VALUES (
  '07500000-0000-4000-8000-000000000190',
  '07500000-0000-4000-8000-000000000061',
  'slack', 'legacy-channel-client-id',
  'legacy-channel-app-secret',
  'legacy-channel-signing-secret',
  'legacy-channel-verification-token',
  true, false,
  '{"connection_mode":"webhook","future":{"token":"legacy-channel-nested-token"}}'
);
SQL

# Pre-099 trigger metadata came through APIs that accepted arbitrary reserved
# names. Neither a forged value equal to the new attestation version nor a
# structurally valid owner can acquire provenance retroactively.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO agent_triggers (
  id, agent_id, name, type, config, reason, is_enabled, is_system,
  cooldown_seconds, fire_count
) VALUES
  (
    '07500000-0000-4000-8000-000000000110',
    '07500000-0000-4000-8000-000000000061',
    'forged trigger context', 'interval',
    '{"minutes":5,"_server_context_version":1,"_origin_user_id":"07500000-0000-4000-8000-000000000063","_matched_message":"forged"}'::jsonb,
    'migration smoke', true, false, 60, 0
  ),
  (
    '07500000-0000-4000-8000-000000000111',
    '07500000-0000-4000-8000-000000000061',
    'valid legacy trigger context', 'interval',
    '{"minutes":10,"_origin_user_id":"07500000-0000-4000-8000-000000000060"}'::jsonb,
    'migration smoke', true, false, 60, 0
  );

-- Reproduce cross-user A2A reuse, reversed duplicate pairs, and durable
-- references that must survive consolidation. User 112 is deliberately in
-- the same company as the original Agent owner.
DROP INDEX IF EXISTS uq_chat_sessions_a2a_owner;
DROP INDEX IF EXISTS uq_trigger_executions_processing_agent;
INSERT INTO users (
  id, tenant_id, display_name, role, is_active, registration_source,
  quota_message_limit, quota_message_period, quota_messages_used,
  quota_max_agents, quota_agent_ttl_hours
) VALUES (
  '07500000-0000-4000-8000-000000000112',
  '07500000-0000-4000-8000-000000000002',
  'Migration A2A Second User', 'member', true, 'migration-smoke',
  50, 'permanent', 0, 2, 0
);
INSERT INTO agents (
  id, name, role_description, creator_id, tenant_id, agent_type, status,
  autonomy_policy, tokens_used_today, tokens_used_month, tokens_used_total,
  cache_read_tokens_today, cache_read_tokens_month, cache_read_tokens_total,
  cache_creation_tokens_today, cache_creation_tokens_month,
  cache_creation_tokens_total,
  context_window_size, max_tool_rounds, max_triggers,
  min_poll_interval_min, webhook_rate_limit, is_expired, is_system,
  llm_calls_today, max_llm_calls_per_day, heartbeat_enabled,
  heartbeat_interval_minutes, heartbeat_active_hours,
  access_mode, company_access_level
) VALUES (
  '07500000-0000-4000-8000-000000000113',
  'Migration A2A Peer',
  'Migration fixture for A2A ownership partitioning',
  '07500000-0000-4000-8000-000000000060',
  '07500000-0000-4000-8000-000000000002',
  'native',
  'idle',
  '{}'::json, 0, 0, 0,
  0, 0, 0,
  0, 0, 0,
  100, 50, 20,
  5, 5, false, false,
  0, 1000, false,
  240, '09:00-18:00',
  'company', 'use'
);
INSERT INTO chat_sessions (
  id, agent_id, user_id, title, source_channel, peer_agent_id,
  is_group, is_primary, created_at, last_message_at
) VALUES
  (
    '07500000-0000-4000-8000-000000000120',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000060',
    'Legacy mixed owner lane', 'agent',
    '07500000-0000-4000-8000-000000000113', false, false,
    now() - interval '10 minutes', now() - interval '5 minutes'
  ),
  (
    '07500000-0000-4000-8000-000000000121',
    '07500000-0000-4000-8000-000000000113',
    '07500000-0000-4000-8000-000000000060',
    'Legacy reversed duplicate', 'agent',
    '07500000-0000-4000-8000-000000000061', false, false,
    now() - interval '9 minutes', now() - interval '4 minutes'
  );
INSERT INTO chat_messages (
  id, agent_id, user_id, role, content, conversation_id, created_at
) VALUES
  (
    '07500000-0000-4000-8000-000000000130',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000060',
    'user', 'owner one message',
    '07500000-0000-4000-8000-000000000120', now() - interval '8 minutes'
  ),
  (
    '07500000-0000-4000-8000-000000000131',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000112',
    'user', 'owner two private message',
    '07500000-0000-4000-8000-000000000120', now() - interval '7 minutes'
  ),
  (
    '07500000-0000-4000-8000-000000000132',
    '07500000-0000-4000-8000-000000000113',
    '07500000-0000-4000-8000-000000000060',
    'assistant', 'reversed duplicate message',
    '07500000-0000-4000-8000-000000000121', now() - interval '6 minutes'
  );
INSERT INTO gateway_messages (
  id, agent_id, sender_agent_id, sender_user_id, conversation_id, content, status
) VALUES
  (
    '07500000-0000-4000-8000-000000000140',
    '07500000-0000-4000-8000-000000000113',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000060',
    '07500000-0000-4000-8000-000000000121', 'owner one gateway', 'pending'
  ),
  (
    '07500000-0000-4000-8000-000000000141',
    '07500000-0000-4000-8000-000000000113',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000112',
    '07500000-0000-4000-8000-000000000120', 'owner two gateway', 'pending'
  );
INSERT INTO media_generation_tasks (
  id, tenant_id, agent_id, user_id, origin_session_id,
  provider, modality, provider_task_id, status,
  metadata_path, output_path, request_metadata,
  completion_delivery_status, realtime_attempt_count,
  attempt_count, consecutive_error_count
) VALUES (
  '07500000-0000-4000-8000-000000000142',
  '07500000-0000-4000-8000-000000000002',
  '07500000-0000-4000-8000-000000000061',
  '07500000-0000-4000-8000-000000000112',
  '07500000-0000-4000-8000-000000000120',
  'migration-smoke', 'video', 'migration-smoke-owner-two', 'polling',
  'workspace/media/migration.json', 'workspace/videos/migration.mp4', '{}'::json,
  'pending', 0, 0, 0
);
INSERT INTO workspace_file_revisions (
  id, agent_id, path, operation, actor_type, actor_id, session_id,
  content_hash
) VALUES (
  '07500000-0000-4000-8000-000000000143',
  '07500000-0000-4000-8000-000000000061', 'workspace/report.md',
  'write', 'user', '07500000-0000-4000-8000-000000000060',
  '07500000-0000-4000-8000-000000000121', 'migration-smoke'
);
INSERT INTO workspace_edit_locks (
  id, agent_id, path, user_id, session_id, expires_at, heartbeat_count
) VALUES (
  '07500000-0000-4000-8000-000000000144',
  '07500000-0000-4000-8000-000000000061', 'workspace/private.md',
  '07500000-0000-4000-8000-000000000112',
  '07500000-0000-4000-8000-000000000120', now() + interval '1 hour', 0
);
INSERT INTO agent_triggers (
  id, agent_id, name, type, config, reason, is_enabled, is_system,
  cooldown_seconds, fire_count
) VALUES
  (
    '07500000-0000-4000-8000-000000000114',
    '07500000-0000-4000-8000-000000000061',
    'legacy a2a reference', 'on_message',
    '{"from_agent_name":"Migration A2A Peer","expected_conversation_id":"07500000-0000-4000-8000-000000000121","_origin_session_id":"07500000-0000-4000-8000-000000000121","_origin_user_id":"07500000-0000-4000-8000-000000000060"}'::jsonb,
    'migration reference smoke', true, false, 60, 0
  ),
  (
    '07500000-0000-4000-8000-000000000115',
    '07500000-0000-4000-8000-000000000061',
    'serialization ordering fixture', 'interval', '{"minutes":30}'::jsonb,
    'migration queue smoke', true, false, 60, 0
  );
INSERT INTO agent_schedules (
  id, agent_id, name, instruction, cron_expr, is_enabled,
  next_run_at, run_count, created_by
) VALUES (
  '07500000-0000-4000-8000-000000000116',
  '07500000-0000-4000-8000-000000000061',
  'preserved schedule intent',
  'Run only after a future safe worker is explicitly enabled',
  '0 9 * * *', true, now() + interval '1 day', 0,
  '07500000-0000-4000-8000-000000000060'
);
INSERT INTO trigger_executions (
  id, trigger_id, agent_id, source, status, idempotency_key, payload,
  payload_text, scheduled_at, started_at, lease_owner, lease_expires_at
) VALUES
  (
    '07500000-0000-4000-8000-000000000150',
    '07500000-0000-4000-8000-000000000114',
    '07500000-0000-4000-8000-000000000061', 'a2a', 'succeeded',
    'migration-a2a-owner-one',
    '{"_a2a_session_id":"07500000-0000-4000-8000-000000000121","_origin_session_id":"07500000-0000-4000-8000-000000000121","_matched_conversation_id":"07500000-0000-4000-8000-000000000121","_origin_user_id":"07500000-0000-4000-8000-000000000060"}'::jsonb,
    '', now() - interval '6 minutes', now() - interval '6 minutes', NULL, NULL
  ),
  (
    '07500000-0000-4000-8000-000000000151',
    '07500000-0000-4000-8000-000000000114',
    '07500000-0000-4000-8000-000000000061', 'a2a', 'succeeded',
    'migration-a2a-owner-two',
    '{"_a2a_session_id":"07500000-0000-4000-8000-000000000120","_origin_session_id":"07500000-0000-4000-8000-000000000120","_matched_conversation_id":"07500000-0000-4000-8000-000000000120","_origin_user_id":"07500000-0000-4000-8000-000000000112"}'::jsonb,
    '', now() - interval '5 minutes', now() - interval '5 minutes', NULL, NULL
  ),
  (
    '07500000-0000-4000-8000-000000000152',
    '07500000-0000-4000-8000-000000000115',
    '07500000-0000-4000-8000-000000000061', 'interval', 'pending',
    'migration-older-pending', '{}'::jsonb, '', now() - interval '4 minutes',
    NULL, NULL, NULL
  ),
  (
    '07500000-0000-4000-8000-000000000153',
    '07500000-0000-4000-8000-000000000115',
    '07500000-0000-4000-8000-000000000061', 'interval', 'processing',
    'migration-newer-processing', '{}'::jsonb, '', now() - interval '3 minutes',
    now() - interval '3 minutes', 'legacy-worker', now() + interval '1 hour'
  );
SQL

# Install and execute the exact production rollback guard before 095. This
# proves that an unsafe old process cannot resurrect MCP while migrations are
# in flight, and that 095's narrowly scoped trusted GUC can still backfill the
# one-company legacy rows.
PYTHONPATH=. .venv/bin/python ../scripts/extract-deploy-mcp-sql.py quarantine | \
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$db_name" --set ON_ERROR_STOP=1 \
    --set snapshot_id=migration-smoke

psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE tools
SET tenant_id = '07500000-0000-4000-8000-000000000062',
    source = 'admin',
    type = 'builtin',
    is_default = true,
    enabled = true,
    mcp_server_url = 'https://unsafe-change.example/mcp',
    config = '{"api_key":"unsafe-change"}'::json
WHERE id = '07500000-0000-4000-8000-000000000070';
INSERT INTO tools (
  id, name, display_name, description, type, category, icon,
  parameters_schema, config, config_schema, mcp_server_url,
  mcp_server_name, mcp_tool_name, enabled, is_default, source, tenant_id
) VALUES (
  '07500000-0000-4000-8000-000000000073',
  'guard_created_mcp', 'Guard-created MCP', '', 'mcp', 'mcp', 'M',
  '{}'::json, '{"api_key":"old-writer-secret"}'::json, '{}'::json,
  'https://old-writer.example/mcp', 'Old writer', 'run', true, true,
  'agent', '07500000-0000-4000-8000-000000000002'
);
INSERT INTO agent_tools (
  id, agent_id, tool_id, enabled, config, source
) VALUES (
  '07500000-0000-4000-8000-000000000074',
  '07500000-0000-4000-8000-000000000061',
  '07500000-0000-4000-8000-000000000073',
  true, '{"api_key":"old-writer-secret"}'::json, 'user_installed'
);
UPDATE agent_tools
SET tool_id = '07500000-0000-4000-8000-000000000071',
    agent_id = '07500000-0000-4000-8000-000000000064',
    enabled = true,
    config = '{"api_key":"rebound-secret"}'::json
WHERE agent_id = '07500000-0000-4000-8000-000000000061'
  AND tool_id = '07500000-0000-4000-8000-000000000070';

-- Reproduce ambiguous historical ownership of one live AgentBay provider
-- sandbox. 101 must quarantine every claimant before creating uniqueness.
INSERT INTO agentbay_session_ledger (
  id, tenant_id, agent_id, user_id, chat_session_id,
  provider_session_id, image_type, purpose, status
) VALUES
  (
    '07500000-0000-4000-8000-000000000140',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000060',
    'migration-agentbay-lane-a', 'provider-duplicate-live',
    'browser', 'interactive', 'active'
  ),
  (
    '07500000-0000-4000-8000-000000000141',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000060',
    'migration-agentbay-lane-b', 'provider-duplicate-live',
    'browser', 'interactive', 'cleanup_required'
  );

-- Reproduce the pre-102 media reservation shape. The first row is an exact
-- one-to-one owner match and must be relinked. The second deliberately binds
-- a different Agent and must remain untouched for fail-closed review.
INSERT INTO credit_reservations (
  id, tenant_id, user_id, agent_id, action, modality, tier,
  provider, model, amount, status, ref_type, ref_id
) VALUES
  (
    '07500000-0000-4000-8000-000000000180',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000060',
    '07500000-0000-4000-8000-000000000061',
    'video', 'video', 'pro', 'minimax', 'MiniMax-Hailuo-2.3',
    49, 'provider_inflight', 'minimax_task', NULL
  ),
  (
    '07500000-0000-4000-8000-000000000182',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000060',
    '07500000-0000-4000-8000-000000000061',
    'video', 'video', 'pro', 'minimax', 'MiniMax-Hailuo-2.3',
    51, 'provider_inflight', 'minimax_task', NULL
  );
INSERT INTO media_generation_tasks (
  id, tenant_id, agent_id, user_id, reservation_id,
  provider, modality, model, status, metadata_path, output_path,
  request_metadata, completion_delivery_status,
  attempt_count, consecutive_error_count
) VALUES
  (
    '07500000-0000-4000-8000-000000000181',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000061',
    '07500000-0000-4000-8000-000000000060',
    '07500000-0000-4000-8000-000000000180',
    'minimax', 'video', 'MiniMax-Hailuo-2.3', 'submitted',
    'workspace/videos/relink-safe.json',
    'workspace/videos/relink-safe.mp4', '{}'::json, 'pending', 0, 0
  ),
  (
    '07500000-0000-4000-8000-000000000183',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000113',
    '07500000-0000-4000-8000-000000000060',
    '07500000-0000-4000-8000-000000000182',
    'minimax', 'video', 'MiniMax-Hailuo-2.3', 'submitted',
    'workspace/videos/relink-mismatch.json',
    'workspace/videos/relink-mismatch.mp4', '{}'::json, 'pending', 0, 0
  );
DELETE FROM tools WHERE id = '07500000-0000-4000-8000-000000000070';
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM tools
    WHERE id = '07500000-0000-4000-8000-000000000070'
      AND type = 'mcp' AND source = 'agent' AND tenant_id IS NULL
      AND is_default = false AND enabled = false
      AND mcp_server_url IS NULL AND config::jsonb = '{}'::jsonb
  ) THEN
    RAISE EXCEPTION 'quarantine guard allowed Tool mutation or deletion';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM tools
    WHERE id = '07500000-0000-4000-8000-000000000073'
      AND type = 'mcp' AND is_default = false AND enabled = false
      AND mcp_server_url IS NULL AND config::jsonb = '{}'::jsonb
  ) THEN
    RAISE EXCEPTION 'quarantine guard allowed unsafe MCP insert';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM agent_tools
    WHERE id = '07500000-0000-4000-8000-000000000074'
      AND enabled = false AND config::jsonb = '{}'::jsonb
  ) THEN
    RAISE EXCEPTION 'quarantine guard allowed unsafe assignment insert';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM agent_tools
    WHERE agent_id = '07500000-0000-4000-8000-000000000061'
      AND tool_id = '07500000-0000-4000-8000-000000000070'
      AND enabled = false AND config::jsonb = '{}'::jsonb
  ) THEN
    RAISE EXCEPTION 'quarantine guard allowed assignment identity mutation';
  END IF;
END $$;
SQL

PYTHONPATH=. .venv/bin/python -m app.scripts.secure_mcp_quarantine migration-smoke

psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
-- ``initial_schema`` follows current ORM metadata, but this lane reconstructs
-- the production schema before revision 106, where provider config was JSON.
ALTER TABLE identity_providers
ALTER COLUMN config TYPE JSON USING config::json;

INSERT INTO identity_providers (
  id, provider_type, name, is_active, sso_login_enabled, config, tenant_id
) VALUES
  (
    '07500000-0000-4000-8000-000000000199', 'web', 'Platform',
    true, false, '{}'::json,
    '07500000-0000-4000-8000-000000000002'
  ),
  (
    '07500000-0000-4000-8000-000000000200', 'github', 'Migration SSO',
    true, true,
    '{"client_id":"migration-client","client_secret":"migration-secret"}'::json,
    NULL
  );

-- Historical ``config`` was nullable JSON and application code did not
-- enforce an object shape.  Production preflight and revision 106 must both
-- reject every non-object JSON form without logging the stored value.
INSERT INTO identity_providers (
  id, provider_type, name, is_active, sso_login_enabled, config, tenant_id
) VALUES
  (
    '07500000-0000-4000-8000-000000000230', 'invalid_json_null',
    'Invalid JSON Null', false, false, 'null'::json, NULL
  ),
  (
    '07500000-0000-4000-8000-000000000231', 'invalid_json_array',
    'Invalid JSON Array', false, false, '[]'::json, NULL
  ),
  (
    '07500000-0000-4000-8000-000000000232', 'invalid_json_string',
    'Invalid JSON String', false, false, '"value"'::json, NULL
  ),
  (
    '07500000-0000-4000-8000-000000000233', 'invalid_json_number',
    'Invalid JSON Number', false, false, '1'::json, NULL
  );

INSERT INTO identities (
  id, email, username, password_hash,
  is_active, is_platform_admin, email_verified
) VALUES
  (
    '07500000-0000-4000-8000-000000000201',
    'sso-origin@example.com', 'sso-origin',
    '$2b$04$oIaUgIZ72SRhjByY./rb1ObilDkrdgd6nnAFu3rxTokIqqu5x8kRK',
    true, false, true
  ),
  (
    '07500000-0000-4000-8000-000000000202',
    'web-only@example.com', 'web-only',
    '$2b$04$wou6LFMDWc07CNoJC4jzI.MJFWyZCOARm97B4syMJpisI2EwlNEpy',
    true, false, true
  ),
  (
    '07500000-0000-4000-8000-000000000203',
    'web-provider-password@example.com', 'web-provider-password',
    '$2b$04$oIaUgIZ72SRhjByY./rb1ObilDkrdgd6nnAFu3rxTokIqqu5x8kRK',
    true, false, true
  ),
  (
    '07500000-0000-4000-8000-000000000204',
    'web-sso-linked@example.com', 'web-sso-linked',
    '$2b$04$wou6LFMDWc07CNoJC4jzI.MJFWyZCOARm97B4syMJpisI2EwlNEpy',
    true, false, true
  ),
  (
    '07500000-0000-4000-8000-000000000205',
    'web-pending@example.com', 'web-pending',
    '$2b$04$wou6LFMDWc07CNoJC4jzI.MJFWyZCOARm97B4syMJpisI2EwlNEpy',
    true, false, false
  ),
  (
    '07500000-0000-4000-8000-000000000206',
    'web-disabled-admin@example.com', 'web-disabled-admin',
    '$2b$04$wou6LFMDWc07CNoJC4jzI.MJFWyZCOARm97B4syMJpisI2EwlNEpy',
    true, false, false
  );

INSERT INTO users (
  id, identity_id, tenant_id, display_name, role, is_active,
  registration_source, preferred_chat_tier_revision,
  quota_message_limit, quota_message_period, quota_messages_used,
  quota_max_agents, quota_agent_ttl_hours
) VALUES
  (
    '07500000-0000-4000-8000-000000000211',
    '07500000-0000-4000-8000-000000000201',
    '07500000-0000-4000-8000-000000000002',
    'SSO Origin', 'member', true, 'github', 0, 50, 'permanent', 0, 2, 0
  ),
  (
    '07500000-0000-4000-8000-000000000212',
    '07500000-0000-4000-8000-000000000202',
    '07500000-0000-4000-8000-000000000002',
    'Web Only', 'member', true, 'web', 0, 50, 'permanent', 0, 2, 0
  ),
  (
    '07500000-0000-4000-8000-000000000213',
    '07500000-0000-4000-8000-000000000203',
    '07500000-0000-4000-8000-000000000002',
    'Web Provider Password', 'member', true, 'web', 0, 50, 'permanent', 0, 2, 0
  ),
  (
    '07500000-0000-4000-8000-000000000214',
    '07500000-0000-4000-8000-000000000204',
    '07500000-0000-4000-8000-000000000002',
    'Web SSO Linked', 'member', true, 'web', 0, 50, 'permanent', 0, 2, 0
  ),
  (
    '07500000-0000-4000-8000-000000000215',
    '07500000-0000-4000-8000-000000000205',
    '07500000-0000-4000-8000-000000000002',
    'Web Pending', 'member', false, 'web', 0, 50, 'permanent', 0, 2, 0
  ),
  (
    '07500000-0000-4000-8000-000000000216',
    '07500000-0000-4000-8000-000000000206',
    '07500000-0000-4000-8000-000000000002',
    'Web Disabled Admin', 'org_admin', false, 'web', 0, 50, 'permanent', 0, 2, 0
  );

INSERT INTO org_members (
  id, open_id, provider_id, name, title, department_path, status,
  tenant_id, user_id
) VALUES
  (
    '07500000-0000-4000-8000-000000000223', NULL,
    '07500000-0000-4000-8000-000000000199', 'Web Only', 'Platform User', '', 'active',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000212'
  ),
  (
    '07500000-0000-4000-8000-000000000224', NULL,
    '07500000-0000-4000-8000-000000000199', 'Web Provider Password', 'Platform User', '', 'active',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000213'
  ),
  (
    '07500000-0000-4000-8000-000000000225', NULL,
    '07500000-0000-4000-8000-000000000199', 'Web SSO Linked', 'Platform User', '', 'active',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000214'
  ),
  (
    '07500000-0000-4000-8000-000000000226', NULL,
    '07500000-0000-4000-8000-000000000199', 'Web Pending', 'Platform User', '', 'active',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000215'
  ),
  (
    '07500000-0000-4000-8000-000000000227', NULL,
    '07500000-0000-4000-8000-000000000199', 'Web Disabled Admin', 'Platform User', '', 'active',
    '07500000-0000-4000-8000-000000000002',
    '07500000-0000-4000-8000-000000000216'
  ),
  (
    '07500000-0000-4000-8000-000000000221', 'public-provider-id',
    '07500000-0000-4000-8000-000000000200', 'Provider Password', '', '', 'active',
    NULL,
    '07500000-0000-4000-8000-000000000213'
  ),
  (
    '07500000-0000-4000-8000-000000000222', 'linked-web-provider-id',
    '07500000-0000-4000-8000-000000000200', 'Linked Web Password', '', '', 'active',
    NULL,
    '07500000-0000-4000-8000-000000000214'
  );
SQL

psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL' | grep -Fx '4'
SELECT count(*)
FROM identity_providers
WHERE config IS NOT NULL
  AND pg_typeof(config)::text IN ('json', 'jsonb')
  AND jsonb_typeof(to_jsonb(config)) IS DISTINCT FROM 'object';
SQL

set +e
invalid_provider_upgrade_output="$(.venv/bin/alembic upgrade head 2>&1)"
invalid_provider_upgrade_status=$?
set -e
if [ "$invalid_provider_upgrade_status" -eq 0 ]; then
  echo "revision 106 accepted non-object identity provider config" >&2
  exit 1
fi
case "$invalid_provider_upgrade_output" in
  *'Non-object identity provider configs require operator cleanup before upgrade: count=4'*) ;;
  *)
    echo "revision 106 did not report the sanitized provider-config blocker" >&2
    printf '%s\n' "$invalid_provider_upgrade_output" >&2
    exit 1
    ;;
esac
unset invalid_provider_upgrade_output invalid_provider_upgrade_status
.venv/bin/alembic current | grep -F "disable_system_okr_automation"
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DELETE FROM identity_providers
WHERE id IN (
  '07500000-0000-4000-8000-000000000230',
  '07500000-0000-4000-8000-000000000231',
  '07500000-0000-4000-8000-000000000232',
  '07500000-0000-4000-8000-000000000233'
);

INSERT INTO users (
  id, identity_id, tenant_id, display_name, role, is_active,
  registration_source, preferred_chat_tier_revision,
  quota_message_limit, quota_message_period, quota_messages_used,
  quota_max_agents, quota_agent_ttl_hours
) VALUES
  (
    '07500000-0000-4000-8000-000000000228',
    '07500000-0000-4000-8000-000000000202', NULL,
    'Tenantless Duplicate One', 'member', true, 'migration-smoke',
    0, 50, 'permanent', 0, 2, 0
  ),
  (
    '07500000-0000-4000-8000-000000000229',
    '07500000-0000-4000-8000-000000000202', NULL,
    'Tenantless Duplicate Two', 'member', true, 'migration-smoke',
    0, 50, 'permanent', 0, 2, 0
  );
SQL

set +e
duplicate_tenantless_upgrade_output="$(.venv/bin/alembic upgrade head 2>&1)"
duplicate_tenantless_upgrade_status=$?
set -e
if [ "$duplicate_tenantless_upgrade_status" -eq 0 ]; then
  echo "revision 106 accepted duplicate tenantless memberships" >&2
  exit 1
fi
case "$duplicate_tenantless_upgrade_output" in
  *'Duplicate tenantless users(identity_id) rows must be audited before upgrade: sample_group_count=1'*) ;;
  *)
    echo "revision 106 did not report the sanitized tenantless-membership blocker" >&2
    printf '%s\n' "$duplicate_tenantless_upgrade_output" >&2
    exit 1
    ;;
esac
unset duplicate_tenantless_upgrade_output duplicate_tenantless_upgrade_status
.venv/bin/alembic current | grep -F "disable_system_okr_automation"
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DELETE FROM users
WHERE id IN (
  '07500000-0000-4000-8000-000000000228',
  '07500000-0000-4000-8000-000000000229'
);
SQL

.venv/bin/alembic upgrade deliverable_execution_shadow
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO user_tenant_onboardings (
  id, user_id, tenant_id, status, current_step, entry_mode,
  personal_assistant_agent_id
) VALUES (
  '07500000-0000-4000-8000-000000000240',
  '07500000-0000-4000-8000-000000000060',
  '07500000-0000-4000-8000-000000000002',
  'completed', 'completed', 'create',
  '07500000-0000-4000-8000-000000000061'
);
INSERT INTO agent_permissions (
  id, agent_id, scope_type, scope_id, access_level
) VALUES (
  '07500000-0000-4000-8000-000000000241',
  '07500000-0000-4000-8000-000000000061',
  'company', '07500000-0000-4000-8000-000000000002', 'manage'
);
DO $$
DECLARE
  foreign_key record;
BEGIN
  FOR foreign_key IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'media_generation_tasks'::regclass
      AND confrelid = 'agents'::regclass
      AND contype = 'f'
  LOOP
    EXECUTE format(
      'ALTER TABLE media_generation_tasks DROP CONSTRAINT %I',
      foreign_key.conname
    );
  END LOOP;
END $$;
ALTER TABLE media_generation_tasks
  ALTER COLUMN agent_id SET NOT NULL,
  ADD CONSTRAINT migration_smoke_media_agent_legacy_fkey
    FOREIGN KEY (agent_id) REFERENCES agents (id) ON DELETE CASCADE;
SQL
.venv/bin/alembic upgrade head
assert_product_line_release_repairs
.venv/bin/alembic downgrade deliverable_execution_shadow
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM agents
    WHERE id = '07500000-0000-4000-8000-000000000061'
      AND access_mode = 'company'
      AND company_access_level = 'use'
  ) OR NOT EXISTS (
    SELECT 1
    FROM agent_permissions
    WHERE id = '07500000-0000-4000-8000-000000000241'
      AND agent_id = '07500000-0000-4000-8000-000000000061'
      AND scope_type = 'company'
      AND scope_id = '07500000-0000-4000-8000-000000000002'
      AND access_level = 'manage'
  ) OR (
    SELECT count(*)
    FROM agent_permissions
    WHERE agent_id = '07500000-0000-4000-8000-000000000061'
  ) <> 1 THEN
    RAISE EXCEPTION 'private assistant downgrade did not restore exact legacy access';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'media_generation_tasks'
      AND column_name = 'agent_id'
      AND is_nullable = 'NO'
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'media_generation_tasks'::regclass
      AND conname = 'migration_smoke_media_agent_legacy_fkey'
      AND confdeltype = 'c'
  ) THEN
    RAISE EXCEPTION 'media task downgrade did not restore exact legacy retention policy';
  END IF;
END $$;
UPDATE agents
SET tenant_id = NULL
WHERE id = '07500000-0000-4000-8000-000000000061';
SQL
set +e
invalid_private_assistant_output="$(.venv/bin/alembic upgrade private_assistant_access 2>&1)"
invalid_private_assistant_status=$?
set -e
if [ "$invalid_private_assistant_status" -eq 0 ]; then
  echo "private assistant repair accepted a tenantless onboarding link" >&2
  exit 1
fi
case "$invalid_private_assistant_output" in
  *'unowned, cross-tenant, or deleted onboarding link'*) ;;
  *)
    echo "private assistant repair did not report its ownership blocker" >&2
    printf '%s\n' "$invalid_private_assistant_output" >&2
    exit 1
    ;;
esac
unset invalid_private_assistant_output invalid_private_assistant_status
.venv/bin/alembic current | grep -F "deliverable_execution_shadow"
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE agents
SET tenant_id = '07500000-0000-4000-8000-000000000002'
WHERE id = '07500000-0000-4000-8000-000000000061';
SQL
.venv/bin/alembic upgrade head
assert_product_line_release_repairs
restore_runtime_chat_foreign_key
assert_at_release_head
assert_sso_password_security

# The M3 Runtime repair must own only the exact NULL metadata it writes. A
# newer probe must survive a one-step downgrade, while contradictory evidence
# must block a later upgrade instead of being overwritten. First reproduce the
# production branch order: the legacy capability backfill has already run, then
# M3 rows arrive with all four Runtime capability fields still NULL.
.venv/bin/alembic downgrade agent_template_default_tools
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE llm_models
SET supports_tool_calling = NULL,
    tool_calling_capability_source = NULL,
    tool_calling_checked_at = NULL,
    tool_calling_error = NULL
WHERE id IN (
  '09300000-0000-4000-8000-000000000001',
  '09300000-0000-4000-8000-000000000002',
  '09300000-0000-4000-8000-000000000003'
);
SQL
.venv/bin/alembic upgrade head
assert_at_release_head
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF (
    SELECT count(*)
    FROM llm_models
    WHERE id IN (
      '09300000-0000-4000-8000-000000000001',
      '09300000-0000-4000-8000-000000000002',
      '09300000-0000-4000-8000-000000000003'
    )
      AND supports_tool_calling = true
      AND tool_calling_capability_source = 'builtin_registry'
      AND tool_calling_checked_at IS NOT NULL
      AND tool_calling_error IS NULL
      AND capabilities::jsonb ? '__reconcile_m3_runtime_caps_applied_at'
  ) <> 3 THEN
    RAISE EXCEPTION 'M3 Runtime repair did not mark exactly three owned rows';
  END IF;
END $$;

UPDATE llm_models
SET tool_calling_capability_source = 'probe',
    tool_calling_checked_at = clock_timestamp()
WHERE id = '09300000-0000-4000-8000-000000000001';
SQL
.venv/bin/alembic downgrade agent_template_default_tools
.venv/bin/alembic current | grep -F "agent_template_default_tools"
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM llm_models
    WHERE id = '09300000-0000-4000-8000-000000000001'
      AND supports_tool_calling = true
      AND tool_calling_capability_source = 'probe'
      AND tool_calling_checked_at IS NOT NULL
      AND tool_calling_error IS NULL
      AND NOT (capabilities::jsonb ? '__reconcile_m3_runtime_caps_applied_at')
  ) OR (
    SELECT count(*)
    FROM llm_models
    WHERE id IN (
      '09300000-0000-4000-8000-000000000002',
      '09300000-0000-4000-8000-000000000003'
    )
      AND supports_tool_calling IS NULL
      AND tool_calling_capability_source IS NULL
      AND tool_calling_checked_at IS NULL
      AND tool_calling_error IS NULL
      AND NOT (capabilities::jsonb ? '__reconcile_m3_runtime_caps_applied_at')
  ) <> 2 THEN
    RAISE EXCEPTION 'M3 Runtime repair downgrade overwrote newer probe evidence';
  END IF;
END $$;

UPDATE llm_models
SET supports_tool_calling = NULL,
    tool_calling_capability_source = NULL,
    tool_calling_checked_at = NULL,
    tool_calling_error = NULL
WHERE id IN (
  '09300000-0000-4000-8000-000000000001',
  '09300000-0000-4000-8000-000000000002',
  '09300000-0000-4000-8000-000000000003'
);
SQL
.venv/bin/alembic upgrade head
assert_at_release_head
.venv/bin/alembic downgrade agent_template_default_tools
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE llm_models
SET supports_tool_calling = false,
    tool_calling_capability_source = 'probe',
    tool_calling_checked_at = clock_timestamp(),
    tool_calling_error = NULL
WHERE id = '09300000-0000-4000-8000-000000000001';
SQL
set +e
contradictory_m3_upgrade_output="$(.venv/bin/alembic upgrade head 2>&1)"
contradictory_m3_upgrade_status=$?
set -e
if [ "$contradictory_m3_upgrade_status" -eq 0 ]; then
  echo "M3 Runtime repair accepted contradictory probe evidence" >&2
  exit 1
fi
case "$contradictory_m3_upgrade_output" in
  *'Refusing MiniMax-M3 Runtime capability repair:'*) ;;
  *)
    echo "M3 Runtime repair did not report its sanitized ownership blocker" >&2
    printf '%s\n' "$contradictory_m3_upgrade_output" >&2
    exit 1
    ;;
esac
unset contradictory_m3_upgrade_output contradictory_m3_upgrade_status
.venv/bin/alembic current | grep -F "agent_template_default_tools"
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE llm_models
SET supports_tool_calling = NULL,
    tool_calling_capability_source = NULL,
    tool_calling_checked_at = NULL,
    tool_calling_error = NULL
WHERE id = '09300000-0000-4000-8000-000000000001';
SQL
.venv/bin/alembic upgrade head
assert_at_release_head

# Historical rows can contain a username that shadows another Identity's
# email. The runtime must resolve the ownership-bearing email deterministically
# instead of raising MultipleResultsFound or authenticating the alias owner.
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO identities (
  id, email, username, password_hash, password_login_enabled,
  auth_version, is_active, is_platform_admin, email_verified
) VALUES
  (
    '07500000-0000-4000-8000-000000000230',
    'namespace-owner@example.test', 'namespace-owner', NULL, false,
    0, true, false, true
  ),
  (
    '07500000-0000-4000-8000-000000000231',
    'namespace-shadow@example.test', 'namespace-owner@example.test', NULL, false,
    0, true, false, true
  );
SQL
PYTHONPATH=. .venv/bin/python - <<'PY'
import asyncio

from app.dao.identity_dao import identity_dao


async def main() -> None:
    identity = await identity_dao.get_by_login_identifier(
        "namespace-owner@example.test"
    )
    assert identity is not None
    assert str(identity.id) == "07500000-0000-4000-8000-000000000230"


asyncio.run(main())
PY
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL' | grep -Fx '0|0'
SELECT
  (
    SELECT count(*)
    FROM identity_providers
    WHERE config IS NOT NULL
      AND pg_typeof(config)::text IN ('json', 'jsonb')
      AND jsonb_typeof(to_jsonb(config)) IS DISTINCT FROM 'object'
  )::text
  || '|'
  || (
    SELECT count(*)
    FROM identity_providers
    WHERE config IS NOT NULL
      AND pg_typeof(config)::text IN ('text', 'character varying')
      AND CAST(config AS text) NOT LIKE 'enc:idp:v1:%'
  )::text;
SQL
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_identity_provider_secrets

# A same-schema redeploy must authenticate existing Text envelopes before
# maintenance. Prefix-only checks are insufficient: corrupt one copied
# envelope, prove the candidate verifier rejects it, then restore the fixture.
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO identity_providers (
  id, provider_type, name, is_active, sso_login_enabled, config, tenant_id
)
SELECT
  '07500000-0000-4000-8000-000000000234', 'tampered_envelope_probe',
  'Tampered Envelope Probe', false, false, config || 'x', NULL
FROM identity_providers
WHERE id = '07500000-0000-4000-8000-000000000200';
SQL
set +e
tampered_envelope_output="$(
  PYTHONPATH=. .venv/bin/python -m app.scripts.verify_identity_provider_secrets 2>&1
)"
tampered_envelope_status=$?
set -e
if [ "$tampered_envelope_status" -eq 0 ]; then
  echo "identity provider verifier accepted a tampered envelope" >&2
  exit 1
fi
case "$tampered_envelope_output" in
  *'identity provider secret envelope authentication or decoding failed'*) ;;
  *)
    echo "identity provider verifier did not report a sanitized authentication failure" >&2
    exit 1
    ;;
esac
case "$tampered_envelope_output" in
  *'migration-secret'*)
    echo "identity provider verifier exposed a stored credential" >&2
    exit 1
    ;;
esac
unset tampered_envelope_output tampered_envelope_status
psql --host "$db_host" --port "$db_port" --username "$db_user" \
  --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DELETE FROM identity_providers
WHERE id = '07500000-0000-4000-8000-000000000234';
SQL
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_identity_provider_secrets

psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM credit_reservations
    WHERE id = '07500000-0000-4000-8000-000000000180'
      AND ref_type = 'media_task'
      AND ref_id = '07500000-0000-4000-8000-000000000181'
  ) THEN
    RAISE EXCEPTION '102 did not relink the exact legacy media owner';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM credit_reservations
    WHERE id = '07500000-0000-4000-8000-000000000182'
      AND ref_type = 'minimax_task'
      AND ref_id IS NULL
  ) THEN
    RAISE EXCEPTION '102 guessed ownership for a mismatched legacy media row';
  END IF;
  IF (
    SELECT count(*)
    FROM agentbay_session_ledger
    WHERE id IN (
      '07500000-0000-4000-8000-000000000140',
      '07500000-0000-4000-8000-000000000141'
    )
      AND status = 'provider_identity_collision'
      AND close_reason = 'provider_identity_collision'
      AND context::jsonb ->> 'provider_identity_collision_ledger_id'
        = '07500000-0000-4000-8000-000000000140'
  ) <> 2 THEN
    RAISE EXCEPTION '101 did not quarantine every duplicate provider owner';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM agentbay_session_ledger
    WHERE id = '07500000-0000-4000-8000-000000000140'
      AND provider_session_id = 'provider-duplicate-live'
  ) OR NOT EXISTS (
    SELECT 1
    FROM agentbay_session_ledger
    WHERE id = '07500000-0000-4000-8000-000000000141'
      AND provider_session_id IS NULL
  ) THEN
    RAISE EXCEPTION '101 did not retain exactly one canonical provider identity';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'agentbay_session_ledger'
      AND indexname = 'uq_agentbay_live_provider_session_id'
      AND indexdef LIKE '%UNIQUE%'
      AND indexdef LIKE '%provider_identity_collision%'
  ) THEN
    RAISE EXCEPTION '101 live provider ownership unique index is missing';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM notifications
    WHERE title = 'Automatic triggers paused for safety review'
  ) THEN
    RAISE EXCEPTION '101 left a stale global automation pause notice';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM notifications
    WHERE title = 'User automation security upgrade completed'
      AND body LIKE 'Durable user triggers and approved actions can run again.%'
      AND body LIKE '%schedules and todo tasks are retained but automatic execution remains paused%'
  ) THEN
    RAISE EXCEPTION '101 published an inaccurate automation availability notice';
  END IF;
  BEGIN
    INSERT INTO agentbay_session_ledger (
      id, tenant_id, agent_id, user_id, chat_session_id,
      provider_session_id, image_type, purpose, status
    ) VALUES (
      '07500000-0000-4000-8000-000000000145',
      '07500000-0000-4000-8000-000000000002',
      '07500000-0000-4000-8000-000000000061',
      '07500000-0000-4000-8000-000000000060',
      'migration-agentbay-reuse-attempt', 'provider-duplicate-live',
      'browser', 'interactive', 'active'
    );
    RAISE EXCEPTION '101 allowed a new live claim for quarantined provider identity';
  EXCEPTION
    WHEN unique_violation THEN NULL;
  END;
  IF NOT EXISTS (
    SELECT 1
    FROM agent_triggers
    WHERE id = '07500000-0000-4000-8000-000000000110'
      AND config = '{"minutes":5}'::jsonb
      AND is_enabled = false
  ) THEN
    RAISE EXCEPTION '099 trusted a forged historical server-context marker';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM agent_triggers
    WHERE id = '07500000-0000-4000-8000-000000000111'
      AND config = '{"minutes":10}'::jsonb
      AND is_enabled = false
  ) THEN
    RAISE EXCEPTION '099 retroactively trusted structurally valid legacy routing';
  END IF;
  IF (
    SELECT count(*)
    FROM notifications
    WHERE ref_id IN (
      '07500000-0000-4000-8000-000000000110'::uuid,
      '07500000-0000-4000-8000-000000000111'::uuid,
      '07500000-0000-4000-8000-000000000114'::uuid
    )
      AND type = 'system'
  ) <> 3 THEN
    RAISE EXCEPTION '099 did not make legacy trigger quarantine observable';
  END IF;
  IF (
    SELECT count(*)
    FROM chat_sessions
    WHERE source_channel = 'agent'
      AND agent_id = '07500000-0000-4000-8000-000000000061'
      AND peer_agent_id = '07500000-0000-4000-8000-000000000113'
      AND user_id IN (
        '07500000-0000-4000-8000-000000000060'::uuid,
        '07500000-0000-4000-8000-000000000112'::uuid
      )
  ) <> 2 THEN
    RAISE EXCEPTION '099 did not partition legacy A2A sessions by owner';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM chat_messages AS message
    JOIN chat_sessions AS session
      ON message.conversation_id = session.id::text
    WHERE message.id IN (
      '07500000-0000-4000-8000-000000000130'::uuid,
      '07500000-0000-4000-8000-000000000131'::uuid,
      '07500000-0000-4000-8000-000000000132'::uuid
    )
      AND message.user_id <> session.user_id
  ) THEN
    RAISE EXCEPTION '099 left mixed-owner A2A messages readable together';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM chat_sessions
    WHERE id = '07500000-0000-4000-8000-000000000121'
  ) THEN
    RAISE EXCEPTION '099 retained reversed duplicate A2A session';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM agent_triggers
    WHERE id = '07500000-0000-4000-8000-000000000114'
      AND config ->> 'expected_conversation_id' =
          '07500000-0000-4000-8000-000000000120'
      AND NOT (config ? '_origin_session_id')
      AND is_enabled = false
  ) THEN
    RAISE EXCEPTION '099 stranded or trusted a legacy trigger session reference';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM agent_triggers
    WHERE id = '07500000-0000-4000-8000-000000000115'
      AND is_enabled = true
  ) OR NOT EXISTS (
    SELECT 1
    FROM agent_schedules
    WHERE id = '07500000-0000-4000-8000-000000000116'
      AND is_enabled = true
      AND next_run_at IS NOT NULL
  ) THEN
    RAISE EXCEPTION '099 destroyed trusted trigger or schedule desired state';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM gateway_messages AS message
    JOIN chat_sessions AS session
      ON message.conversation_id = session.id::text
    WHERE message.id IN (
      '07500000-0000-4000-8000-000000000140'::uuid,
      '07500000-0000-4000-8000-000000000141'::uuid
    )
      AND message.sender_user_id <> session.user_id
  ) THEN
    RAISE EXCEPTION '099 stranded gateway messages in another owner lane';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM media_generation_tasks AS task
    JOIN chat_sessions AS session ON session.id = task.origin_session_id
    WHERE task.id = '07500000-0000-4000-8000-000000000142'
      AND task.user_id = session.user_id
  ) THEN
    RAISE EXCEPTION '099 lost media completion session ownership';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM workspace_file_revisions AS revision
    JOIN chat_sessions AS session ON revision.session_id = session.id::text
    WHERE revision.id = '07500000-0000-4000-8000-000000000143'
      AND revision.actor_id = session.user_id
  ) OR NOT EXISTS (
    SELECT 1
    FROM workspace_edit_locks AS lock
    JOIN chat_sessions AS session ON lock.session_id = session.id::text
    WHERE lock.id = '07500000-0000-4000-8000-000000000144'
      AND lock.user_id = session.user_id
  ) THEN
    RAISE EXCEPTION '099 stranded workspace session references';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM trigger_executions AS execution
    JOIN chat_sessions AS session
      ON execution.payload ->> '_a2a_session_id' = session.id::text
    WHERE execution.id IN (
      '07500000-0000-4000-8000-000000000150'::uuid,
      '07500000-0000-4000-8000-000000000151'::uuid
    )
      AND execution.payload ->> '_origin_user_id' <> session.user_id::text
  ) THEN
    RAISE EXCEPTION '099 stranded trigger execution in another owner lane';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM trigger_executions
    WHERE id = '07500000-0000-4000-8000-000000000153'
      AND status = 'failed'
      AND started_at IS NULL
      AND lease_owner IS NULL
      AND lease_expires_at IS NULL
      AND last_error = 'Automatic trigger execution paused by RC5 release policy'
  ) THEN
    RAISE EXCEPTION '099 did not serialize newer processing before the global pause';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'chat_sessions'
      AND indexname = 'uq_chat_sessions_a2a_owner'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'trigger_executions'
      AND indexname = 'uq_trigger_executions_processing_agent'
  ) THEN
    RAISE EXCEPTION '099 did not recreate release serialization indexes';
  END IF;
END $$;
SQL

# Reproduce a production-era administrator fallback that already points to the
# 093 M3 route. 100 must not select it as M3's fallback and create a 2-cycle.
.venv/bin/alembic downgrade trigger_privacy_serial
assert_legacy_channel_config_downgraded
assert_sso_password_security_downgraded
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE llm_models
SET supports_vision = true
WHERE id = '07500000-0000-4000-8000-000000000023';
INSERT INTO model_routes (
  id, saas_tier, modality, llm_model_id, priority, fallback_route_id, enabled
) VALUES (
  '07500000-0000-4000-8000-000000000026', 'lite', 'image',
  '07500000-0000-4000-8000-000000000023', 999,
  '09300000-0000-4000-8000-000000000102', true
);
SQL
.venv/bin/alembic upgrade head
assert_at_release_head
assert_sso_password_security
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO llm_models (
  id, provider, model, api_key_encrypted, label, enabled, supports_vision,
  modality, modalities, tier
) VALUES
  (
    '07500000-0000-4000-8000-000000000027', 'minimax', 'conflicting-capability',
    'migration-smoke-placeholder', 'Conflicting Capability', true, false,
    'video', '["text"]'::json, 'basic'
  ),
  (
    '07500000-0000-4000-8000-000000000028', 'minimax', 'legacy-vision-alias',
    'migration-smoke-placeholder', 'Legacy Vision Alias', true, false,
    'text', '["vision"]'::json, 'basic'
  );
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM model_routes
    WHERE id = '09300000-0000-4000-8000-000000000102'
      AND fallback_route_id = '07500000-0000-4000-8000-000000000026'
  ) THEN
    RAISE EXCEPTION '100 created a reverse M3 fallback cycle';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM model_routes
    WHERE id = '09300000-0000-4000-8000-000000000102'
      AND priority > 999
      AND enabled = true
  ) THEN
    RAISE EXCEPTION '100 did not keep M3 as the exact top image route';
  END IF;
  BEGIN
    INSERT INTO model_routes (
      id, saas_tier, modality, llm_model_id, priority, enabled
    ) VALUES (
      '07500000-0000-4000-8000-000000000027', 'lite', 'video',
      '07500000-0000-4000-8000-000000000023', 77, true
    );
    RAISE EXCEPTION '100 allowed an enabled modality-incompatible route';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'enabled model route requires an enabled modality-compatible model%' THEN
        RAISE;
      END IF;
  END;
  BEGIN
    UPDATE llm_models
    SET enabled = false
    WHERE id = '09300000-0000-4000-8000-000000000001';
    RAISE EXCEPTION '100 allowed a routed model to be disabled';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'a globally routed model must remain platform-owned, enabled, modality-compatible, and connection-stable%' THEN
        RAISE;
      END IF;
  END;
  BEGIN
    UPDATE llm_models
    SET tenant_id = '07500000-0000-4000-8000-000000000002'
    WHERE id = '09300000-0000-4000-8000-000000000001';
    RAISE EXCEPTION '100 allowed a global route to adopt a tenant credential';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'a globally routed model must remain platform-owned, enabled, modality-compatible, and connection-stable%' THEN
        RAISE;
      END IF;
  END;
  BEGIN
    UPDATE llm_models
    SET provider = 'openai'
    WHERE id = '09300000-0000-4000-8000-000000000001';
    RAISE EXCEPTION '100 allowed routed model connection identity to change';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'a globally routed model must remain platform-owned, enabled, modality-compatible, and connection-stable%' THEN
        RAISE;
      END IF;
  END;
  BEGIN
    INSERT INTO model_routes (
      id, saas_tier, modality, llm_model_id, priority, enabled
    ) VALUES (
      '07500000-0000-4000-8000-000000000029', 'lite', 'video',
      '07500000-0000-4000-8000-000000000027', 76, true
    );
    RAISE EXCEPTION '100 allowed singular modality to override declared modalities';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'enabled model route requires an enabled modality-compatible model%' THEN
        RAISE;
      END IF;
  END;
  INSERT INTO model_routes (
    id, saas_tier, modality, llm_model_id, priority, enabled
  ) VALUES (
    '07500000-0000-4000-8000-000000000029', 'lite', 'image',
    '07500000-0000-4000-8000-000000000028', 76, true
  );
  BEGIN
    UPDATE model_routes
    SET enabled = false
    WHERE id = '09300000-0000-4000-8000-000000000102';
    RAISE EXCEPTION '100 allowed an active fallback target to be disabled';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'active fallback target cannot be disabled or moved to another slot%' THEN
        RAISE;
      END IF;
  END;
  BEGIN
    DELETE FROM model_routes
    WHERE id = '09300000-0000-4000-8000-000000000102';
    RAISE EXCEPTION '100 allowed a referenced fallback target to be deleted';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'model route remains referenced as a fallback target%' THEN
        RAISE;
      END IF;
  END;
  BEGIN
    UPDATE model_routes
    SET fallback_route_id = '07500000-0000-4000-8000-000000000026'
    WHERE id = '09300000-0000-4000-8000-000000000102';
    RAISE EXCEPTION '100 allowed a direct-SQL fallback cycle';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE 'model route fallback cycle detected%' THEN
        RAISE;
      END IF;
  END;
END $$;
DELETE FROM model_routes
WHERE id = '07500000-0000-4000-8000-000000000029';
DELETE FROM model_routes
WHERE id = '07500000-0000-4000-8000-000000000026';
DELETE FROM llm_models
WHERE id IN (
  '07500000-0000-4000-8000-000000000027',
  '07500000-0000-4000-8000-000000000028'
);
UPDATE llm_models
SET supports_vision = false
WHERE id = '07500000-0000-4000-8000-000000000023';
SQL

# 095 must turn its trusted bypass back off before commit; the still-pending
# guard therefore continues to reject an ordinary writer after migration.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE tools
SET enabled = true,
    mcp_server_url = 'https://post-migration-writer.example/mcp',
    config = '{"api_key":"post-migration-secret"}'::json
WHERE id = '07500000-0000-4000-8000-000000000070';
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM tools
    WHERE id = '07500000-0000-4000-8000-000000000070'
      AND tenant_id = '07500000-0000-4000-8000-000000000002'
      AND enabled = false AND mcp_server_url IS NULL
      AND config::jsonb = '{}'::jsonb
  ) THEN
    RAISE EXCEPTION '095 bypass leaked beyond the trusted migration';
  END IF;
END $$;
SQL

PYTHONPATH=. .venv/bin/python ../scripts/extract-deploy-mcp-sql.py restore | \
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$db_name" --set ON_ERROR_STOP=1 \
    --set snapshot_id=migration-smoke
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-secured
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py run-seeder-and-assert
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-agent-tool-upsert
PYTHONPATH=. .venv/bin/python ../scripts/mcp-import-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/plan-update-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/media-remediation-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/billing-reconciliation-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/agentbay-identity-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/plaza-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/channel-config-encryption-postgres-smoke.py \
  --require-legacy-fixture
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_channel_secrets
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_identity_provider_secrets

psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF to_regclass('public.media_generation_tasks') IS NULL THEN
    RAISE EXCEPTION 'missing media_generation_tasks';
  END IF;
  IF to_regclass('public.notifications') IS NULL THEN
    RAISE EXCEPTION 'missing notifications required by media completion';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'media_generation_tasks'
      AND indexname = 'ix_media_generation_due'
  ) THEN
    RAISE EXCEPTION 'missing media generation due index';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'media_generation_tasks'
      AND column_name = 'consecutive_error_count'
  ) THEN
    RAISE EXCEPTION 'missing bounded media recovery counter';
  END IF;
  IF (
    SELECT count(*) FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'media_generation_tasks'
      AND column_name IN (
        'origin_session_id', 'completion_message_id', 'output_size',
        'completion_delivery_status', 'realtime_attempt_count',
        'realtime_next_attempt_at', 'realtime_published_at',
        'realtime_last_error'
      )
  ) <> 8 OR NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'media_generation_tasks'::regclass
      AND conname = 'ck_media_generation_completion_delivery_status'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'media_generation_tasks'
      AND indexname = 'ix_media_generation_completion_outbox'
  ) THEN
    RAISE EXCEPTION 'missing durable media completion delivery contract';
  END IF;
  IF to_regclass('public.production_issues') IS NULL
    OR to_regclass('public.production_issue_events') IS NULL
    OR to_regclass('public.production_issue_alert_deliveries') IS NULL THEN
    RAISE EXCEPTION 'missing production issue monitoring tables';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'production_issues'
      AND indexname = 'ix_production_issues_status_last_seen'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'production_issue_events'
      AND indexname = 'ix_production_issue_events_issue_created'
  ) THEN
    RAISE EXCEPTION 'missing production issue monitoring indexes';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'production_issue_alert_deliveries'
      AND indexname = 'ix_production_issue_alert_delivery_due'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'production_issues'
      AND column_name = 'alert_epoch'
  ) THEN
    RAISE EXCEPTION 'missing durable production issue alert outbox contract';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'production_issue_alert_deliveries'::regclass
      AND conname = 'ck_production_issue_alert_delivery_state'
  ) THEN
    RAISE EXCEPTION 'missing production issue alert delivery state constraint';
  END IF;
  IF (
    SELECT count(*) FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'production_issue_alert_deliveries'
      AND column_name IN (
        'attribution_version',
        'claim_worker_actor_id',
        'claim_worker_release_id',
        'claim_worker_release_commit',
        'delivered_by_worker_actor_id',
        'delivered_by_release_id',
        'delivered_by_release_commit'
      )
  ) <> 7 OR NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'production_issue_alert_deliveries'::regclass
      AND conname = 'ck_production_issue_alert_delivery_attribution'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'production_issue_alert_deliveries'::regclass
      AND conname = 'ck_production_issue_alert_delivery_attribution_version'
  ) THEN
    RAISE EXCEPTION 'missing production issue alert worker attribution contract';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'llm_credentials'
      AND column_name = 'modality_status'
  ) THEN
    RAISE EXCEPTION 'missing credential modality circuit state';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'llm_credentials'
      AND column_name = 'plan_tier'
      AND data_type = 'character varying'
  ) THEN
    RAISE EXCEPTION 'missing provider subscription plan tier';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users'
      AND column_name = 'preferred_chat_tier'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users'
      AND column_name = 'preferred_chat_tier_revision'
  ) THEN
    RAISE EXCEPTION 'missing versioned cross-Agent user chat tier preference';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'users'::regclass
      AND conname = 'ck_users_preferred_chat_tier'
  ) THEN
    RAISE EXCEPTION 'missing user chat tier check constraint';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'llm_models' AND column_name = 'verification_status'
  ) THEN
    RAISE EXCEPTION 'missing llm_models.verification_status';
  END IF;
  IF to_regclass('public.agentbay_session_ledger') IS NULL THEN
    RAISE EXCEPTION 'missing agentbay_session_ledger';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'agents' AND column_name = 'last_error'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'agents' AND column_name = 'last_error_at'
  ) THEN
    RAISE EXCEPTION 'missing agent error diagnostics columns';
  END IF;
  IF (
    SELECT count(*) FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'skills'
      AND indexname IN (
        'ux_skills_global_name',
        'ux_skills_global_folder_name',
        'ux_skills_tenant_name',
        'ux_skills_tenant_folder_name'
      )
  ) <> 4 THEN
    RAISE EXCEPTION 'missing tenant-scoped skill indexes';
  END IF;
  IF to_regclass('public.trigger_executions') IS NULL THEN
    RAISE EXCEPTION 'missing durable trigger execution ledger';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'chat_sessions' AND column_name = 'model_tier'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'chat_sessions' AND column_name = 'model_modality'
  ) THEN
    RAISE EXCEPTION 'missing chat session model selection columns';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'llm_credentials'
      AND column_name = 'status'
      AND column_default LIKE '%unverified%'
  ) THEN
    RAISE EXCEPTION 'unsafe llm_credentials.status default';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'agents'
      AND column_name = 'heartbeat_enabled'
      AND column_default = 'false'
  ) THEN
    RAISE EXCEPTION 'unsafe agents.heartbeat_enabled default';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM plans
    WHERE code = 'free'
      AND allowed_modalities::jsonb = '["text","image","video"]'::jsonb
      AND features::jsonb @> '{"generation_modalities":["image","audio","music","video"],"generation_tiers":["lite"]}'::jsonb
  ) THEN
    RAISE EXCEPTION 'missing free-plan media generation entitlement and M3 understanding inputs';
  END IF;
  IF (
    SELECT count(*) FROM plans
    WHERE code IN ('starter', 'pro', 'scale')
      AND allowed_modalities::jsonb = '["text","image","video"]'::jsonb
      AND features::jsonb ? 'generation_modalities'
      AND features::jsonb ? 'generation_tiers'
  ) <> 3 THEN
    RAISE EXCEPTION 'missing paid-plan media generation entitlements and M3 understanding inputs';
  END IF;
  IF (
    SELECT count(*)
    FROM model_routes mr
    JOIN llm_models lm ON lm.id = mr.llm_model_id
    WHERE mr.modality IN ('text', 'image', 'video')
      AND mr.enabled = true
      AND mr.saas_tier IN ('lite', 'pro', 'ultra')
      AND lm.model = 'MiniMax-M3'
      AND lm.supports_vision = true
      AND lm.modalities::jsonb @> '["text","image","video"]'::jsonb
      AND lm.supports_tool_calling = true
      AND lm.tool_calling_capability_source IN ('probe', 'builtin_registry')
      AND lm.tool_calling_checked_at IS NOT NULL
      AND lm.tool_calling_error IS NULL
  ) <> 9 THEN
    RAISE EXCEPTION 'MiniMax-M3 understanding routes lack verified Runtime tool-calling capability';
  END IF;
  IF (
    SELECT count(*) FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'trigger_executions'
      AND indexname IN (
        'ix_trigger_executions_agent_id',
        'ix_trigger_executions_trigger_id',
        'ix_trigger_executions_status_scheduled',
        'uq_trigger_execution_idempotency'
      )
  ) <> 4 THEN
    RAISE EXCEPTION 'missing trigger execution indexes';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'credit_transactions'
      AND indexname = 'uq_credit_transactions_idempotent_grants'
      AND indexdef LIKE '%refund%'
  ) THEN
    RAISE EXCEPTION 'refund grants are not protected by an idempotency index';
  END IF;
  IF (
    SELECT balance FROM credit_balances
    WHERE tenant_id = '07500000-0000-4000-8000-000000000002'
  ) <> (
    SELECT COALESCE(SUM(delta), 0) FROM credit_transactions
    WHERE tenant_id = '07500000-0000-4000-8000-000000000002'
  ) THEN
    RAISE EXCEPTION 'credit ledger drift was not reconciled';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM credit_transactions
    WHERE tenant_id = '07500000-0000-4000-8000-000000000002'
      AND reason = 'adjust'
      AND ref_type = 'migration'
      AND delta = 750
  ) THEN
    RAISE EXCEPTION 'missing legacy credit migration adjustment';
  END IF;
  IF (
    SELECT count(*) FROM tools
    WHERE name IN (
      'generate_image_minimax', 'generate_speech_minimax',
      'generate_music_minimax', 'generate_video_minimax'
    )
      AND config::jsonb ? 'lite_model'
      AND config::jsonb ? 'pro_model'
      AND config::jsonb ? 'ultra_model'
  ) <> 4 THEN
    RAISE EXCEPTION 'MiniMax media route matrix was not seeded';
  END IF;
  IF EXISTS (
    SELECT 1 FROM billing_rules
    WHERE (action = 'tts' OR modality = 'tts') AND enabled = true
  ) THEN
    RAISE EXCEPTION 'legacy TTS billing rule remains enabled';
  END IF;
END $$;

INSERT INTO skills (
  id, tenant_id, name, description, category, icon,
  folder_name, is_builtin, is_default
) VALUES
  (
    '07500000-0000-4000-8000-000000000010', NULL,
    'Migration Shared Skill', '', 'test', 'S',
    'migration-shared-skill', true, false
  ),
  (
    '07500000-0000-4000-8000-000000000011',
    '07500000-0000-4000-8000-000000000002',
    'Migration Shared Skill', '', 'test', 'S',
    'migration-shared-skill', false, false
  );

DO $$
BEGIN
  BEGIN
    INSERT INTO skills (
      id, tenant_id, name, description, category, icon,
      folder_name, is_builtin, is_default
    ) VALUES (
      '07500000-0000-4000-8000-000000000012',
      '07500000-0000-4000-8000-000000000002',
      'Migration Shared Skill', '', 'test', 'S',
      'migration-shared-skill-duplicate', false, false
    );
    RAISE EXCEPTION 'same-tenant duplicate skill name was accepted';
  EXCEPTION
    WHEN unique_violation THEN NULL;
  END;
END $$;

DELETE FROM skills
WHERE id IN (
  '07500000-0000-4000-8000-000000000010',
  '07500000-0000-4000-8000-000000000011',
  '07500000-0000-4000-8000-000000000012'
);
SQL

# The M3 data migration must be independently reversible: existing M2.x
# routes and administrator-owned catalog rows must survive a one-step rollback.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
UPDATE plans
SET allowed_modalities = '["text","image","video","audio"]'::jsonb
WHERE code = 'scale';

-- Reproduce legitimate post-deploy dependencies on revision-owned rows. The
-- downgrade must detach a custom fallback edge, preserve a still-referenced
-- model, and allow the same migration to be upgraded again afterwards.
INSERT INTO model_routes (
  id, saas_tier, modality, llm_model_id, priority, fallback_route_id, enabled
) VALUES (
  '07500000-0000-4000-8000-000000000025', 'lite', 'text',
  '07500000-0000-4000-8000-000000000023', 77,
  '09300000-0000-4000-8000-000000000101', true
);
UPDATE tenants
SET default_model_id = '09300000-0000-4000-8000-000000000001'
WHERE id = '07500000-0000-4000-8000-000000000002';
SQL
.venv/bin/alembic downgrade add_user_chat_tier_preference
.venv/bin/alembic current | grep -F "add_user_chat_tier_preference"
assert_legacy_channel_config_downgraded
assert_sso_password_security_downgraded
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-secured
# Exact v1.10.12 production shape: uniqueness was already enforced by a
# standalone index with the name later used by revision 096's constraint.
# The in-place upgrade must accept it, and a later downgrade must retain this
# pre-existing invariant rather than claiming ownership of it.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DROP INDEX IF EXISTS uq_agent_tools_agent_tool;
CREATE UNIQUE INDEX uq_agent_tools_agent_tool
ON agent_tools (agent_id, tool_id);
SQL
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF (
    SELECT count(*) FROM llm_models
    WHERE capabilities::jsonb @> '{"seed_revision":"seed_minimax_m3_understanding"}'::jsonb
  ) <> 1 OR NOT EXISTS (
    SELECT 1 FROM llm_models
    WHERE id = '09300000-0000-4000-8000-000000000001'
      AND capabilities::jsonb @> '{"seed_revision":"seed_minimax_m3_understanding"}'::jsonb
  ) THEN
    RAISE EXCEPTION 'downgrade did not preserve exactly the referenced M3 seed model';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM model_routes
    WHERE id = '07500000-0000-4000-8000-000000000025'
      AND fallback_route_id IS NULL
  ) THEN
    RAISE EXCEPTION 'administrator fallback route was not safely detached';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM tenants
    WHERE id = '07500000-0000-4000-8000-000000000002'
      AND default_model_id = '09300000-0000-4000-8000-000000000001'
  ) THEN
    RAISE EXCEPTION 'referenced M3 seed model dependency was not preserved';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM llm_models
    WHERE id = '07500000-0000-4000-8000-000000000023'
      AND provider = 'minimax'
      AND model = 'MiniMax-M3'
      AND label = 'MiniMax-M3 Lite (Platform)'
      AND supports_vision = false
      AND modality = 'text'
      AND modalities::jsonb = '["text"]'::jsonb
      AND max_output_tokens = 777
      AND capabilities::jsonb = '{"administrator_owned":true}'::jsonb
  ) THEN
    RAISE EXCEPTION 'administrator-owned M3 catalog row changed during migration rollback';
  END IF;
  IF (
    SELECT count(*)
    FROM model_routes mr
    JOIN llm_models lm ON lm.id = mr.llm_model_id
    WHERE mr.modality = 'text'
      AND mr.enabled = true
      AND (
        (mr.saas_tier = 'lite' AND lm.model = 'MiniMax-M2.5')
        OR (mr.saas_tier = 'pro' AND lm.model = 'MiniMax-M2.7')
        OR (mr.saas_tier = 'ultra' AND lm.model = 'MiniMax-M2.7-highspeed')
      )
  ) <> 3 THEN
    RAISE EXCEPTION 'legacy MiniMax text routes were not restored by downgrade';
  END IF;
  IF (
    SELECT count(*) FROM plans
    WHERE code IN ('free', 'starter', 'pro')
      AND allowed_modalities::jsonb = '["text"]'::jsonb
  ) <> 3 THEN
    RAISE EXCEPTION 'plan understanding modalities were not restored by downgrade';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM plans
    WHERE code = 'scale'
      AND allowed_modalities::jsonb = '["text","image","video","audio"]'::jsonb
  ) THEN
    RAISE EXCEPTION 'post-upgrade administrator plan edit was overwritten by downgrade';
  END IF;
  IF EXISTS (
    SELECT 1 FROM billing_rules
    WHERE id::text LIKE '09300000-0000-4000-8000-0000000002%'
  ) THEN
    RAISE EXCEPTION 'revision-owned M3 billing rules survived migration downgrade';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM billing_rules
    WHERE id = '07500000-0000-4000-8000-000000000024'
      AND action = 'chat'
      AND modality = 'video'
      AND tier = 'ultra'
      AND unit = 'call'
      AND credit_cost = 77
      AND priority = 93
  ) THEN
    RAISE EXCEPTION 'administrator-owned billing rule was deleted by migration rollback';
  END IF;
END $$;
SQL
.venv/bin/alembic upgrade head
assert_at_release_head
assert_sso_password_security
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'agent_tools'
      AND indexname = 'uq_agent_tools_agent_tool'
      AND indexdef = 'CREATE UNIQUE INDEX uq_agent_tools_agent_tool ON public.agent_tools USING btree (agent_id, tool_id)'
  ) OR EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'uq_agent_tools_agent_tool'
      AND conrelid = 'agent_tools'::regclass
  ) THEN
    RAISE EXCEPTION '096 did not preserve the production-era standalone unique index';
  END IF;
END $$;
SQL
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-secured
PYTHONPATH=. .venv/bin/python ../scripts/channel-config-encryption-postgres-smoke.py \
  --require-legacy-fixture --legacy-only
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_channel_secrets
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_identity_provider_secrets

AGENT_RUNTIME_V2_SOURCE_TYPES=trigger \
  PYTHONPATH=. .venv/bin/python ../scripts/a2a-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/media-generation-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/production-issue-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/approval-execution-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/chat-tier-preference-postgres-smoke.py

psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO users (
  id, tenant_id, display_name, role, is_active, registration_source,
  preferred_chat_tier, preferred_chat_tier_revision,
  quota_message_limit, quota_message_period, quota_messages_used,
  quota_max_agents, quota_agent_ttl_hours
) VALUES (
  '07500000-0000-4000-8000-000000000070',
  '07500000-0000-4000-8000-000000000002',
  'Migration Preference', 'member', true, 'migration-smoke',
  'ultra', 7, 50, 'permanent', 0, 2, 0
);
SQL

.venv/bin/alembic downgrade seed_saas_mvp_catalog
.venv/bin/alembic current | grep -F "seed_saas_mvp_catalog"
assert_legacy_channel_config_downgraded
assert_sso_password_security_downgraded
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL' | grep -Fx 'ultra|7'
SELECT preferred_chat_tier || '|' || preferred_chat_tier_revision
FROM users
WHERE id = '07500000-0000-4000-8000-000000000070';
SQL
.venv/bin/alembic upgrade head
assert_at_release_head
assert_sso_password_security
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-secured
PYTHONPATH=. .venv/bin/python ../scripts/channel-config-encryption-postgres-smoke.py \
  --require-legacy-fixture --legacy-only
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_channel_secrets
PYTHONPATH=. .venv/bin/python -m app.scripts.verify_identity_provider_secrets
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL' | grep -Fx 'ultra|7'
SELECT preferred_chat_tier || '|' || preferred_chat_tier_revision
FROM users
WHERE id = '07500000-0000-4000-8000-000000000070';
SQL
