#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REMOTE_HOST="${REMOTE_HOST:-43.160.233.128}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-/Users/sun/.ssh/reeftotem_deploy_ed25519}"
APP_ROOT="${APP_ROOT:-/opt/astra-poc}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-astra-poc}"
COMPOSE_PROFILES="${COMPOSE_PROFILES:-}"
COMPOSE_PROFILES_ARG="${COMPOSE_PROFILES:-__none__}"
PUBLIC_URL="${PUBLIC_URL:-https://opc.reeftotem.ai}"
RUN_LOCAL_CHECKS="${RUN_LOCAL_CHECKS:-1}"
RUN_REMOTE_SMOKE="${RUN_REMOTE_SMOKE:-0}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
DRAIN_TIMEOUT_SECONDS="${DRAIN_TIMEOUT_SECONDS:-900}"

SMOKE_ENV_KEYS=(
    SMOKE_TENANT_EMAIL
    SMOKE_TENANT_PASSWORD
    SMOKE_PLATFORM_ADMIN_EMAIL
    SMOKE_PLATFORM_ADMIN_PASSWORD
)

SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
SSH_OPTS=(
    -i "$SSH_KEY"
    -o BatchMode=yes
    -o ConnectTimeout=15
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=6
)

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing required command: $1" >&2
        exit 1
    }
}

require_cmd git
require_cmd tar
require_cmd ssh
require_cmd scp

if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    for key in "${SMOKE_ENV_KEYS[@]}"; do
        if [ -z "${!key:-}" ]; then
            echo "RUN_REMOTE_SMOKE=1 requires $key" >&2
            exit 1
        fi
    done
fi

if [ ! -f "$SSH_KEY" ]; then
    echo "SSH key not found: $SSH_KEY" >&2
    exit 1
fi

if [ "$RUN_LOCAL_CHECKS" = "1" ]; then
    echo "[local] checking Alembic heads"
    ALEMBIC_HEADS="$(cd backend && uv run alembic heads)"
    ALEMBIC_HEAD_COUNT="$(printf '%s\n' "$ALEMBIC_HEADS" | grep -c '(head)')"
    if [ "$ALEMBIC_HEAD_COUNT" != "1" ]; then
        printf '%s\n' "$ALEMBIC_HEADS" >&2
        echo "expected exactly one Alembic head" >&2
        exit 1
    fi

    echo "[local] running focused backend release-gate tests"
    (cd backend && uv run pytest \
        tests/test_minimax_failover.py \
        tests/test_load_balancer.py \
        tests/test_credentials_verification.py \
        tests/test_media_generation_lifecycle.py \
        tests/test_media_incident_remediation.py \
        tests/test_production_issue_monitoring.py \
        tests/test_saas_admin.py \
        tests/test_subscription_lifecycle.py \
        tests/test_plan_crud.py \
        tests/test_subscription_billing.py \
        tests/test_production_deploy_contract.py \
        -q)

    echo "[local] building frontend"
    (cd frontend && npm run build)
fi

if [ -n "$(git status --short)" ] && [ "$ALLOW_DIRTY" != "1" ]; then
    echo "working tree is dirty; commit first or run with ALLOW_DIRTY=1 for an explicit dirty release" >&2
    exit 1
fi

COMMIT="$(git rev-parse --short HEAD)"
DIRTY_SUFFIX=""
if [ -n "$(git status --short)" ]; then
    DIRTY_SUFFIX="-dirty"
fi
VERSION="$(tr -d '[:space:]' < backend/VERSION)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
RELEASE_ID="${STAMP}-${COMMIT}-clawith-saas${DIRTY_SUFFIX}"
PACKAGE_DIR="$ROOT_DIR/.tmp/releases"
PACKAGE="$PACKAGE_DIR/${RELEASE_ID}.tar.gz"
SMOKE_ENV_FILE="$PACKAGE_DIR/${RELEASE_ID}.smoke.env"
SMOKE_ENV_REMOTE="/tmp/${RELEASE_ID}.smoke.env"
SMOKE_ENV_UPLOADED=0

