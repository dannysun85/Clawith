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
require_cmd python3

case "$DRAIN_TIMEOUT_SECONDS" in
    ''|*[!0-9]*)
        echo "DRAIN_TIMEOUT_SECONDS must be a non-negative integer" >&2
        exit 1
        ;;
esac
if [ "$DRAIN_TIMEOUT_SECONDS" -gt 86400 ]; then
    echo "DRAIN_TIMEOUT_SECONDS must not exceed 86400" >&2
    exit 1
fi

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
        tests/test_worker_runtime_health.py \
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
NONCE="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
RELEASE_ID="${STAMP}-${COMMIT}-${NONCE}-clawith-saas${DIRTY_SUFFIX}"
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
CURRENT_TARGET=""
RELEASE="$APP_ROOT/releases/$RELEASE_ID"
BACKUP="$APP_ROOT/backups/$RELEASE_ID"
PACKAGE="/tmp/${RELEASE_ID}.tar.gz"
COMPOSE_FILE="docker-compose.prod.yml"
ACTIVE_SLOT_FILE="$APP_ROOT/active-slot"
ACTIVE_RELEASE_FILE="$APP_ROOT/active-release"
ACTIVE_STATE_FILE="$APP_ROOT/active-state"
CUTOVER_STATE_FILE="$APP_ROOT/cutover-state"
PENDING_DRAIN_FILE="$APP_ROOT/pending-drain"
NGINX_SITE="/etc/nginx/sites-enabled/astra-poc.conf"
NGINX_LOG_FORMAT="/etc/nginx/conf.d/00-astra-log-redaction.conf"

mkdir -p "$APP_ROOT"
if ! command -v flock >/dev/null 2>&1; then
    echo "flock is required for production deployment serialization" >&2
    exit 1
fi
exec 9>"$APP_ROOT/deploy.lock"
if ! flock -n 9; then
    echo "another production deployment is already running" >&2
    rm -f "$PACKAGE" "$SMOKE_ENV_FILE"
    exit 1
fi
CURRENT_TARGET="$(readlink -f "$CURRENT")"

write_atomic_line() {
    local path="$1"
    local value="$2"
    python3 - "$path" "$value" <<'PY_ATOMIC_LINE'
import os
from pathlib import Path
import tempfile
import sys

path = Path(sys.argv[1])
value = sys.argv[2]
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
temporary = Path(temporary_name)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
PY_ATOMIC_LINE
}

remove_durable_file() {
    python3 - "$1" <<'PY_REMOVE_FILE'
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
if path.exists():
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
PY_REMOVE_FILE
}

write_atomic_symlink() {
    local path="$1"
    local target="$2"
    python3 - "$path" "$target" <<'PY_ATOMIC_SYMLINK'
import os
from pathlib import Path
import tempfile
import sys

path = Path(sys.argv[1])
target = sys.argv[2]
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
os.close(fd)
os.unlink(temporary_name)
try:
    os.symlink(target, temporary_name)
    os.replace(temporary_name, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.lexists(temporary_name):
        os.unlink(temporary_name)
PY_ATOMIC_SYMLINK
}

commit_active_state() {
    local slot="$1"
    local release_id="$2"

    case "$slot" in
        a|b|legacy) ;;
        *) return 1 ;;
    esac
    case "$release_id" in
        ''|*[!A-Za-z0-9._-]*) return 1 ;;
    esac

    # active-state is the atomic authority. The two legacy files are retained
    # as operator-facing compatibility mirrors and may be healed after a crash.
    write_atomic_line "$ACTIVE_STATE_FILE" "slot=$slot release=$release_id" || return 1
    write_atomic_line "$ACTIVE_SLOT_FILE" "$slot" || return 1
    write_atomic_line "$ACTIVE_RELEASE_FILE" "$release_id"
}

load_active_state() {
    local state
    local mirrored_slot
    local mirrored_release

    RECORDED_SLOT="legacy"
    ACTIVE_RELEASE_ID=""
    ACTIVE_STATE_PRESENT=0
    ACTIVE_STATE_SOURCE="bootstrap"

    if [ -e "$ACTIVE_STATE_FILE" ] || [ -L "$ACTIVE_STATE_FILE" ]; then
        if [ ! -f "$ACTIVE_STATE_FILE" ] || [ -L "$ACTIVE_STATE_FILE" ] || \
            [ ! -s "$ACTIVE_STATE_FILE" ]; then
            echo "invalid active state file" >&2
            return 1
        fi
        state="$(cat "$ACTIVE_STATE_FILE")" || return 1
        if [[ "$state" == *$'\n'* ]] || \
            [[ ! "$state" =~ ^slot=(a|b|legacy)[[:space:]]release=([A-Za-z0-9._-]+)$ ]]; then
            echo "invalid active state format" >&2
            return 1
        fi
        RECORDED_SLOT="${BASH_REMATCH[1]}"
        ACTIVE_RELEASE_ID="${BASH_REMATCH[2]}"
        ACTIVE_STATE_PRESENT=1
        ACTIVE_STATE_SOURCE="atomic"
        return 0
    fi

    if [ -e "$ACTIVE_SLOT_FILE" ] || [ -L "$ACTIVE_SLOT_FILE" ] || \
        [ -e "$ACTIVE_RELEASE_FILE" ] || [ -L "$ACTIVE_RELEASE_FILE" ]; then
        if [ ! -f "$ACTIVE_SLOT_FILE" ] || [ -L "$ACTIVE_SLOT_FILE" ] || \
            [ ! -s "$ACTIVE_SLOT_FILE" ] || [ ! -f "$ACTIVE_RELEASE_FILE" ] || \
            [ -L "$ACTIVE_RELEASE_FILE" ] || [ ! -s "$ACTIVE_RELEASE_FILE" ]; then
            echo "legacy active state is incomplete" >&2
            return 1
        fi
        mirrored_slot="$(cat "$ACTIVE_SLOT_FILE")" || return 1
        mirrored_release="$(cat "$ACTIVE_RELEASE_FILE")" || return 1
        if [[ "$mirrored_slot" == *$'\n'* ]] || \
            [[ ! "$mirrored_slot" =~ ^(a|b|legacy)$ ]]; then
            echo "invalid recorded active slot" >&2
            return 1
        fi
        if [[ "$mirrored_release" == *$'\n'* ]] || \
            [[ ! "$mirrored_release" =~ ^[A-Za-z0-9._-]+$ ]]; then
            echo "invalid recorded active release" >&2
            return 1
        fi
        RECORDED_SLOT="$mirrored_slot"
        ACTIVE_RELEASE_ID="$mirrored_release"
        ACTIVE_STATE_PRESENT=1
        ACTIVE_STATE_SOURCE="legacy-pair"
    fi
}

