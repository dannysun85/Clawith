#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db_name="clawith_migration_smoke_${USER//[^a-zA-Z0-9_]/_}_$$"
fresh_db_name="${db_name}_fresh"
db_user="${PGUSER:-$USER}"
db_host="${PGHOST:-127.0.0.1}"
db_port="${PGPORT:-5432}"

cleanup() {
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$fresh_db_name"
}
trap cleanup EXIT

createdb --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
createdb --host "$db_host" --port "$db_port" --username "$db_user" "$fresh_db_name"
export DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${db_name}"

cd "$repo_root/backend"

# The bootstrap revision reflects current ORM metadata, so a true empty-db
# install exercises a different path from the production-era reconstruction
# below. Both must reach the one release head without duplicate DDL.
DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${fresh_db_name}" \
  .venv/bin/alembic upgrade head
DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${fresh_db_name}" \
  .venv/bin/alembic current | grep -F "model_route_integrity (head)"

.venv/bin/alembic upgrade add_douyin_collab_publish_fields

# Simulate a deployment already stamped beyond revisions that were later
# inserted into historical migration order.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
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
# The bootstrap migration creates tables from current ORM metadata, while this
# smoke deliberately reconstructs the pre-096 production epoch. Remove the
# future constraint so duplicate historical grants can be seeded and 096 is
# proven to quarantine them before recreating uniqueness.
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
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
.venv/bin/alembic upgrade head
.venv/bin/alembic current | grep -F "model_route_integrity (head)"

psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
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
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'llm_credentials'
      AND column_name = 'modality_status'
  ) THEN
    RAISE EXCEPTION 'missing credential modality circuit state';
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
  ) <> 9 THEN
    RAISE EXCEPTION 'MiniMax-M3 text/image/video understanding routes were not seeded';
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
  '07500000-0000-4000-8000-000000000025', 'lite', 'custom-understanding',
  '07500000-0000-4000-8000-000000000023', 77,
  '09300000-0000-4000-8000-000000000101', true
);
UPDATE tenants
SET default_model_id = '09300000-0000-4000-8000-000000000001'
WHERE id = '07500000-0000-4000-8000-000000000002';
SQL
.venv/bin/alembic downgrade add_user_chat_tier_preference
.venv/bin/alembic current | grep -F "add_user_chat_tier_preference"
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-secured
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
.venv/bin/alembic current | grep -F "model_route_integrity (head)"
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-secured

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
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL' | grep -Fx 'ultra|7'
SELECT preferred_chat_tier || '|' || preferred_chat_tier_revision
FROM users
WHERE id = '07500000-0000-4000-8000-000000000070';
SQL
.venv/bin/alembic upgrade head
.venv/bin/alembic current | grep -F "model_route_integrity (head)"
PYTHONPATH=. .venv/bin/python ../scripts/code-execution-migration-postgres-smoke.py assert-secured
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL' | grep -Fx 'ultra|7'
SELECT preferred_chat_tier || '|' || preferred_chat_tier_revision
FROM users
WHERE id = '07500000-0000-4000-8000-000000000070';
SQL