cleanup_local() {
    rm -f "$PACKAGE" "$SMOKE_ENV_FILE"
    if [ "$SMOKE_ENV_UPLOADED" = "1" ]; then
        ssh "${SSH_OPTS[@]}" "$SSH_TARGET" rm -f "$SMOKE_ENV_REMOTE" >/dev/null 2>&1 || true
    fi
}
trap cleanup_local EXIT

mkdir -p "$PACKAGE_DIR"
echo "[local] packaging $RELEASE_ID"
COPYFILE_DISABLE=1 tar \
    --no-xattrs \
    --no-mac-metadata \
    --no-fflags \
    --exclude .git \
    --exclude .tmp \
    --exclude .data \
    --exclude .deepseek \
    --exclude .playwright-cli \
    --exclude .omx \
    --exclude output \
    --exclude .pytest_cache \
    --exclude .ruff_cache \
    --exclude .mypy_cache \
    --exclude '*/__pycache__' \
    --exclude '*.pyc' \
    --exclude node_modules \
    --exclude frontend/node_modules \
    --exclude frontend/dist \
    --exclude backend/.venv \
    --exclude .env \
    -czf "$PACKAGE" .

if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    (
        umask 077
        : > "$SMOKE_ENV_FILE"
        for key in "${SMOKE_ENV_KEYS[@]}"; do
            printf '%s=%q\n' "$key" "${!key}" >> "$SMOKE_ENV_FILE"
        done
    )
fi

echo "[remote] uploading package"
scp "${SSH_OPTS[@]}" "$PACKAGE" "${SSH_TARGET}:/tmp/${RELEASE_ID}.tar.gz"
if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    scp "${SSH_OPTS[@]}" "$SMOKE_ENV_FILE" "${SSH_TARGET}:${SMOKE_ENV_REMOTE}"
    SMOKE_ENV_UPLOADED=1
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" chmod 600 "$SMOKE_ENV_REMOTE"
fi

echo "[remote] deploying $RELEASE_ID"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" bash -s -- \
    "$APP_ROOT" "$RELEASE_ID" "$COMPOSE_PROJECT" "$COMPOSE_PROFILES_ARG" "$VERSION" "$COMMIT" "$PUBLIC_URL" "$RUN_REMOTE_SMOKE" "$SMOKE_ENV_REMOTE" "$DRAIN_TIMEOUT_SECONDS" <<'REMOTE_SCRIPT'
set -euo pipefail

APP_ROOT="$1"
RELEASE_ID="$2"
COMPOSE_PROJECT="$3"
COMPOSE_PROFILES="$4"
if [ "$COMPOSE_PROFILES" = "__none__" ]; then
    COMPOSE_PROFILES=""
fi
VERSION="$5"
COMMIT="$6"
PUBLIC_URL="$7"
RUN_REMOTE_SMOKE="$8"
SMOKE_ENV_FILE="$9"
DRAIN_TIMEOUT_SECONDS="${10}"

CURRENT="$APP_ROOT/current"
PREVIOUS="$(readlink -f "$CURRENT")"
RELEASE="$APP_ROOT/releases/$RELEASE_ID"
BACKUP="$APP_ROOT/backups/$RELEASE_ID"
PACKAGE="/tmp/${RELEASE_ID}.tar.gz"
COMPOSE_FILE="docker-compose.prod.yml"
ACTIVE_SLOT_FILE="$APP_ROOT/active-slot"
ACTIVE_SLOT="legacy"
if [ -f "$ACTIVE_SLOT_FILE" ]; then
    ACTIVE_SLOT="$(tr -d '[:space:]' < "$ACTIVE_SLOT_FILE")"