write_cutover_state() {
    write_atomic_line "$CUTOVER_STATE_FILE" "$1 slot=$2 release=$3"
}

slot_for_port() {
    case "$1" in
        3008) printf 'a' ;;
        3009) printf 'b' ;;
        *) return 1 ;;
    esac
}

port_for_slot() {
    case "$1" in
        a|legacy) printf '3008' ;;
        b) printf '3009' ;;
        *) return 1 ;;
    esac
}

release_payloads_match() {
    python3 - "$1" "$2" "$3" "$4" 2>/dev/null <<'PY'
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
}

wait_for_local_release() {
    local port="$1"
    local expected_version="$2"
    local expected_commit="$3"
    local cache_key="$4"
    local attempts="$5"
    local health
    local version
    for _ in $(seq 1 "$attempts"); do
        health="$(curl -fsS -H 'Cache-Control: no-cache' "http://127.0.0.1:${port}/api/health?release=$cache_key" || true)"
        version="$(curl -fsS -H 'Cache-Control: no-cache' "http://127.0.0.1:${port}/api/version?release=$cache_key" || true)"
        if release_payloads_match "$health" "$version" "$expected_version" "$expected_commit"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_for_public_release() {
    local expected_version="$1"
    local expected_commit="$2"
    local cache_key="$3"
    local attempts="$4"
    PUBLIC_HEALTH=""
    PUBLIC_VERSION=""
    for _ in $(seq 1 "$attempts"); do
        PUBLIC_HEALTH="$(curl -fsS -H 'Cache-Control: no-cache' "$PUBLIC_URL/api/health?release=$cache_key" || true)"
        PUBLIC_VERSION="$(curl -fsS -H 'Cache-Control: no-cache' "$PUBLIC_URL/api/version?release=$cache_key" || true)"
        if release_payloads_match "$PUBLIC_HEALTH" "$PUBLIC_VERSION" "$expected_version" "$expected_commit"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

audit_effective_nginx() {
    sudo nginx -T 2>&1 | sudo python3 \
        "$RELEASE/scripts/configure_production_nginx.py" \
        audit-effective - "$NGINX_SITE"
}

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

project_for_slot() {
    case "$1" in
        legacy) printf '%s' "$COMPOSE_PROJECT" ;;
        a|b) printf '%s-app-%s' "$COMPOSE_PROJECT" "$1" ;;
        *) return 1 ;;
    esac
}

count_established_connections() {
    local port="$1"
    case "$port" in
        3008|3009) ;;
        *) return 1 ;;
    esac
    ss -Htn state established "sport = :$port" | awk 'END {print NR + 0}'
}

cancel_pending_drain_for_active_release() {
    local state
    local expected_state="$OLD_PROJECT $OLD_PORT $PREVIOUS"

    if [ ! -e "$PENDING_DRAIN_FILE" ] && [ ! -L "$PENDING_DRAIN_FILE" ]; then
        return 0
    fi
    if [ ! -f "$PENDING_DRAIN_FILE" ] || [ -L "$PENDING_DRAIN_FILE" ] || \
        [ ! -s "$PENDING_DRAIN_FILE" ]; then
        echo "invalid pending-drain marker while cancelling rollback drain" >&2
        return 1
    fi
    state="$(cat "$PENDING_DRAIN_FILE")" || return 1
    if [ "$state" != "$expected_state" ]; then
        echo "pending-drain does not match the restored active release" >&2
        return 1
    fi
    remove_durable_file "$PENDING_DRAIN_FILE"
}

