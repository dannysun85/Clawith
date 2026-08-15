#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db_user="${PGUSER:-$USER}"
db_host="${PGHOST:-127.0.0.1}"
db_port="${PGPORT:-5432}"
suffix="${USER//[^a-zA-Z0-9_]/_}_$$"
db_name="clawith_g11_purge_${suffix}"
partial_db_name="${db_name}_partial"
storage_root="$(mktemp -d "${TMPDIR:-/tmp}/clawith-g11-purge.XXXXXX")"

cleanup() {
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
  dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$partial_db_name"
  case "$storage_root" in
    "${TMPDIR:-/tmp}"/clawith-g11-purge.*) rm -rf -- "$storage_root" ;;
    *) echo "refusing to remove unexpected storage root" >&2 ;;
  esac
}
trap cleanup EXIT

createdb --host "$db_host" --port "$db_port" --username "$db_user" "$partial_db_name"
(
  cd "$repo_root/backend"
  DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${partial_db_name}" \
    .venv/bin/alembic upgrade identity_mfa
  psql --host "$db_host" --port "$db_port" --username "$db_user" \
    --dbname "$partial_db_name" --set ON_ERROR_STOP=1 \
    --command 'DROP TABLE tenant_deletion_tombstones, tenant_deletion_holds, tenant_deletion_jobs CASCADE; CREATE TABLE tenant_deletion_jobs (id uuid PRIMARY KEY)'
  set +e
  partial_output="$(DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${partial_db_name}" .venv/bin/alembic upgrade head 2>&1)"
  partial_status=$?
  set -e
  if [[ $partial_status -eq 0 ]] || [[ "$partial_output" != *"partial tables"* ]]; then
    echo "partial tenant purge schema unexpectedly passed migration" >&2
    exit 1
  fi
)
dropdb --if-exists --host "$db_host" --port "$db_port" --username "$db_user" "$partial_db_name"

createdb --host "$db_host" --port "$db_port" --username "$db_user" "$db_name"
(
  cd "$repo_root/backend"
  export DATABASE_URL="postgresql+asyncpg://${db_user}@${db_host}:${db_port}/${db_name}"
  export ENVIRONMENT="test"
  export ALLOW_LOCAL_TENANT_PURGE="true"
  export STORAGE_BACKEND="local"
  export STORAGE_LOCAL_ROOT="$storage_root"
  export AGENT_DATA_DIR="$storage_root"
  .venv/bin/alembic upgrade head
  .venv/bin/alembic current | grep -F 'tenant_deletion_purge (head)'
  PYTHONPATH=. .venv/bin/python scripts/smoke_tenant_purge.py --storage-root "$storage_root"
)