fi
case "$ACTIVE_SLOT" in
    a)
        OLD_PROJECT="${COMPOSE_PROJECT}-app-a"
        OLD_PORT=3008
        CANDIDATE_SLOT=b
        CANDIDATE_PORT=3009
        ;;
    b)
        OLD_PROJECT="${COMPOSE_PROJECT}-app-b"
        OLD_PORT=3009
        CANDIDATE_SLOT=a
        CANDIDATE_PORT=3008
        ;;
    *)
        ACTIVE_SLOT="legacy"
        OLD_PROJECT="$COMPOSE_PROJECT"
        OLD_PORT=3008
        CANDIDATE_SLOT=b
        CANDIDATE_PORT=3009
        ;;
esac
CANDIDATE_PROJECT="${COMPOSE_PROJECT}-app-${CANDIDATE_SLOT}"
CANDIDATE_BACKEND_ALIAS="${CANDIDATE_PROJECT}-backend"
JWT_ROTATION_MARKER="$APP_ROOT/.jwt-url-leak-rotation-v1"
ROTATE_JWT=0
if [ ! -f "$JWT_ROTATION_MARKER" ]; then
    ROTATE_JWT=1
fi

compose_project() {
    local project="$1"
    local env_file="$2"
    local compose_file="$3"
    shift 3
    if [ -n "$COMPOSE_PROFILES" ]; then
        docker compose --env-file "$env_file" --profile "$COMPOSE_PROFILES" -p "$project" -f "$compose_file" "$@"
    else
        docker compose --env-file "$env_file" -p "$project" -f "$compose_file" "$@"
    fi
}

if [ -z "$PREVIOUS" ] || [ ! -d "$PREVIOUS" ]; then
    echo "previous release not found from $CURRENT" >&2
    exit 1
fi

mkdir -p "$RELEASE" "$BACKUP"
tar -xzf "$PACKAGE" -C "$RELEASE"

if [ -f "$RELEASE/deploy/astra-poc/docker-compose.prod.yml" ]; then
    cp "$RELEASE/deploy/astra-poc/docker-compose.prod.yml" "$RELEASE/$COMPOSE_FILE"
else
    cp "$PREVIOUS/$COMPOSE_FILE" "$RELEASE/$COMPOSE_FILE"
fi

cp "$PREVIOUS/.env" "$RELEASE/.env"
if [ ! -d "$RELEASE/sidecars" ] && [ -d "$PREVIOUS/sidecars" ]; then
    cp -a "$PREVIOUS/sidecars" "$RELEASE/sidecars"
fi

python3 - "$RELEASE/.env" "$VERSION" "$COMMIT" "$RELEASE_ID" "$CANDIDATE_PORT" "$CANDIDATE_BACKEND_ALIAS" "$COMPOSE_PROJECT" "$ROTATE_JWT" <<'PY'
from pathlib import Path
import secrets
import sys

path = Path(sys.argv[1])
updates = {
    "ASTRA_RELEASE_VERSION": sys.argv[2],
    "ASTRA_RELEASE_COMMIT": sys.argv[3],
    "ASTRA_RELEASE_ID": sys.argv[4],
    "ASTRA_RELEASE": sys.argv[4],
    "COMMIT": sys.argv[3],
    "ALLOW_MIGRATION_FAILURE": "false",
    "HEARTBEAT_ENABLED": "false",
    "TRIGGER_DAEMON_ENABLED": "true",
    "OKR_AUTOMATION_ENABLED": "false",
    "TRIGGER_MAX_CONCURRENCY": "8",
    "TRIGGER_CLAIM_BATCH_SIZE": "16",
    "COMPANY_ASSIGNMENT_RUNNER_ENABLED": "false",
    "API_PROCESS_ROLE": "api,bootstrap",
    "WORKER_PROCESS_ROLE": "worker,connector",
    "FRONTEND_BIND": "127.0.0.1",
    "FRONTEND_PORT": sys.argv[5],
    "BACKEND_NETWORK_ALIAS": sys.argv[6],
    "API_UPSTREAM": f"{sys.argv[6]}:8000",
    "POSTGRES_VOLUME": f"{sys.argv[7]}_pgdata",
    "REDIS_VOLUME": f"{sys.argv[7]}_redisdata",
    "AGENT_DATA_VOLUME": f"{sys.argv[7]}_agentdata",
}
if sys.argv[8] == "1":
    updates["JWT_SECRET_KEY"] = secrets.token_urlsafe(64)
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
out = []
for line in lines:
    if "=" not in line or line.lstrip().startswith("#"):
        out.append(line)
        continue
    key = line.split("=", 1)[0].strip()
    if key in updates:
        out.append(f'{key}="{updates[key]}"')
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f'{key}="{value}"')
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