complete_pending_drain() {
    local state
    local pending_project
    local pending_port
    local pending_release
    local pending_release_id
    local pending_env_release_id
    local expected_port
    local connections

    if [ ! -e "$PENDING_DRAIN_FILE" ] && [ ! -L "$PENDING_DRAIN_FILE" ]; then
        return 0
    fi
    if [ ! -f "$PENDING_DRAIN_FILE" ] || [ -L "$PENDING_DRAIN_FILE" ] || \
        [ ! -s "$PENDING_DRAIN_FILE" ]; then
        echo "invalid pending-drain marker" >&2
        return 1
    fi
    state="$(cat "$PENDING_DRAIN_FILE")" || return 1
    if [[ "$state" == *$'\n'* ]] || \
        [[ ! "$state" =~ ^([A-Za-z0-9._-]+)[[:space:]](3008|3009)[[:space:]]([^[:space:]]+)$ ]]; then
        echo "invalid pending-drain format" >&2
        return 1
    fi
    pending_project="${BASH_REMATCH[1]}"
    pending_port="${BASH_REMATCH[2]}"
    pending_release="${BASH_REMATCH[3]}"

    case "$pending_project" in
        "$COMPOSE_PROJECT") expected_port=3008 ;;
        "${COMPOSE_PROJECT}-app-a") expected_port=3008 ;;
        "${COMPOSE_PROJECT}-app-b") expected_port=3009 ;;
        *)
            echo "pending-drain references an unmanaged Compose project" >&2
            return 1
            ;;
    esac
    if [ "$pending_port" != "$expected_port" ]; then
        echo "pending-drain project and port do not match" >&2
        return 1
    fi
    if [ ! -d "$pending_release" ]; then
        echo "pending-drain release directory is missing" >&2
        return 1
    fi
    pending_release="$(readlink -f "$pending_release")" || return 1
    case "$pending_release" in
        "$APP_ROOT"/releases/*) ;;
        *)
            echo "pending-drain release is outside the managed release directory" >&2
            return 1
            ;;
    esac
    if [ ! -s "$pending_release/.env" ] || \
        [ ! -s "$pending_release/$COMPOSE_FILE" ]; then
        echo "pending-drain release is incomplete" >&2
        return 1
    fi
    pending_release_id="$(basename "$pending_release")"
    pending_env_release_id="$(
        grep -E '^ASTRA_RELEASE_ID=' "$pending_release/.env" | tail -1 | \
            cut -d= -f2- | sed -E 's/^"//; s/"$//'
    )"
    if [ "$pending_env_release_id" != "$pending_release_id" ]; then
        echo "pending-drain release identity does not match its environment" >&2
        return 1
    fi

    # A crash during rollback can leave the durable drain intent behind after
    # its source release has become authoritative again. In that exact case the
    # drain was cancelled, so heal the marker instead of blocking forever. Any
    # mismatch remains fail-closed.
    if [ "$pending_project" = "$OLD_PROJECT" ]; then
        if [ "$pending_port" != "$OLD_PORT" ] || \
            [ "$pending_release" != "$PREVIOUS" ]; then
            echo "pending-drain conflicts with the active release" >&2
            return 1
        fi
        cancel_pending_drain_for_active_release
        return $?
    fi
    if [ "$pending_port" != "$CANDIDATE_PORT" ] || \
        [ "$pending_release" = "$PREVIOUS" ]; then
        echo "pending-drain does not describe the inactive slot" >&2
        return 1
    fi
    connections="$(count_established_connections "$pending_port")" || return 1
    case "$connections" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ "$connections" != "0" ]; then
        echo "pending-drain still has $connections established connection(s); refusing slot reuse" >&2
        return 1
    fi

    compose_project \
        "$pending_project" "$pending_release/.env" "$pending_release/$COMPOSE_FILE" \
        stop worker frontend backend || return 1
    remove_durable_file "$PENDING_DRAIN_FILE"
}

release_for_slot() {
    local slot="$1"
    local journal="$APP_ROOT/slot-${slot}-release"
    local release

    if [ ! -s "$journal" ]; then
        return 1
    fi
    release="$(tr -d '\r\n' < "$journal")"
    if [ -z "$release" ] || [ ! -d "$release" ]; then
        return 1
    fi
    release="$(readlink -f "$release")"
    case "$release" in
        "$APP_ROOT"/releases/*) ;;
        *) return 1 ;;
    esac
    if [ ! -s "$release/VERSION" ] || [ ! -s "$release/COMMIT" ] || \
        [ ! -s "$release/.env" ] || [ ! -s "$release/$COMPOSE_FILE" ]; then
        return 1
    fi
    printf '%s' "$release"
}

wait_for_worker_release() {
    local project="$1"
    local env_file="$2"
    local compose_file="$3"
    local expected_release_id="$4"
    local attempts="$5"
    local backend_id
    local backend_health
    local backend_image_id
    local worker_id
    local worker_health
    local worker_image_id
    local worker_image_name
    local worker_environment
    local worker_release_id
    local worker_role

    for _ in $(seq 1 "$attempts"); do
        backend_id="$(
            compose_project "$project" "$env_file" "$compose_file" ps -q backend \
                2>/dev/null || true
        )"
        worker_id="$(
            compose_project "$project" "$env_file" "$compose_file" ps -q worker \
                2>/dev/null || true
        )"
        if [ -n "$backend_id" ] && [[ "$backend_id" != *$'\n'* ]] && \
            [ -n "$worker_id" ] && [[ "$worker_id" != *$'\n'* ]]; then
            backend_health="$(
                docker inspect \
                    -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                    "$backend_id" 2>/dev/null || true
            )"
            worker_health="$(
                docker inspect \
                    -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                    "$worker_id" 2>/dev/null || true
            )"
            backend_image_id="$(
                docker inspect -f '{{.Image}}' "$backend_id" 2>/dev/null || true
            )"
            worker_image_id="$(
                docker inspect -f '{{.Image}}' "$worker_id" 2>/dev/null || true
            )"
            worker_image_name="$(
                docker inspect -f '{{.Config.Image}}' "$worker_id" 2>/dev/null || true
            )"
            worker_environment="$(
                docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' \
                    "$worker_id" 2>/dev/null || true
            )"
            worker_release_id="$(
                printf '%s\n' "$worker_environment" | \
                    sed -n 's/^ASTRA_RELEASE_ID=//p' | tail -1
            )"
            worker_role="$(
                printf '%s\n' "$worker_environment" | \
                    sed -n 's/^PROCESS_ROLE=//p' | tail -1
            )"
            case "$worker_image_name" in
                *":$expected_release_id")
                    if [ "$backend_health" = "healthy" ] && \
                        [ "$worker_health" = "healthy" ] && \
                        [ -n "$backend_image_id" ] && \
                        [ "$backend_image_id" = "$worker_image_id" ] && \
                        [ "$worker_release_id" = "$expected_release_id" ]; then
                        case ",$worker_role," in
                            *,worker,*) return 0 ;;
                        esac
                    fi
                    ;;
            esac
        fi
        sleep 2
    done
    return 1
}

managed_worker_ids() {
    local project="$1"
    docker ps -q \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=worker"
}

stop_managed_workers_except() {
    local target_project="$1"
    local project
    local worker_ids

    for project in \
        "$COMPOSE_PROJECT" \
        "${COMPOSE_PROJECT}-app-a" \
        "${COMPOSE_PROJECT}-app-b"; do
        if [ "$project" = "$target_project" ]; then
            continue
        fi
        worker_ids="$(managed_worker_ids "$project")" || return 1
        if [ -n "$worker_ids" ]; then
            # IDs come only from exact Compose project/service labels.
            docker stop --time 120 $worker_ids >/dev/null || return 1
        fi
    done
}

assert_single_active_worker() {
    local target_project="$1"
    local env_file="$2"
    local compose_file="$3"
    local expected_release_id="$4"
    local target_worker_id
    local project
    local worker_ids
    local worker_id
    local only_worker_id=""
    local worker_count=0
    local worker_project

    target_worker_id="$(
        compose_project "$target_project" "$env_file" "$compose_file" ps -q worker \
            2>/dev/null || true
    )"
    if [ -z "$target_worker_id" ] || [[ "$target_worker_id" == *$'\n'* ]]; then
        echo "target worker container is missing or ambiguous" >&2
        return 1
    fi

    for project in \
        "$COMPOSE_PROJECT" \
        "${COMPOSE_PROJECT}-app-a" \
        "${COMPOSE_PROJECT}-app-b"; do
        worker_ids="$(managed_worker_ids "$project")" || return 1
        for worker_id in $worker_ids; do
            worker_count=$((worker_count + 1))
            only_worker_id="$worker_id"
        done
    done
    if [ "$worker_count" != "1" ] || [ "$only_worker_id" != "$target_worker_id" ]; then
        echo "expected exactly one managed worker for $target_project; found $worker_count" >&2
        return 1
    fi
    worker_project="$(
        docker inspect \
            -f '{{index .Config.Labels "com.docker.compose.project"}}' \
            "$target_worker_id" 2>/dev/null || true
    )"
    if [ "$worker_project" != "$target_project" ]; then
        echo "active worker Compose project does not match target" >&2
        return 1
    fi
    wait_for_worker_release \
        "$target_project" "$env_file" "$compose_file" \
        "$expected_release_id" 1
}

activate_worker_release() {
    local project="$1"
    local env_file="$2"
    local compose_file="$3"
    local expected_release_id="$4"
    local attempts="$5"

    stop_managed_workers_except "$project" || return 1
    if ! compose_project \
        "$project" "$env_file" "$compose_file" up -d --no-deps worker; then
        return 1
    fi
    if ! wait_for_worker_release \
        "$project" "$env_file" "$compose_file" "$expected_release_id" "$attempts" || \
        ! assert_single_active_worker \
            "$project" "$env_file" "$compose_file" "$expected_release_id"; then
        compose_project "$project" "$env_file" "$compose_file" stop worker || true
        return 1
    fi
}

remaining_old_nginx_workers() {
    local master_pid="$1"
    local old_workers="$2"
    local parent_pid
    local pid

    for pid in $old_workers; do
        parent_pid="$(
            sudo ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true
        )"
        if [ "$parent_pid" = "$master_pid" ]; then
            printf '%s\n' "$pid"
        fi
    done
}

NGINX_RELOAD_MASTER_PID=""
NGINX_RELOAD_OLD_WORKERS=""

reload_nginx_with_worker_snapshot() {
    command -v pgrep >/dev/null 2>&1 || return 1
    NGINX_RELOAD_MASTER_PID="$(sudo cat /run/nginx.pid | tr -d '[:space:]')" || return 1
    case "$NGINX_RELOAD_MASTER_PID" in
        ''|*[!0-9]*) return 1 ;;
    esac
    # The deploy-wide flock prevents another release from reloading Nginx
    # between this snapshot, public validation, and the convergence check.
    NGINX_RELOAD_OLD_WORKERS="$(
        sudo pgrep -P "$NGINX_RELOAD_MASTER_PID" 2>/dev/null || true
    )"
    sudo systemctl reload nginx
}

retire_pre_reload_nginx_workers() {
    local remaining
    local parent_pid
    local deadline
    local pid
    # Old Nginx workers own pre-reload WebSockets. Give them the same bounded
    # grace window promised to application connections before TERM is allowed.
    local grace_seconds="${NGINX_WORKER_GRACE_SECONDS:-$DRAIN_TIMEOUT_SECONDS}"
    local term_seconds="${NGINX_WORKER_TERM_SECONDS:-15}"

    case "$grace_seconds" in ''|*[!0-9]*) return 1 ;; esac
    case "$term_seconds" in ''|*[!0-9]*) return 1 ;; esac
    if [ "$grace_seconds" -gt 86400 ] || [ "$term_seconds" -gt 300 ]; then
        return 1
    fi
    case "$NGINX_RELOAD_MASTER_PID" in
        ''|*[!0-9]*) return 1 ;;
    esac

    # A graceful reload leaves pre-reload workers serving established
    # WebSockets with their compiled log format. Only PIDs in the pre-reload
    # snapshot are eligible for termination; newly spawned workers are never
    # included.
    deadline=$(( $(date +%s) + grace_seconds ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        remaining="$(
            remaining_old_nginx_workers \
                "$NGINX_RELOAD_MASTER_PID" "$NGINX_RELOAD_OLD_WORKERS"
        )"
        if [ -z "$remaining" ]; then
            sudo systemctl is-active --quiet nginx
            return $?
        fi
        sleep 1
    done

    remaining="$(
        remaining_old_nginx_workers \
            "$NGINX_RELOAD_MASTER_PID" "$NGINX_RELOAD_OLD_WORKERS"
    )"
    for pid in $remaining; do
        parent_pid="$(
            sudo ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true
        )"
        if [ "$parent_pid" = "$NGINX_RELOAD_MASTER_PID" ]; then
            # A worker can exit between the parent check and TERM. The final
            # convergence check, rather than this racy signal, is authoritative.
            sudo kill -TERM "$pid" 2>/dev/null || true
        fi
    done

    deadline=$(( $(date +%s) + term_seconds ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        remaining="$(
            remaining_old_nginx_workers \
                "$NGINX_RELOAD_MASTER_PID" "$NGINX_RELOAD_OLD_WORKERS"
        )"
        if [ -z "$remaining" ]; then
            sudo systemctl is-active --quiet nginx
            return $?
        fi
        sleep 1
    done
    remaining="$(
        remaining_old_nginx_workers \
            "$NGINX_RELOAD_MASTER_PID" "$NGINX_RELOAD_OLD_WORKERS"
    )"
    if [ -z "$remaining" ]; then
        sudo systemctl is-active --quiet nginx
        return $?
    fi
    echo "old Nginx workers did not retire after privacy-safe reload" >&2
    return 1
}

parse_cutover_state() {
    local state

    CUTOVER_PHASE=""
    CUTOVER_SLOT=""
    CUTOVER_RELEASE_ID=""
    CUTOVER_NONTERMINAL=0
    if [ ! -e "$CUTOVER_STATE_FILE" ]; then
        return 0
    fi
    if [ ! -f "$CUTOVER_STATE_FILE" ] || [ ! -s "$CUTOVER_STATE_FILE" ]; then
        echo "invalid empty or non-regular cutover state" >&2
        return 1
    fi
    state="$(cat "$CUTOVER_STATE_FILE")" || return 1
    if [[ "$state" == *$'\n'* ]]; then
        echo "invalid multi-line cutover state" >&2
        return 1
    fi
    if [[ ! "$state" =~ ^([a-z_]+)[[:space:]]slot=(a|b|legacy)[[:space:]]release=([A-Za-z0-9._-]+)$ ]]; then
        echo "invalid cutover state format" >&2
        return 1
    fi
    CUTOVER_PHASE="${BASH_REMATCH[1]}"
    CUTOVER_SLOT="${BASH_REMATCH[2]}"
    CUTOVER_RELEASE_ID="${BASH_REMATCH[3]}"
    case "$CUTOVER_PHASE" in
        complete|rollback_complete|recovery_complete)
            ;;
        candidate_ready|nginx_reloaded|public_verified|traffic_and_worker_committed|\
        rollback_started|rollback_incomplete|rollback_partial|\
        rollback_recovering_candidate|recovery_started|recovery_incomplete)
            CUTOVER_NONTERMINAL=1
            ;;
        *)
            echo "unknown cutover phase: $CUTOVER_PHASE" >&2
            return 1
            ;;
    esac
}

select_recovery_target() {
    RECOVERY_REQUIRED=0
    RECOVERY_TARGET_SLOT=""
    RECOVERY_TARGET_PORT=""
    RECOVERY_TARGET_RELEASE_ID=""

    if [ "$CUTOVER_NONTERMINAL" = "1" ]; then
        RECOVERY_REQUIRED=1
        RECOVERY_TARGET_SLOT="$CUTOVER_SLOT"
        RECOVERY_TARGET_PORT="$(port_for_slot "$CUTOVER_SLOT")" || return 1
        RECOVERY_TARGET_RELEASE_ID="$CUTOVER_RELEASE_ID"
        return 0
    fi
    if [ "$RECORDED_SLOT" != "legacy" ] && [ "$RECORDED_SLOT" != "$DISK_SLOT" ]; then
        RECOVERY_REQUIRED=1
    elif [ "$RECORDED_SLOT" = "legacy" ] && [ "$NGINX_ACTIVE_PORT" != "3008" ]; then
        RECOVERY_REQUIRED=1
    fi
    if [ "$RECOVERY_REQUIRED" = "1" ]; then
        RECOVERY_TARGET_SLOT="$DISK_SLOT"
        RECOVERY_TARGET_PORT="$NGINX_ACTIVE_PORT"
    fi
}

validate_stable_state() {
    local recorded_release
    local recorded_release_id
    local canonical_current
    local expected_port

    if [ "$ACTIVE_STATE_PRESENT" = "0" ]; then
        if [ -n "$CUTOVER_PHASE" ]; then
            echo "cutover state exists without committed active state" >&2
            return 1
        fi
        if [ "$RECORDED_SLOT" != "legacy" ] || [ "$NGINX_ACTIVE_PORT" != "3008" ]; then
            echo "legacy bootstrap state is inconsistent with Nginx" >&2
            return 1
        fi
        return 0
    fi

    if ! recorded_release="$(release_for_slot "$RECORDED_SLOT")"; then
        echo "committed active slot has no valid release journal" >&2
        return 1
    fi
    recorded_release_id="$(basename "$recorded_release")"
    if [ "$recorded_release_id" != "$ACTIVE_RELEASE_ID" ]; then
        echo "active release does not match the active slot journal" >&2
        return 1
    fi
    canonical_current="$(readlink -f "$CURRENT")" || return 1
    if [ ! -L "$CURRENT" ] || [ "$canonical_current" != "$recorded_release" ]; then
        echo "current symlink does not match the committed active release" >&2
        return 1
    fi
    if [ -n "$CUTOVER_PHASE" ]; then
        if [ "$CUTOVER_SLOT" != "$RECORDED_SLOT" ] || \
            [ "$CUTOVER_RELEASE_ID" != "$ACTIVE_RELEASE_ID" ]; then
            echo "terminal cutover state does not match committed active state" >&2
            return 1
        fi
        expected_port="$(port_for_slot "$RECORDED_SLOT")" || return 1
        if [ "$expected_port" != "$NGINX_ACTIVE_PORT" ]; then
            echo "terminal cutover state does not match the live Nginx upstream" >&2
            return 1
        fi
    fi
}

recover_indeterminate_cutover() {
    local recorded_slot="$1"
    local target_slot="$2"
    local target_port="$3"
    local expected_target_release_id="${4:-}"
    local source_port
    local target_release
    local target_release_id
    local target_version
    local target_commit
    local target_project
    local fallback_slot

    if ! target_release="$(release_for_slot "$target_slot")"; then
        echo "cannot recover cutover: target slot $target_slot has no valid release journal" >&2
        return 1
    fi
    target_release_id="$(basename "$target_release")"
    if [ -n "$expected_target_release_id" ] && \
        [ "$target_release_id" != "$expected_target_release_id" ]; then
        echo "cannot recover cutover: target release journal does not match cutover state" >&2
        return 1
    fi
    target_version="$(tr -d '[:space:]' < "$target_release/VERSION")"
    target_commit="$(tr -d '[:space:]' < "$target_release/COMMIT")"
    target_project="$(project_for_slot "$target_slot")" || return 1
    case "$target_port" in
        3008) source_port=3009 ;;
        3009) source_port=3008 ;;
        *) return 1 ;;
    esac

    if [ "$recorded_slot" != "$target_slot" ]; then
        fallback_slot="$recorded_slot"
    else
        case "$target_slot" in
            a) fallback_slot=b ;;
            b) fallback_slot=a ;;
            legacy) fallback_slot=b ;;
            *) return 1 ;;
        esac
    fi
    if [ "$recorded_slot" != "$target_slot" ] && \
        ! release_for_slot "$fallback_slot" >/dev/null; then
        echo "cannot recover cutover: committed fallback slot $fallback_slot has no valid release journal" >&2
        return 1
    fi

    write_cutover_state recovery_started "$target_slot" "$target_release_id" || return 1
    if ! wait_for_local_release \
        "$target_port" "$target_version" "$target_commit" \
        "recovery-local-$target_release_id" 30; then
        echo "cannot recover cutover: target slot identity is not healthy" >&2
        write_cutover_state recovery_incomplete "$target_slot" "$target_release_id" || true
        return 1
    fi

    # A hard interruption may leave the site file updated but the log-format file
    # stale. Re-running install is idempotent and completes that intended pair
    # before the on-disk target is made authoritative.
    if ! sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        install "$NGINX_SITE" "$source_port" "$target_port" \
        "$NGINX_LOG_FORMAT"; then
        write_cutover_state recovery_incomplete "$target_slot" "$target_release_id" || true
        return 1
    fi
    if ! sudo nginx -t >/dev/null || ! audit_effective_nginx >/dev/null; then
        write_cutover_state recovery_incomplete "$target_slot" "$target_release_id" || true
        return 1
    fi
    write_atomic_symlink "$CURRENT" "$target_release" || return 1
    if ! reload_nginx_with_worker_snapshot; then
        write_cutover_state recovery_incomplete "$target_slot" "$target_release_id" || true
        return 1
    fi
    if ! wait_for_public_release \
        "$target_version" "$target_commit" \
        "recovery-public-$target_release_id" 30; then
        echo "cannot recover cutover: public identity did not converge to target slot" >&2
        write_cutover_state recovery_incomplete "$target_slot" "$target_release_id" || true
        return 1
    fi

    if ! activate_worker_release \
        "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        "$target_release_id" 90; then
        write_cutover_state recovery_incomplete "$target_slot" "$target_release_id" || true
        return 1
    fi
    if ! retire_pre_reload_nginx_workers; then
        write_cutover_state recovery_incomplete "$target_slot" "$target_release_id" || true
        return 1
    fi

    commit_active_state "$target_slot" "$target_release_id" || return 1
    write_cutover_state recovery_complete "$target_slot" "$target_release_id" || return 1
    RECORDED_SLOT="$target_slot"
}

if ! load_active_state; then
    echo "refusing deployment with invalid committed active state" >&2
    exit 1
fi
if ! parse_cutover_state; then
    echo "refusing deployment with an invalid cutover journal" >&2
    exit 1
fi

mkdir -p "$RELEASE" "$BACKUP"
tar -xzf "$PACKAGE" -C "$RELEASE"

NGINX_ACTIVE_PORT="$(
    sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        active-port "$NGINX_SITE"
)"
DISK_SLOT="$(slot_for_port "$NGINX_ACTIVE_PORT")" || {
    echo "unsupported production Nginx upstream: $NGINX_ACTIVE_PORT" >&2
    exit 1
}
if [ "$CUTOVER_NONTERMINAL" = "0" ] && ! validate_stable_state; then
    echo "refusing deployment with inconsistent committed release state" >&2
    exit 1
fi
select_recovery_target || {
    echo "cannot select a safe cutover recovery target" >&2
    exit 1
}
if [ "$RECOVERY_REQUIRED" = "1" ]; then
    echo "[remote] indeterminate cutover: recorded=$RECORDED_SLOT disk=$DISK_SLOT target=$RECOVERY_TARGET_SLOT; preserving both slots"
    if ! recover_indeterminate_cutover \
        "$RECORDED_SLOT" "$RECOVERY_TARGET_SLOT" "$RECOVERY_TARGET_PORT" \
        "$RECOVERY_TARGET_RELEASE_ID"; then
        echo "indeterminate cutover recovery failed; both slots were preserved" >&2
        exit 1
    fi
    DISK_SLOT="$RECOVERY_TARGET_SLOT"
    NGINX_ACTIVE_PORT="$RECOVERY_TARGET_PORT"
    CURRENT_TARGET="$(readlink -f "$CURRENT")"
    if ! load_active_state || ! parse_cutover_state || ! validate_stable_state; then
        echo "cutover recovery did not produce a consistent terminal state" >&2
        exit 1
    fi
elif [ "$ACTIVE_STATE_PRESENT" = "1" ]; then
    # Heal compatibility mirrors or migrate the pre-v1.10.12 pair only after
    # the canonical state, current symlink, and live Nginx target agree.
    commit_active_state "$RECORDED_SLOT" "$ACTIVE_RELEASE_ID" || {
        echo "cannot persist canonical active release state" >&2
        exit 1
    }
fi

if [ "$RECORDED_SLOT" = "legacy" ]; then
    ACTIVE_SLOT="legacy"
else
    ACTIVE_SLOT="$DISK_SLOT"
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
if [ "$ACTIVE_SLOT" = "legacy" ]; then
    PREVIOUS="$CURRENT_TARGET"
else
    ACTIVE_SLOT_RELEASE_FILE="$APP_ROOT/slot-${ACTIVE_SLOT}-release"
    if [ -f "$ACTIVE_SLOT_RELEASE_FILE" ]; then
        PREVIOUS="$(tr -d '\r\n' < "$ACTIVE_SLOT_RELEASE_FILE")"
    elif [ "$ACTIVE_SLOT" = "$RECORDED_SLOT" ] && [ -d "$CURRENT_TARGET" ]; then
        PREVIOUS="$CURRENT_TARGET"
        write_atomic_line "$ACTIVE_SLOT_RELEASE_FILE" "$PREVIOUS"
    else
        echo "cannot reconcile live slot $ACTIVE_SLOT to a release directory" >&2
        exit 1
    fi
fi
JWT_ROTATION_MARKER="$APP_ROOT/.jwt-url-leak-rotation-v1"
ROTATE_JWT=0
if [ ! -f "$JWT_ROTATION_MARKER" ]; then
    ROTATE_JWT=1
fi

if [ ! -L "$CURRENT" ] || [ -z "$PREVIOUS" ] || [ ! -d "$PREVIOUS" ]; then
    echo "previous release not found from $CURRENT" >&2
    exit 1
fi
PREVIOUS="$(readlink -f "$PREVIOUS")"
case "$PREVIOUS" in
    "$APP_ROOT"/releases/*) ;;
    *)
        echo "previous release is outside the managed release directory" >&2
        exit 1
        ;;
esac
if [ ! -s "$PREVIOUS/VERSION" ] || [ ! -s "$PREVIOUS/COMMIT" ] || \
    [ ! -s "$PREVIOUS/.env" ] || [ ! -s "$PREVIOUS/$COMPOSE_FILE" ]; then
    echo "previous release is incomplete" >&2
    exit 1
fi
PREVIOUS_RELEASE_ID="$(basename "$PREVIOUS")"
write_atomic_line "$APP_ROOT/slot-${ACTIVE_SLOT}-release" "$PREVIOUS"
if [ "$ACTIVE_STATE_PRESENT" = "0" ]; then
    commit_active_state "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || {
        echo "cannot bootstrap canonical active release state" >&2
        exit 1
    }
    ACTIVE_STATE_PRESENT=1
    ACTIVE_RELEASE_ID="$PREVIOUS_RELEASE_ID"
fi
if ! complete_pending_drain; then
    echo "cannot safely reuse inactive slot while pending drain is unresolved" >&2
    exit 1
fi

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
test -s "$PREVIOUS/VERSION"
test -s "$PREVIOUS/COMMIT"
PREVIOUS_VERSION="$(tr -d '[:space:]' < "$PREVIOUS/VERSION")"
PREVIOUS_COMMIT="$(tr -d '[:space:]' < "$PREVIOUS/COMMIT")"
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

NGINX_BACKUP="$BACKUP/astra-poc.nginx.before.conf"
NGINX_LOG_FORMAT_BACKUP="$BACKUP/00-astra-log-redaction.before.conf"
NGINX_CONFIG_TOUCHED=0
CANDIDATE_READY_FOR_FALLBACK=0

ensure_old_application_ready() {
    if wait_for_local_release \
        "$OLD_PORT" "$PREVIOUS_VERSION" "$PREVIOUS_COMMIT" \
        "rollback-local-$PREVIOUS_RELEASE_ID" 3; then
        return 0
    fi
    compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" up -d --no-deps backend || return 1
    compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" up -d --no-deps frontend || return 1
    wait_for_local_release \
        "$OLD_PORT" "$PREVIOUS_VERSION" "$PREVIOUS_COMMIT" \
        "rollback-local-$PREVIOUS_RELEASE_ID" 60
}

restore_previous_nginx() {
    # Automatic rollback changes only the upstream. Restoring the historical
    # site/log-format backups would re-enable request metadata in access logs.
    sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        install "$NGINX_SITE" "$CANDIDATE_PORT" "$OLD_PORT" \
        "$NGINX_LOG_FORMAT" || return 1
    sudo nginx -t >/dev/null || return 1
    audit_effective_nginx >/dev/null || return 1
    write_atomic_symlink "$CURRENT" "$PREVIOUS" || return 1
    reload_nginx_with_worker_snapshot
}

recover_candidate_traffic() {
    write_cutover_state \
        rollback_recovering_candidate "$CANDIDATE_SLOT" "$RELEASE_ID" || return 1
    if ! wait_for_local_release \
        "$CANDIDATE_PORT" "$VERSION" "$COMMIT" \
        "rollback-recover-local-$RELEASE_ID" 30; then
        write_cutover_state \
            recovery_incomplete "$CANDIDATE_SLOT" "$RELEASE_ID" || true
        return 1
    fi
    sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        install "$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT" \
        "$NGINX_LOG_FORMAT" || return 1
    sudo nginx -t >/dev/null || return 1
    audit_effective_nginx >/dev/null || return 1
    write_atomic_symlink "$CURRENT" "$RELEASE" || return 1
    if ! reload_nginx_with_worker_snapshot; then
        write_cutover_state \
            recovery_incomplete "$CANDIDATE_SLOT" "$RELEASE_ID" || true
        return 1
    fi
    if ! wait_for_public_release \
        "$VERSION" "$COMMIT" "rollback-recover-$RELEASE_ID" 30; then
        write_cutover_state \
            recovery_incomplete "$CANDIDATE_SLOT" "$RELEASE_ID" || true
        return 1
    fi
    if ! activate_worker_release \
        "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
        "$RELEASE_ID" 90; then
        write_cutover_state \
            recovery_incomplete "$CANDIDATE_SLOT" "$RELEASE_ID" || true
        return 1
    fi
    if ! retire_pre_reload_nginx_workers; then
        write_cutover_state \
            recovery_incomplete "$CANDIDATE_SLOT" "$RELEASE_ID" || true
        return 1
    fi
    commit_active_state "$CANDIDATE_SLOT" "$RELEASE_ID" || return 1
    write_cutover_state recovery_complete "$CANDIDATE_SLOT" "$RELEASE_ID"
}

recover_candidate_if_ready() {
    if [ "$CANDIDATE_READY_FOR_FALLBACK" != "1" ]; then
        return 1
    fi
    recover_candidate_traffic
}

rollback() {
    local rollback_status
    echo "[remote] rollback to $PREVIOUS" >&2
    trap - ERR HUP INT TERM
    set +e
    rm -f "$SMOKE_ENV_FILE"
    if ! write_cutover_state \
        rollback_started "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID"; then
        echo "[remote] rollback refused: cannot persist rollback intent" >&2
        return 1
    fi

    if ! ensure_old_application_ready; then
        echo "[remote] rollback incomplete: previous application is not healthy; candidate remains running" >&2
        if ! recover_candidate_if_ready; then
            write_cutover_state \
                rollback_incomplete "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || true
        fi
        return 1
    fi

    if [ "$NGINX_CONFIG_TOUCHED" = "1" ]; then
        if ! restore_previous_nginx; then
            echo "[remote] rollback incomplete: privacy-safe Nginx rollback failed; candidate remains running" >&2
            if ! recover_candidate_if_ready; then
                write_cutover_state \
                    rollback_incomplete "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || true
            fi
            return 1
        fi
    else
        write_atomic_symlink "$CURRENT" "$PREVIOUS" || return 1
    fi

    if ! wait_for_public_release "$PREVIOUS_VERSION" "$PREVIOUS_COMMIT" "rollback-$RELEASE_ID" 30; then
        echo "[remote] rollback incomplete: previous public identity was not restored" >&2
        if [ "$NGINX_CONFIG_TOUCHED" = "1" ]; then
            recover_candidate_traffic || true
        fi
        return 1
    fi

    rollback_status=0
    if ! activate_worker_release \
        "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" \
        "$PREVIOUS_RELEASE_ID" 90; then
        if [ "$NGINX_CONFIG_TOUCHED" = "1" ]; then
            recover_candidate_traffic || true
        else
            write_cutover_state \
                rollback_incomplete "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || true
        fi
        return 1
    fi

    if [ "$NGINX_CONFIG_TOUCHED" = "1" ] && \
        ! retire_pre_reload_nginx_workers; then
        echo "[remote] rollback incomplete: pre-rollback Nginx workers did not drain" >&2
        recover_candidate_traffic || \
            write_cutover_state \
                rollback_incomplete "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || true
        return 1
    fi

    commit_active_state "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || return 1
    # A successful rollback makes the old release active again, cancelling the
    # pre-cutover drain intent. The durable recovery path performs the same
    # exact-match healing if interruption occurs before this removal.
    cancel_pending_drain_for_active_release || return 1
    compose_project \
        "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
        stop frontend backend || rollback_status=1
    if [ "$rollback_status" != "0" ]; then
        write_cutover_state \
            rollback_partial "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || true
        return 1
    fi
    write_cutover_state \
        rollback_complete "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || return 1
    return 0
}

abort_release() {
    local rollback_status
    echo "$1" >&2
    trap - ERR HUP INT TERM
    set +e
    rollback
    rollback_status=$?
    if [ "$rollback_status" != "0" ]; then
        echo "[remote] release failed and rollback requires operator attention" >&2
    fi
    exit 1
}

on_error() {
    local original_status="$1"
    local rollback_status
    trap - ERR HUP INT TERM
    set +e
    rollback
    rollback_status=$?
    if [ "$rollback_status" != "0" ]; then
        echo "[remote] release failed and rollback requires operator attention" >&2
    fi
    exit "$original_status"
}

on_signal() {
    local signal_name="$1"
    local signal_status="$2"
    local rollback_status
    echo "[remote] release interrupted by $signal_name" >&2
    trap - ERR HUP INT TERM
    set +e
    rollback
    rollback_status=$?
    if [ "$rollback_status" != "0" ]; then
        echo "[remote] interrupted release requires operator attention" >&2
    fi
    exit "$signal_status"
}
trap 'on_error $?' ERR
trap 'on_signal HUP 129' HUP
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

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
    false
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
echo "[remote] verifying candidate release identity"
if ! wait_for_local_release \
    "$CANDIDATE_PORT" "$VERSION" "$COMMIT" "$RELEASE_ID" 60; then
    abort_release "candidate slot did not expose expected release $VERSION/$COMMIT"
fi
curl -fsS "http://127.0.0.1:${CANDIDATE_PORT}/api/version" | tee "$BACKUP/version.candidate.json"
CANDIDATE_READY_FOR_FALLBACK=1
write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"

# Persist the source release before any traffic mutation. If the host stops at
# any later instruction, cutover recovery converges first and the next deploy
# either resumes this exact inactive drain or cancels it after an exact rollback.
write_atomic_line "$PENDING_DRAIN_FILE" "$OLD_PROJECT $OLD_PORT $PREVIOUS"
write_cutover_state candidate_ready "$CANDIDATE_SLOT" "$RELEASE_ID"

echo "[remote] installing privacy-safe access logging and switching traffic"
sudo cp "$NGINX_SITE" "$NGINX_BACKUP"
if sudo test -f "$NGINX_LOG_FORMAT"; then
    sudo cp "$NGINX_LOG_FORMAT" "$NGINX_LOG_FORMAT_BACKUP"
fi
NGINX_CONFIG_TOUCHED=1
sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
    install "$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT" \
    "$NGINX_LOG_FORMAT"
sudo nginx -t
audit_effective_nginx
write_atomic_symlink "$CURRENT" "$RELEASE"
reload_nginx_with_worker_snapshot
write_cutover_state nginx_reloaded "$CANDIDATE_SLOT" "$RELEASE_ID"

echo "[remote] verifying public cutover identity"
if ! wait_for_public_release "$VERSION" "$COMMIT" "$RELEASE_ID" 30; then
    abort_release "public cutover did not expose expected release $VERSION/$COMMIT"
fi
printf '%s\n%s\n' "$PUBLIC_HEALTH" "$PUBLIC_VERSION"
printf '%s\n' "$PUBLIC_HEALTH" > "$BACKUP/health.public.json"
printf '%s\n' "$PUBLIC_VERSION" > "$BACKUP/version.public.json"
write_cutover_state public_verified "$CANDIDATE_SLOT" "$RELEASE_ID"

if ! activate_worker_release \
    "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
    "$RELEASE_ID" 90; then
    abort_release "candidate worker did not become healthy on release $RELEASE_ID"
fi
if ! retire_pre_reload_nginx_workers; then
    abort_release "pre-cutover Nginx workers did not drain within the bounded window"
fi
commit_active_state "$CANDIDATE_SLOT" "$RELEASE_ID"
write_cutover_state traffic_and_worker_committed "$CANDIDATE_SLOT" "$RELEASE_ID"

echo "[remote] draining old application connections on port $OLD_PORT"
DRAINED=0
DEADLINE=$(( $(date +%s) + DRAIN_TIMEOUT_SECONDS ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    CONNECTIONS="$(count_established_connections "$OLD_PORT")"
    if [ "$CONNECTIONS" = "0" ]; then
        DRAINED=1
        break
    fi
    echo "[remote] waiting for $CONNECTIONS old connection(s) to drain"
    sleep 10
done

if [ "$DRAINED" = "1" ]; then
    compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" stop frontend backend
    remove_durable_file "$PENDING_DRAIN_FILE"
else
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
        abort_release "remote smoke environment file is missing"
    fi
    set -a
    # This file is generated locally with bash-escaped values and mode 0600.
    source "$SMOKE_ENV_FILE"
    set +a
    rm -f "$SMOKE_ENV_FILE"
    scripts/subscription-production-smoke.sh --api-base "$PUBLIC_URL/api" --frontend-url "$PUBLIC_URL" | tee "$BACKUP/subscription-smoke.json"
fi

if [ "$ROTATE_JWT" = "1" ]; then
    touch "$JWT_ROTATION_MARKER"
fi
write_cutover_state complete "$CANDIDATE_SLOT" "$RELEASE_ID"

trap - ERR HUP INT TERM
sudo find /var/log/nginx -maxdepth 1 -type f -name 'access.log.*' -exec chmod 600 {} + >/dev/null 2>&1 || true
rm -f "$PACKAGE" "$SMOKE_ENV_FILE"
echo "[remote] release $RELEASE_ID deployed on slot $CANDIDATE_SLOT"
REMOTE_SCRIPT

echo "[done] deployed $RELEASE_ID to $PUBLIC_URL"
