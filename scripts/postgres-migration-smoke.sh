#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db_name="clawith_migration_smoke_${USER//[^a-zA-Z0-9_]/_}_$$"
db_user="${PGUSER:-$USER}"
db_host="${PGHOST:-127.0.0.1}"
db_port="${PGPORT:-5432}"

cleanup() {
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
}
trap cleanup EXIT

createdb --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
export DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${db_name}"

cd "$repo_root/backend"
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

.venv/bin/alembic upgrade head
.venv/bin/alembic current | grep -F "disable_system_okr_automation (head)"
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
  IF to_regclass('public.production_issues') IS NULL
    OR to_regclass('public.production_issue_events') IS NULL THEN
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
.venv/bin/alembic current | grep -F "disable_system_okr_automation (head)"

PYTHONPATH=. .venv/bin/python ../scripts/a2a-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/media-generation-postgres-smoke.py
PYTHONPATH=. .venv/bin/python ../scripts/production-issue-postgres-smoke.py
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
.venv/bin/alembic current | grep -F "disable_system_okr_automation (head)"
psql --host "$db_host" --port "$db_port" --username "$db_user" --dbname "$db_name" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL' | grep -Fx 'ultra|7'
SELECT preferred_chat_tier || '|' || preferred_chat_tier_revision
FROM users
WHERE id = '07500000-0000-4000-8000-000000000070';
SQL