cp "$PREVIOUS/VERSION" "$BACKUP/VERSION.previous" 2>/dev/null || true
cp "$PREVIOUS/COMMIT" "$BACKUP/COMMIT.previous" 2>/dev/null || true
cp "$PREVIOUS/$COMPOSE_FILE" "$BACKUP/docker-compose.previous.yml"
cp "$PREVIOUS/.env" "$BACKUP/env.previous"
compose_project "$COMPOSE_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" ps > "$BACKUP/docker-ps.before.txt"

read_env() {
    local key="$1"
    local default="$2"
    local value
    value="$(grep -E "^${key}=" "$PREVIOUS/.env" | tail -1 | cut -d= -f2- | sed -E 's/^"//; s/"$//')"
    if [ -z "$value" ]; then
        value="$default"
    fi
    printf '%s' "$value"
}

POSTGRES_USER="$(read_env POSTGRES_USER astra)"
POSTGRES_DB="$(read_env POSTGRES_DB astra)"
echo "[remote] backing up database to $BACKUP/db.sql.gz"
compose_project "$COMPOSE_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" \
    exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" < /dev/null | gzip > "$BACKUP/db.sql.gz"

printf '%s\n' "$VERSION" > "$RELEASE/VERSION"
printf '%s\n' "$COMMIT" > "$RELEASE/COMMIT"
printf '%s\n' "$PREVIOUS" > "$RELEASE/PREVIOUS_RELEASE"

NGINX_SITE="/etc/nginx/sites-enabled/astra-poc.conf"
NGINX_BACKUP="$BACKUP/astra-poc.nginx.before.conf"
NGINX_SWITCHED=0
OLD_WORKER_STOPPED=0
OLD_APP_STOPPED=0
MIGRATION_APPLIED=0
PRE_MIGRATION_REVISION=""

rollback() {
    echo "[remote] rollback to $PREVIOUS" >&2
    set +e
    rm -f "$SMOKE_ENV_FILE"
    # Stop every candidate process that can touch the database before schema
    # rollback. If downgrade fails after cutover, keeping the candidate API
    # stopped deliberately fails closed instead of serving new code against an
    # older or partially restored schema.
    compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" stop worker backend
    if [ "$MIGRATION_APPLIED" = "1" ]; then
        echo "[remote] restoring database revision $PRE_MIGRATION_REVISION"
        if ! compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
            run --rm --no-deps -T --entrypoint alembic backend downgrade "$PRE_MIGRATION_REVISION" < /dev/null; then
            echo "[remote] CRITICAL: database downgrade failed; candidate API is stopped and workers remain quiesced" >&2
            echo "[remote] backup available at $BACKUP/db.sql.gz" >&2
            return
        fi
        MIGRATION_APPLIED=0
    fi
    if [ "$NGINX_SWITCHED" = "1" ] && [ -f "$NGINX_BACKUP" ]; then
        sudo cp "$NGINX_BACKUP" "$NGINX_SITE"
        sudo nginx -t >/dev/null && sudo systemctl reload nginx
    fi
    ln -sfnT "$PREVIOUS" "$CURRENT"
    compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" stop worker frontend backend
    if [ "$OLD_APP_STOPPED" = "1" ]; then
        compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" up -d --no-deps backend
        compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" up -d --no-deps frontend
    fi
    if [ "$OLD_WORKER_STOPPED" = "1" ]; then
        compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" up -d --no-deps worker
    fi
}
trap rollback ERR

echo "[remote] clearing inactive slot $CANDIDATE_SLOT"
compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" rm -sf worker frontend backend >/dev/null 2>&1 || true

echo "[remote] building candidate slot $CANDIDATE_SLOT"
compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" build backend frontend

echo "[remote] quiescing old worker before automation-state migrations"
compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" stop --timeout 90 worker
OLD_WORKER_STOPPED=1

PRE_MIGRATION_REVISION="$(
    compose_project "$COMPOSE_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" \
        exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
        'SELECT version_num FROM alembic_version ORDER BY version_num' < /dev/null
)"
if [ -z "$PRE_MIGRATION_REVISION" ] || printf '%s\n' "$PRE_MIGRATION_REVISION" | grep -q '[[:space:]]'; then
    echo "expected exactly one pre-migration Alembic revision, got: $PRE_MIGRATION_REVISION" >&2
    exit 1
fi
printf '%s\n' "$PRE_MIGRATION_REVISION" > "$BACKUP/alembic-revision.previous.txt"

echo "[remote] applying migrations before candidate startup"
compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
    run --rm --no-deps -T --entrypoint alembic backend upgrade head < /dev/null
MIGRATION_APPLIED=1

compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" up -d --no-deps backend
echo "[remote] waiting for candidate backend health"
for _ in $(seq 1 90); do
    CANDIDATE_BACKEND_ID="$(compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" ps -q backend)"
    if [ -n "$CANDIDATE_BACKEND_ID" ] && [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CANDIDATE_BACKEND_ID")" = "healthy" ]; then
        break
    fi
    sleep 2
done
CANDIDATE_BACKEND_ID="$(compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" ps -q backend)"
test -n "$CANDIDATE_BACKEND_ID"
test "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CANDIDATE_BACKEND_ID")" = "healthy"

compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" up -d --no-deps frontend
echo "[remote] waiting for candidate frontend health"
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${CANDIDATE_PORT}/api/health" >/dev/null; then
        break
    fi
    sleep 2
done
curl -fsS "http://127.0.0.1:${CANDIDATE_PORT}/api/health" >/dev/null
curl -fsS "http://127.0.0.1:${CANDIDATE_PORT}/api/version" | tee "$BACKUP/version.candidate.json"

echo "[remote] installing query-redacted access logging and switching traffic"
sudo cp "$NGINX_SITE" "$NGINX_BACKUP"
sudo python3 - "$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT" <<'PY'
from pathlib import Path
import re
import sys

site_path = Path(sys.argv[1])
old_port = sys.argv[2]
candidate_port = sys.argv[3]
text = site_path.read_text(encoding="utf-8")
pattern = rf"proxy_pass\s+http://127\.0\.0\.1:{re.escape(old_port)};"
text, replacements = re.subn(pattern, f"proxy_pass http://127.0.0.1:{candidate_port};", text)
if replacements != 1:
    raise SystemExit(
        f"expected exactly one production upstream on port {old_port}, found {replacements}"
    )
if "access.log astra_no_args" not in text:
    text = text.replace("server {", "server {\n    access_log /var/log/nginx/access.log astra_no_args;", 1)
site_path.write_text(text, encoding="utf-8")

log_format = Path("/etc/nginx/conf.d/00-astra-log-redaction.conf")
log_format.write_text(
    "log_format astra_no_args '$remote_addr - $remote_user [$time_local] "
    "\"$request_method $uri $server_protocol\" $status $body_bytes_sent "
    "\"$http_referer\" \"$http_user_agent\"';\n",
    encoding="utf-8",
)
PY
sudo nginx -t
ln -sfnT "$RELEASE" "$CURRENT"
sudo systemctl reload nginx
NGINX_SWITCHED=1

echo "[remote] verifying public cutover identity"
PUBLIC_READY=0
PUBLIC_HEALTH=""
PUBLIC_VERSION=""
for _ in $(seq 1 30); do
    PUBLIC_HEALTH="$(curl -fsS -H 'Cache-Control: no-cache' "$PUBLIC_URL/api/health?release=$RELEASE_ID" || true)"
    PUBLIC_VERSION="$(curl -fsS -H 'Cache-Control: no-cache' "$PUBLIC_URL/api/version?release=$RELEASE_ID" || true)"
    if python3 - "$PUBLIC_HEALTH" "$PUBLIC_VERSION" "$VERSION" "$COMMIT" 2>/dev/null <<'PY'
import json
import sys

health = json.loads(sys.argv[1])
version = json.loads(sys.argv[2])
expected_version = sys.argv[3]
expected_commit = sys.argv[4]
if health.get("status") != "ok" or health.get("version") != expected_version:
    raise SystemExit(1)
if version.get("version") != expected_version or version.get("commit") != expected_commit:
    raise SystemExit(1)
PY
    then
        PUBLIC_READY=1
        break
    fi
    sleep 1
done
printf '%s\n%s\n' "$PUBLIC_HEALTH" "$PUBLIC_VERSION"
if [ "$PUBLIC_READY" != "1" ]; then
    echo "public cutover did not expose expected release $VERSION/$COMMIT" >&2
    exit 1
fi
printf '%s\n' "$PUBLIC_HEALTH" > "$BACKUP/health.public.json"
printf '%s\n' "$PUBLIC_VERSION" > "$BACKUP/version.public.json"

compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" up -d --no-deps worker

echo "[remote] draining old application connections on port $OLD_PORT"
DRAINED=0
DEADLINE=$(( $(date +%s) + DRAIN_TIMEOUT_SECONDS ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    CONNECTIONS="$(ss -Htn state established | awk -v suffix=":${OLD_PORT}" '$5 ~ suffix "$" {count++} END {print count + 0}')"
    if [ "$CONNECTIONS" = "0" ]; then
        DRAINED=1
        break
    fi
    echo "[remote] waiting for $CONNECTIONS old connection(s) to drain"
    sleep 10
done

if [ "$DRAINED" = "1" ]; then
    compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" stop frontend backend
    OLD_APP_STOPPED=1
    rm -f "$APP_ROOT/pending-drain"
else
    printf '%s\n' "$OLD_PROJECT $OLD_PORT $PREVIOUS" > "$APP_ROOT/pending-drain"
    echo "[remote] old slot still has live connections; leaving it running for manual drain completion"
fi

{
    echo "candidate project: $CANDIDATE_PROJECT"
    compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" ps
    echo "data project: $COMPOSE_PROJECT"
    compose_project "$COMPOSE_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" ps postgres redis
} > "$BACKUP/docker-ps.after.txt"

if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    if [ ! -f "$SMOKE_ENV_FILE" ]; then
        echo "remote smoke environment file is missing" >&2
        exit 1
    fi
    set -a
    # This file is generated locally with bash-escaped values and mode 0600.
    source "$SMOKE_ENV_FILE"
    set +a
    rm -f "$SMOKE_ENV_FILE"
    scripts/subscription-production-smoke.sh --api-base "$PUBLIC_URL/api" --frontend-url "$PUBLIC_URL" | tee "$BACKUP/subscription-smoke.json"
fi

printf '%s\n' "$CANDIDATE_SLOT" > "$ACTIVE_SLOT_FILE"
printf '%s\n' "$RELEASE_ID" > "$APP_ROOT/active-release"
if [ "$ROTATE_JWT" = "1" ]; then
    touch "$JWT_ROTATION_MARKER"
fi

trap - ERR
sudo logrotate -f /etc/logrotate.d/nginx >/dev/null 2>&1 || true
sudo find /var/log/nginx -maxdepth 1 -type f -name 'access.log.*' -exec chmod 600 {} + >/dev/null 2>&1 || true
rm -f "$PACKAGE" "$SMOKE_ENV_FILE"
echo "[remote] release $RELEASE_ID deployed on slot $CANDIDATE_SLOT"
REMOTE_SCRIPT

echo "[done] deployed $RELEASE_ID to $PUBLIC_URL"
