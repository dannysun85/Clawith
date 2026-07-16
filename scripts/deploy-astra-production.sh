#!/usr/bin/env bash
{ set +x; } 2>/dev/null
set -euo pipefail

SMOKE_ENV_KEYS=(
    SMOKE_TENANT_EMAIL
    SMOKE_TENANT_PASSWORD
    SMOKE_PLATFORM_ADMIN_EMAIL
    SMOKE_PLATFORM_ADMIN_PASSWORD
)
SMOKE_ENV_VALUES=()

capture_smoke_credentials() {
    SMOKE_ENV_VALUES=(
        "${SMOKE_TENANT_EMAIL-}"
        "${SMOKE_TENANT_PASSWORD-}"
        "${SMOKE_PLATFORM_ADMIN_EMAIL-}"
        "${SMOKE_PLATFORM_ADMIN_PASSWORD-}"
    )
    unset SMOKE_TENANT_EMAIL SMOKE_TENANT_PASSWORD
    unset SMOKE_PLATFORM_ADMIN_EMAIL SMOKE_PLATFORM_ADMIN_PASSWORD
    export -n SMOKE_ENV_VALUES 2>/dev/null || true
}

assert_smoke_credentials_not_exported() {
    python3 - "${SMOKE_ENV_KEYS[@]}" <<'PY_SMOKE_ENV_ISOLATION'
import os
import sys

leaked = [key for key in sys.argv[1:] if key in os.environ]
if leaked:
    raise SystemExit("smoke credentials remained exported to local release gates")
PY_SMOKE_ENV_ISOLATION
}

emit_smoke_credential_payload() {
    python3 - "${SMOKE_ENV_KEYS[@]}" \
        3< <(printf '%s\0' "${SMOKE_ENV_VALUES[@]}") <<'PY_SMOKE_CREDENTIAL_PAYLOAD'
import json
import os
import sys

keys = sys.argv[1:]
raw = os.fdopen(3, "rb").read(65_537)
if len(raw) > 65_536 or not raw.endswith(b"\0"):
    raise SystemExit("invalid smoke credential input")
encoded_values = raw[:-1].split(b"\0")
if len(encoded_values) != len(keys):
    raise SystemExit("invalid smoke credential count")
values = [value.decode("utf-8") for value in encoded_values]
payload = dict(zip(keys, values))
data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
if len(data) > 16_384:
    raise SystemExit("remote smoke credential payload is too large")
sys.stdout.buffer.write(data)
PY_SMOKE_CREDENTIAL_PAYLOAD
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing required command: $1" >&2
        exit 1
    }
}

capture_smoke_credentials

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
RUN_REMOTE_SMOKE="${RUN_REMOTE_SMOKE:-1}"
REMOTE_SMOKE_BREAK_GLASS_ARTIFACT="${REMOTE_SMOKE_BREAK_GLASS_ARTIFACT:-}"
DRAIN_TIMEOUT_SECONDS="${DRAIN_TIMEOUT_SECONDS:-900}"

SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
SSH_OPTS=(
    -i "$SSH_KEY"
    -o BatchMode=yes
    -o ConnectTimeout=15
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=6
)

require_cmd git
require_cmd tar
require_cmd gzip
require_cmd ssh
require_cmd scp
require_cmd python3
require_cmd uv
require_cmd npm
require_cmd docker
require_cmd createdb
require_cmd dropdb
require_cmd psql
assert_smoke_credentials_not_exported

if [ "$RUN_LOCAL_CHECKS" != "1" ]; then
    echo "production releases cannot disable local release gates" >&2
    exit 1
fi
case "$RUN_REMOTE_SMOKE" in
    0|1) ;;
    *)
        echo "RUN_REMOTE_SMOKE must be 0 or 1" >&2
        exit 1
        ;;
esac

if [ -n "$(git status --short)" ]; then
    echo "working tree is dirty; production releases require a reviewed commit" >&2
    exit 1
fi

VERSION="$(tr -d '[:space:]' < backend/VERSION)"
FRONTEND_VERSION="$(tr -d '[:space:]' < frontend/VERSION)"
if [ -z "$VERSION" ] || [ "$VERSION" != "$FRONTEND_VERSION" ]; then
    echo "backend/VERSION and frontend/VERSION must be identical and non-empty" >&2
    exit 1
fi
if ! grep -Fq "$VERSION" RELEASE_NOTES.md; then
    echo "RELEASE_NOTES.md does not mention version $VERSION" >&2
    exit 1
fi
COMMIT="$(git rev-parse HEAD)"
case "$COMMIT" in
    ''|*[!0-9a-f]*)
        echo "release commit must be a full lowercase Git object ID" >&2
        exit 1
        ;;
    *) ;;
esac
[ "${#COMMIT}" = "40" ] || {
    echo "release commit must contain exactly 40 hexadecimal characters" >&2
    exit 1
}
RELEASE_BASE_TAG="$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null)" || {
    echo "release requires an ancestor tag to define the complete Ruff baseline" >&2
    exit 1
}
RELEASE_BASE_COMMIT="$(git rev-parse "${RELEASE_BASE_TAG}^{commit}")"
git merge-base --is-ancestor "$RELEASE_BASE_COMMIT" HEAD || {
    echo "release Ruff baseline is not an ancestor of the candidate" >&2
    exit 1
}

REMOTE_SMOKE_BREAK_GLASS_DIGEST="none"
REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH="none"
if [ "$RUN_REMOTE_SMOKE" = "0" ]; then
    if [ -z "$REMOTE_SMOKE_BREAK_GLASS_ARTIFACT" ] || \
        [ ! -f "$REMOTE_SMOKE_BREAK_GLASS_ARTIFACT" ] || \
        [ -L "$REMOTE_SMOKE_BREAK_GLASS_ARTIFACT" ]; then
        echo "RUN_REMOTE_SMOKE=0 requires a regular break-glass approval artifact" >&2
        exit 1
    fi
    BREAK_GLASS_VALIDATION="$(python3 - \
        "$REMOTE_SMOKE_BREAK_GLASS_ARTIFACT" "$VERSION" "$COMMIT" <<'PY_BREAK_GLASS'
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
version = sys.argv[2]
commit = sys.argv[3]
if path.stat().st_size > 16_384:
    raise SystemExit("break-glass artifact is too large")
fields = {}
for line in path.read_text(encoding="utf-8").splitlines():
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if key in fields:
        raise SystemExit(f"break-glass artifact contains duplicate field: {key}")
    fields[key] = value.strip()
required = {
    "approval_id",
    "approval_nonce",
    "approved_by",
    "bypassed_gates",
    "reason",
    "issued_at_utc",
    "expires_at_utc",
    "release_version",
    "release_commit",
}
if required - fields.keys():
    raise SystemExit("break-glass artifact is missing required fields")
if any(not fields[key] for key in required):
    raise SystemExit("break-glass artifact contains an empty required field")
if fields["release_version"] != version:
    raise SystemExit("break-glass artifact targets a different release version")
if fields["release_commit"] != commit:
    raise SystemExit("break-glass artifact targets a different release commit")
if {item.strip() for item in fields["bypassed_gates"].split(",") if item.strip()} != {
    "subscription_api",
    "subscription_browser",
}:
    raise SystemExit("break-glass artifact must explicitly bypass subscription_api and subscription_browser")
if not re.fullmatch(r"[A-Za-z0-9._-]{16,128}", fields["approval_nonce"]):
    raise SystemExit("break-glass approval_nonce has an invalid format")
issued = datetime.fromisoformat(fields["issued_at_utc"].replace("Z", "+00:00"))
expires = datetime.fromisoformat(fields["expires_at_utc"].replace("Z", "+00:00"))
if issued.tzinfo is None or expires.tzinfo is None:
    raise SystemExit("break-glass artifact timestamps must include a timezone")
now = datetime.now(timezone.utc)
if issued > now + timedelta(minutes=5):
    raise SystemExit("break-glass artifact was issued in the future")
if expires <= now:
    raise SystemExit("break-glass artifact is expired")
if expires <= issued or expires - issued > timedelta(hours=4):
    raise SystemExit("break-glass approval window must be positive and at most four hours")
print(
    hashlib.sha256(path.read_bytes()).hexdigest(),
    hashlib.sha256(fields["approval_nonce"].encode("utf-8")).hexdigest(),
)
PY_BREAK_GLASS
    )"
    read -r REMOTE_SMOKE_BREAK_GLASS_DIGEST \
        REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH <<< "$BREAK_GLASS_VALIDATION"
fi

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
    for index in "${!SMOKE_ENV_KEYS[@]}"; do
        if [ -z "${SMOKE_ENV_VALUES[$index]}" ]; then
            echo "RUN_REMOTE_SMOKE=1 requires ${SMOKE_ENV_KEYS[$index]}" >&2
            exit 1
        fi
    done
else
    unset SMOKE_ENV_VALUES
fi

if [ ! -f "$SSH_KEY" ]; then
    echo "SSH key not found: $SSH_KEY" >&2
    exit 1
fi

echo "[local] checking Alembic heads"
ALEMBIC_HEADS="$(cd backend && uv run alembic heads)"
ALEMBIC_HEAD_COUNT="$(printf '%s\n' "$ALEMBIC_HEADS" | grep -c '(head)')"
if [ "$ALEMBIC_HEAD_COUNT" != "1" ]; then
    printf '%s\n' "$ALEMBIC_HEADS" >&2
    echo "expected exactly one Alembic head" >&2
    exit 1
fi

echo "[local] checking that the release adds no Ruff violations"
uv run --project backend python scripts/ruff_diff_gate.py \
    --base "$RELEASE_BASE_COMMIT" --target HEAD
git diff --check

echo "[local] running full backend suite"
(cd backend && uv run pytest -q)

echo "[local] running full frontend suite and production build"
(cd frontend && npm test)
(cd frontend && npm run build)

echo "[local] testing bounded browser assertions"
(cd deploy/browser-smoke && npm test)

echo "[local] running PostgreSQL upgrade/downgrade/re-upgrade smoke"
bash scripts/postgres-migration-smoke.sh

echo "[local] validating effective production compose"
POSTGRES_PASSWORD=release-gate \
SECRET_KEY=release-gate-secret \
JWT_SECRET_KEY=release-gate-jwt \
CORS_ORIGINS=https://release-gate.invalid \
PUBLIC_BASE_URL=https://release-gate.invalid \
ASTRA_ALERT_WORKER_ACTOR_ID=00000000-0000-4000-8000-000000000001 \
docker compose -f deploy/astra-poc/docker-compose.prod.yml config --quiet

STAMP="$(date -u +%Y%m%d-%H%M%S)"
NONCE="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
RELEASE_ID="${STAMP}-${COMMIT:0:12}-${NONCE}-clawith-saas"
PACKAGE_DIR="$ROOT_DIR/.tmp/releases"
PACKAGE_TAR="$PACKAGE_DIR/${RELEASE_ID}.tar"
PACKAGE="$PACKAGE_DIR/${RELEASE_ID}.tar.gz"
REMOTE_SMOKE_RUNTIME_ROOT="/dev/shm/astra-deploy-smoke"
SMOKE_ENV_REMOTE="$REMOTE_SMOKE_RUNTIME_ROOT/${RELEASE_ID}.smoke-credentials.json"
REMOTE_SMOKE_CREDENTIAL_DIGEST="none"
BREAK_GLASS_FILE="$PACKAGE_DIR/${RELEASE_ID}.break-glass.approval"
BREAK_GLASS_FILE_REMOTE="/tmp/${RELEASE_ID}.break-glass.approval"
SMOKE_ENV_UPLOADED=0
BREAK_GLASS_UPLOADED=0

cleanup_local() {
    rm -f "$PACKAGE_TAR" "$PACKAGE" "$BREAK_GLASS_FILE"
    if [ "$SMOKE_ENV_UPLOADED" = "1" ]; then
        ssh "${SSH_OPTS[@]}" "$SSH_TARGET" rm -f "$SMOKE_ENV_REMOTE" >/dev/null 2>&1 || true
    fi
    if [ "$BREAK_GLASS_UPLOADED" = "1" ]; then
        ssh "${SSH_OPTS[@]}" "$SSH_TARGET" rm -f "$BREAK_GLASS_FILE_REMOTE" >/dev/null 2>&1 || true
    fi
}
trap cleanup_local EXIT

mkdir -p "$PACKAGE_DIR"
if [ "$RUN_REMOTE_SMOKE" = "0" ]; then
    cp "$REMOTE_SMOKE_BREAK_GLASS_ARTIFACT" "$BREAK_GLASS_FILE"
    chmod 0600 "$BREAK_GLASS_FILE"
fi
echo "[local] packaging $RELEASE_ID"
git archive --format=tar --output="$PACKAGE_TAR" "$COMMIT"
ARCHIVE_COMMIT="$(git get-tar-commit-id < "$PACKAGE_TAR")"
[ "$ARCHIVE_COMMIT" = "$COMMIT" ] || {
    echo "release archive is not bound to the reviewed commit" >&2
    exit 1
}
gzip -n -c "$PACKAGE_TAR" > "$PACKAGE"
PACKAGE_SHA256="$(python3 - "$PACKAGE" <<'PY_PACKAGE_SHA256'
import hashlib
from pathlib import Path
import sys

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY_PACKAGE_SHA256
)"

if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    REMOTE_SMOKE_CREDENTIAL_DIGEST="$(
        emit_smoke_credential_payload | python3 -c '
import hashlib
import sys

data = sys.stdin.buffer.read(16_385)
if len(data) > 16_384:
    raise SystemExit("remote smoke credential payload is too large")
print(hashlib.sha256(data).hexdigest())
'
    )"
fi

echo "[remote] uploading package"
scp "${SSH_OPTS[@]}" "$PACKAGE" "${SSH_TARGET}:/tmp/${RELEASE_ID}.tar.gz"
if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
        env -u BASH_ENV -u BASHOPTS -u SHELLOPTS bash -s -- \
        "$REMOTE_SMOKE_RUNTIME_ROOT" <<'REMOTE_SMOKE_RUNTIME_ROOT'
{ set +x; } 2>/dev/null
set -euo pipefail
runtime_root="$1"
runtime_uid="$(id -u)"
if [ -e "$runtime_root" ] || [ -L "$runtime_root" ]; then
    [ -d "$runtime_root" ] && [ ! -L "$runtime_root" ] || exit 1
    [ "$(stat -c '%u' "$runtime_root")" = "$runtime_uid" ] || exit 1
    [ "$(stat -c '%a' "$runtime_root")" = "700" ] || exit 1
else
    install -d -m 0700 "$runtime_root"
fi
REMOTE_SMOKE_RUNTIME_ROOT
    REMOTE_CREDENTIAL_WRITER_B64="$(python3 - <<'PY_REMOTE_CREDENTIAL_WRITER'
import base64

source = r'''
import hashlib
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
expected_digest = sys.argv[2]
root = path.parent
root_stat = root.lstat()
if (
    not stat.S_ISDIR(root_stat.st_mode)
    or root.is_symlink()
    or root_stat.st_uid != os.getuid()
    or stat.S_IMODE(root_stat.st_mode) != 0o700
    or not path.name.endswith(".smoke-credentials.json")
):
    raise SystemExit(1)
data = sys.stdin.buffer.read(16_385)
if len(data) > 16_384 or hashlib.sha256(data).hexdigest() != expected_digest:
    raise SystemExit(1)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags, 0o600)
try:
    os.fchmod(fd, 0o600)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.fsync(fd)
finally:
    os.close(fd)
'''
print(base64.b64encode(source.encode("utf-8")).decode("ascii"))
PY_REMOTE_CREDENTIAL_WRITER
    )"
    emit_smoke_credential_payload |
        ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
            "python3 -c \"import base64;exec(base64.b64decode('$REMOTE_CREDENTIAL_WRITER_B64'))\" '$SMOKE_ENV_REMOTE' '$REMOTE_SMOKE_CREDENTIAL_DIGEST'"
    SMOKE_ENV_UPLOADED=1
    unset SMOKE_ENV_VALUES
else
    scp "${SSH_OPTS[@]}" "$BREAK_GLASS_FILE" "${SSH_TARGET}:${BREAK_GLASS_FILE_REMOTE}"
    BREAK_GLASS_UPLOADED=1
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" chmod 600 "$BREAK_GLASS_FILE_REMOTE"
fi

echo "[remote] deploying $RELEASE_ID"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    env -u BASH_ENV -u BASHOPTS -u SHELLOPTS bash -s -- \
    "$APP_ROOT" "$RELEASE_ID" "$COMPOSE_PROJECT" "$COMPOSE_PROFILES_ARG" \
    "$VERSION" "$COMMIT" "$PUBLIC_URL" "$RUN_REMOTE_SMOKE" \
    "$SMOKE_ENV_REMOTE" "$DRAIN_TIMEOUT_SECONDS" \
    "$REMOTE_SMOKE_BREAK_GLASS_DIGEST" "$BREAK_GLASS_FILE_REMOTE" \
    "$RELEASE_BASE_COMMIT" "$REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH" \
    "$PACKAGE_SHA256" "$REMOTE_SMOKE_CREDENTIAL_DIGEST" <<'REMOTE_SCRIPT'
{ set +x; } 2>/dev/null
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
REMOTE_SMOKE_BREAK_GLASS_DIGEST="${11}"
REMOTE_SMOKE_BREAK_GLASS_FILE="${12}"
RELEASE_BASE_COMMIT="${13}"
REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH="${14}"
PACKAGE_SHA256="${15}"
REMOTE_SMOKE_CREDENTIAL_DIGEST="${16}"

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
MCP_QUARANTINE_SNAPSHOT_ID=""
ROLLBACK_REQUIRES_MCP_QUARANTINE=0
SCHEMA_FORWARD_ONLY=0
MAINTENANCE_ENABLED=0
BROWSER_SMOKE_IMAGE="astra-browser-smoke:${RELEASE_ID}"
BROWSER_SMOKE_READY=0
BROWSER_SMOKE_CONTAINER=""
BROWSER_SMOKE_NETWORK=""
BROWSER_SMOKE_FRONTEND_ID=""
BROWSER_SMOKE_TEMP_DIR=""
BROWSER_SMOKE_RUNTIME_ROOT="/dev/shm/astra-deploy-smoke"

mkdir -p "$APP_ROOT"
if ! command -v flock >/dev/null 2>&1; then
    echo "flock is required for production deployment serialization" >&2
    exit 1
fi
if ! command -v timeout >/dev/null 2>&1; then
    echo "timeout is required for bounded production deployment commands" >&2
    exit 1
fi
exec 9>"$APP_ROOT/deploy.lock"
if ! flock -n 9; then
    echo "another production deployment is already running" >&2
    rm -f "$PACKAGE" "$SMOKE_ENV_FILE"
    exit 1
fi
CURRENT_TARGET="$(python3 -c \
    'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' \
    "$CURRENT" 2>/dev/null)"

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

install_durable_file() {
    local source="$1"
    local destination="$2"
    python3 - "$source" "$destination" <<'PY_INSTALL_DURABLE_FILE'
import os
from pathlib import Path
import stat
import sys
import tempfile

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
source_fd = os.open(source, flags)
try:
    metadata = os.fstat(source_fd)
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 1_048_576:
        raise SystemExit(1)
    payload = bytearray()
    while True:
        chunk = os.read(source_fd, 65_536)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > 1_048_576:
            raise SystemExit(1)
finally:
    os.close(source_fd)

destination.parent.mkdir(parents=True, exist_ok=True)
if destination.parent.is_symlink():
    raise SystemExit(1)
temporary_fd, temporary_name = tempfile.mkstemp(
    prefix=f".{destination.name}.", dir=destination.parent
)
temporary = Path(temporary_name)
try:
    os.fchmod(temporary_fd, 0o600)
    with os.fdopen(temporary_fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
source.unlink(missing_ok=True)
PY_INSTALL_DURABLE_FILE
}

browser_smoke_bundle_digest() {
    local release="$1"
    python3 - "$release/deploy/browser-smoke" <<'PY_BROWSER_BUNDLE_DIGEST'
import hashlib
from pathlib import Path
import sys

root = Path(sys.argv[1])
names = (
    "Dockerfile",
    "EVIDENCE_SCHEMA",
    "browser_launch_selftest.mjs",
    "browser_assertions.mjs",
    "package-lock.json",
    "package.json",
    "seccomp_profile.json",
    "subscription_browser_smoke.mjs",
)
digest = hashlib.sha256()
for name in names:
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise SystemExit(1)
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(f"sha256:{digest.hexdigest()}")
PY_BROWSER_BUNDLE_DIGEST
}

browser_smoke_requires_v2() {
    local release="$1"
    [ -f "$release/deploy/browser-smoke/EVIDENCE_SCHEMA" ] && \
        [ ! -L "$release/deploy/browser-smoke/EVIDENCE_SCHEMA" ] && \
        [ "$(tr -d '[:space:]' < "$release/deploy/browser-smoke/EVIDENCE_SCHEMA")" = "2" ]
}

prepare_browser_smoke_runtime_root() {
    python3 - "$BROWSER_SMOKE_RUNTIME_ROOT" "$SMOKE_ENV_FILE" \
        "$RUN_REMOTE_SMOKE" <<'PY_BROWSER_RUNTIME_CLEANUP'
import os
from pathlib import Path
import shutil
import stat
import sys

root = Path(sys.argv[1])
credential_path = Path(sys.argv[2]) if sys.argv[3] == "1" else None
try:
    root_stat = root.lstat()
except FileNotFoundError:
    root.mkdir(mode=0o700)
    root_stat = root.lstat()
if (
    not stat.S_ISDIR(root_stat.st_mode)
    or root.is_symlink()
    or root_stat.st_uid != os.getuid()
    or stat.S_IMODE(root_stat.st_mode) != 0o700
):
    raise SystemExit(1)
if credential_path is not None and (
    credential_path.parent != root
    or not credential_path.name.endswith(".smoke-credentials.json")
):
    raise SystemExit(1)
current_credential_seen = False
for child in root.iterdir():
    if child.name.startswith(".candidate-smoke."):
        child_stat = child.lstat()
        if (
            not stat.S_ISDIR(child_stat.st_mode)
            or child.is_symlink()
            or child_stat.st_uid != os.getuid()
            or stat.S_IMODE(child_stat.st_mode) != 0o700
        ):
            raise SystemExit(1)
        shutil.rmtree(child)
        continue
    if not child.name.endswith(".smoke-credentials.json"):
        continue
    child_stat = child.lstat()
    if (
        not stat.S_ISREG(child_stat.st_mode)
        or child.is_symlink()
        or child_stat.st_uid != os.getuid()
        or stat.S_IMODE(child_stat.st_mode) != 0o600
    ):
        raise SystemExit(1)
    if credential_path is not None and child == credential_path:
        current_credential_seen = True
        continue
    child.unlink()
if credential_path is not None and not current_credential_seen:
    raise SystemExit(1)
PY_BROWSER_RUNTIME_CLEANUP
}

cleanup_browser_smoke_runtime() {
    local remove_temporary="${1:-1}"
    if [ -n "$BROWSER_SMOKE_CONTAINER" ]; then
        docker rm -f "$BROWSER_SMOKE_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [ -n "$BROWSER_SMOKE_NETWORK" ] && [ -n "$BROWSER_SMOKE_FRONTEND_ID" ]; then
        docker network disconnect -f \
            "$BROWSER_SMOKE_NETWORK" "$BROWSER_SMOKE_FRONTEND_ID" \
            >/dev/null 2>&1 || true
    fi
    if [ -n "$BROWSER_SMOKE_NETWORK" ]; then
        docker network rm "$BROWSER_SMOKE_NETWORK" >/dev/null 2>&1 || true
    fi
    BROWSER_SMOKE_CONTAINER=""
    BROWSER_SMOKE_NETWORK=""
    BROWSER_SMOKE_FRONTEND_ID=""
    if [ "$remove_temporary" = "1" ] && [ -n "${BROWSER_SMOKE_TEMP_DIR:-}" ]; then
        case "$BROWSER_SMOKE_TEMP_DIR" in
            "$BROWSER_SMOKE_RUNTIME_ROOT"/.candidate-smoke.*)
                rm -rf "$BROWSER_SMOKE_TEMP_DIR"
                ;;
        esac
        BROWSER_SMOKE_TEMP_DIR=""
    fi
}

browser_smoke_preflight_signal() {
    local signal_name="$1"
    local signal_status="$2"
    trap - HUP INT TERM
    cleanup_browser_smoke_runtime 0
    echo "browser smoke preflight interrupted by $signal_name" >&2
    exit "$signal_status"
}

ensure_browser_smoke_image() {
    local target_release="$1"
    local target_release_id="$2"
    local image="astra-browser-smoke:${target_release_id}"
    local safe_id
    local host_uid
    local host_gid
    local label

    browser_smoke_requires_v2 "$target_release" || return 1
    browser_smoke_bundle_digest "$target_release" >/dev/null || return 1
    command -v timeout >/dev/null 2>&1 || return 1
    host_uid="$(id -u)" || return 1
    host_gid="$(id -g)" || return 1
    [ "$host_uid" != "0" ] || {
        echo "browser smoke must run under a non-root deployment account" >&2
        return 1
    }
    docker build --pull --tag "$image" "$target_release/deploy/browser-smoke" || return 1
    label="$(
        docker image inspect \
            --format '{{ index .Config.Labels "ai.reeftotem.astra.browser-smoke-schema" }}' \
            "$image" 2>/dev/null
    )" || return 1
    [ "$label" = "2" ] || return 1

    safe_id="$(printf '%s' "$target_release_id" | sha256sum | cut -c1-16)" || return 1
    BROWSER_SMOKE_NETWORK="astra-browser-preflight-${safe_id}"
    BROWSER_SMOKE_CONTAINER="astra-browser-preflight-${safe_id}"
    BROWSER_SMOKE_FRONTEND_ID=""
    cleanup_browser_smoke_runtime 0
    BROWSER_SMOKE_NETWORK="astra-browser-preflight-${safe_id}"
    BROWSER_SMOKE_CONTAINER="astra-browser-preflight-${safe_id}"
    trap 'browser_smoke_preflight_signal HUP 129' HUP
    trap 'browser_smoke_preflight_signal INT 130' INT
    trap 'browser_smoke_preflight_signal TERM 143' TERM
    docker network create \
        --internal \
        --label "ai.reeftotem.astra.browser-smoke-release=$target_release_id" \
        "$BROWSER_SMOKE_NETWORK" >/dev/null || {
        cleanup_browser_smoke_runtime 0
        trap - HUP INT TERM
        return 1
    }
    if ! timeout --signal=TERM --kill-after=5s 45s \
        docker run --rm \
        --name "$BROWSER_SMOKE_CONTAINER" \
        --label "ai.reeftotem.astra.browser-smoke-release=$target_release_id" \
        --init \
        --user "${host_uid}:${host_gid}" \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --security-opt "seccomp=$target_release/deploy/browser-smoke/seccomp_profile.json" \
        --pids-limit 256 \
        --ulimit nofile=1024:1024 \
        --memory 1g \
        --cpus 1.0 \
        --shm-size 256m \
        --tmpfs /tmp:rw,nosuid,nodev,size=256m \
        --tmpfs /home/pwuser:rw,nosuid,nodev,size=64m \
        --env HOME=/home/pwuser \
        --network "$BROWSER_SMOKE_NETWORK" \
        --entrypoint node \
        "$image" \
        /opt/astra-browser-smoke/browser_launch_selftest.mjs; then
        cleanup_browser_smoke_runtime 0
        trap - HUP INT TERM
        return 1
    fi
    cleanup_browser_smoke_runtime 0
    trap - HUP INT TERM
    if [ "$target_release_id" = "$RELEASE_ID" ]; then
        BROWSER_SMOKE_IMAGE="$image"
        BROWSER_SMOKE_READY=1
    fi
}

alert_canary_requires_v1() {
    local release="$1"
    [ -f "$release/backend/app/scripts/verify_production_issue_alerts.py" ] && \
        [ ! -L "$release/backend/app/scripts/verify_production_issue_alerts.py" ]
}

alert_canary_runner_digest() {
    local release="$1"
    local runner="$release/backend/app/scripts/verify_production_issue_alerts.py"
    [ -f "$runner" ] && [ ! -L "$runner" ] || return 1
    printf 'sha256:%s' "$(sha256sum "$runner" | awk '{print $1}')"
}

run_alert_canary_preflight() {
    local target_release="$1"
    local target_project="$2"
    local target_release_id="$3"
    local output="$4"
    local target_version
    local target_commit

    alert_canary_requires_v1 "$target_release" || return 1
    target_version="$(tr -d '[:space:]' < "$target_release/VERSION")" || return 1
    target_commit="$(tr -d '[:space:]' < "$target_release/COMMIT")" || return 1
    if ! compose_project_timed 45 5 \
        "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        run --rm --no-deps -T --entrypoint python backend \
        -m app.scripts.verify_production_issue_alerts \
        --release-id "$target_release_id" --preflight-only > "$output"; then
        rm -f "$output"
        return 1
    fi
    python3 - \
        "$output" "$target_release_id" "$target_version" "$target_commit" \
        <<'PY_ALERT_PREFLIGHT_VALIDATE'
import json
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= 1_048_576:
    raise SystemExit(1)
payload = json.loads(path.read_text(encoding="utf-8"))
sinks = payload.get("configured_sinks")
if (
    payload.get("ok") is not True
    or payload.get("schema_version") != 1
    or payload.get("mode") != "preflight"
    or payload.get("release_id") != sys.argv[2]
    or payload.get("release_version") != sys.argv[3]
    or payload.get("release_commit") != sys.argv[4]
    or not isinstance(sinks, list)
    or not sinks
    or set(sinks) - {"notification", "webhook"}
    or len(sinks) != len(set(sinks))
    or re.fullmatch(
        r"hmac-sha256:[0-9a-f]{64}",
        str(payload.get("sink_config_fingerprint", "")),
    )
    is None
):
    raise SystemExit(1)
PY_ALERT_PREFLIGHT_VALIDATE
}

inspect_worker_runtime_identity() {
    local worker_id="$1"

    # Docker stores the complete process environment in Config.Env. Filter in
    # the Go template before any data reaches the shell, then validate exact
    # cardinality and a non-secret character contract in a separate process.
    docker inspect -f \
        '{{range .Config.Env}}{{if eq (index (split . "=") 0) "ASTRA_RELEASE_ID"}}{{println .}}{{end}}{{if eq (index (split . "=") 0) "ASTRA_RELEASE_COMMIT"}}{{println .}}{{end}}{{if eq (index (split . "=") 0) "ASTRA_ALERT_WORKER_ACTOR_ID"}}{{println .}}{{end}}{{if eq (index (split . "=") 0) "PROCESS_ROLE"}}{{println .}}{{end}}{{end}}' \
        "$worker_id" | python3 -c '
import re
import sys
import uuid

raw = sys.stdin.read(8_193)
if len(raw) > 8_192:
    raise SystemExit("worker runtime identity output is too large")
values = {
    "ASTRA_RELEASE_ID": [],
    "ASTRA_RELEASE_COMMIT": [],
    "ASTRA_ALERT_WORKER_ACTOR_ID": [],
    "PROCESS_ROLE": [],
}
for line in raw.splitlines():
    key, separator, value = line.partition("=")
    if separator and key in values:
        values[key].append(value)
if any(len(items) != 1 for items in values.values()):
    raise SystemExit("worker runtime identity is missing or duplicated")
release_id = values["ASTRA_RELEASE_ID"][0]
release_commit = values["ASTRA_RELEASE_COMMIT"][0]
actor_id = values["ASTRA_ALERT_WORKER_ACTOR_ID"][0]
process_role = values["PROCESS_ROLE"][0]
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", release_id) is None:
    raise SystemExit("worker release identity has an invalid format")
if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:,[A-Za-z][A-Za-z0-9_-]*)*", process_role) is None:
    raise SystemExit("worker process role has an invalid format")
if re.fullmatch(r"[0-9a-f]{40}", release_commit) is None:
    raise SystemExit("worker release commit has an invalid format")
try:
    parsed_actor_id = uuid.UUID(actor_id)
except ValueError:
    raise SystemExit("worker actor identity has an invalid format") from None
if str(parsed_actor_id) != actor_id.lower():
    raise SystemExit("worker actor identity is not canonical")
if len(process_role) > 128:
    raise SystemExit("worker process role is too long")
sys.stdout.write(f"{release_id}\n{release_commit}\n{actor_id.lower()}\n{process_role}\n")
'
}

run_candidate_alert_canary() {
    local target_release="$1"
    local target_project="$2"
    local target_release_id="$3"
    local output="$4"
    local target_backup="$APP_ROOT/backups/$target_release_id"
    local target_commit
    local raw_output="${output}.raw"
    local worker_id_before
    local worker_id_after
    local worker_image_id
    local worker_image_name
    local worker_project
    local worker_identity
    local -a worker_identity_fields=()
    local worker_identity_field
    local worker_release_id
    local worker_release_commit
    local worker_actor_id
    local worker_role
    local runner_digest

    alert_canary_requires_v1 "$target_release" || return 1
    target_commit="$(tr -d '[:space:]' < "$target_release/COMMIT")" || return 1
    assert_single_active_worker \
        "$target_project" "$target_release/.env" \
        "$target_release/$COMPOSE_FILE" "$target_release_id" || return 1
    worker_id_before="$(
        compose_project \
            "$target_project" "$target_release/.env" \
            "$target_release/$COMPOSE_FILE" ps -q worker
    )" || return 1
    [ -n "$worker_id_before" ] && [[ "$worker_id_before" != *$'\n'* ]] || return 1
    worker_image_id="$(docker inspect -f '{{.Image}}' "$worker_id_before")" || return 1
    worker_image_name="$(docker inspect -f '{{.Config.Image}}' "$worker_id_before")" || return 1
    worker_project="$(
        docker inspect \
            -f '{{index .Config.Labels "com.docker.compose.project"}}' \
            "$worker_id_before"
    )" || return 1
    worker_identity="$(inspect_worker_runtime_identity "$worker_id_before")" || return 1
    while IFS= read -r worker_identity_field; do
        worker_identity_fields+=("$worker_identity_field")
    done <<< "$worker_identity"
    [ "${#worker_identity_fields[@]}" = "4" ] || return 1
    worker_release_id="${worker_identity_fields[0]}"
    worker_release_commit="${worker_identity_fields[1]}"
    worker_actor_id="${worker_identity_fields[2]}"
    worker_role="${worker_identity_fields[3]}"
    [ "$worker_project" = "$target_project" ] || return 1
    [ "$worker_release_id" = "$target_release_id" ] || return 1
    [ "$worker_release_commit" = "$target_commit" ] || return 1
    case ",$worker_role," in *,worker,*) ;; *) return 1 ;; esac
    case "$worker_image_id" in sha256:*) ;; *) return 1 ;; esac
    case "$worker_image_name" in *":$target_release_id") ;; *) return 1 ;; esac
    runner_digest="$(alert_canary_runner_digest "$target_release")" || return 1

    if ! compose_project_timed 210 10 \
        "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        run --rm --no-deps -T --entrypoint python backend \
        -m app.scripts.verify_production_issue_alerts \
        --release-id "$target_release_id" --timeout-seconds 180 \
        > "$raw_output"; then
        rm -f "$raw_output" "$output"
        return 1
    fi
    assert_single_active_worker \
        "$target_project" "$target_release/.env" \
        "$target_release/$COMPOSE_FILE" "$target_release_id" || {
        rm -f "$raw_output" "$output"
        return 1
    }
    worker_id_after="$(
        compose_project \
            "$target_project" "$target_release/.env" \
            "$target_release/$COMPOSE_FILE" ps -q worker
    )" || return 1
    if [ "$worker_id_after" != "$worker_id_before" ]; then
        rm -f "$raw_output" "$output"
        return 1
    fi

    if ! python3 - \
        "$raw_output" "$target_backup/production-alert-preflight.json" \
        "$output" "$worker_id_before" "$worker_image_id" \
        "$worker_image_name" "$worker_project" "$runner_digest" \
        "$worker_actor_id" "$worker_release_commit" \
        <<'PY_ALERT_CANARY_MERGE'
import json
from pathlib import Path
import re
import sys

raw_path = Path(sys.argv[1])
preflight_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
raw = json.loads(raw_path.read_text(encoding="utf-8"))
preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
deliveries = raw.get("deliveries")
issue_id = str(raw.get("issue_id", ""))
epoch = raw.get("alert_epoch")
if (
    raw.get("ok") is not True
    or raw.get("schema_version") != 1
    or raw.get("mode") != "delivery"
    or raw.get("status") != "resolved"
    or not raw.get("alerted_at")
    or not raw.get("resolved_at")
    or not isinstance(epoch, int)
    or epoch < 1
    or re.fullmatch(r"[0-9a-f-]{36}", issue_id) is None
    or not isinstance(deliveries, dict)
    or not deliveries
    or set(deliveries) - {"notification", "webhook"}
    or raw.get("sink_config_fingerprint")
    != preflight.get("sink_config_fingerprint")
    or set(deliveries) != set(preflight.get("configured_sinks") or [])
):
    raise SystemExit(1)
for sink, delivery in deliveries.items():
    delivered_by = delivery.get("delivered_by") if isinstance(delivery, dict) else None
    if (
        not isinstance(delivery, dict)
        or delivery.get("status") != "delivered"
        or delivery.get("attribution_version") != 1
        or not isinstance(delivery.get("attempts"), int)
        or delivery["attempts"] < 1
        or delivery.get("error_code") is not None
        or not delivery.get("delivered_at")
        or delivery.get("idempotency_key")
        != f"production-issue:{issue_id}:{epoch}:{sink}"
        or not isinstance(delivered_by, dict)
        or delivered_by.get("worker_actor_id") != sys.argv[9]
        or delivered_by.get("release_id") != raw.get("release_id")
        or delivered_by.get("release_commit") != raw.get("release_commit")
    ):
        raise SystemExit(1)
if raw.get("release_id") != preflight.get("release_id") or raw.get(
    "release_version"
) != preflight.get("release_version") or raw.get("release_commit") != preflight.get(
    "release_commit"
):
    raise SystemExit(1)
worker_id, image_id, image_name, project, runner_digest, actor_id, worker_commit = sys.argv[4:11]
if (
    re.fullmatch(r"[0-9a-f]{64}", worker_id) is None
    or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
    or not image_name.endswith(":" + raw["release_id"])
    or not project
    or re.fullmatch(r"sha256:[0-9a-f]{64}", runner_digest) is None
    or re.fullmatch(r"[0-9a-f-]{36}", actor_id) is None
    or worker_commit != raw["release_commit"]
):
    raise SystemExit(1)
raw["worker_identity"] = {
    "container_id": worker_id,
    "image_id": image_id,
    "image_name": image_name,
    "compose_project": project,
    "worker_actor_id": actor_id,
    "release_id": raw["release_id"],
    "release_commit": worker_commit,
}
raw["runner_sha256"] = runner_digest
output_path.write_text(
    json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY_ALERT_CANARY_MERGE
    then
        rm -f "$raw_output" "$output"
        return 1
    fi
    chmod 0600 "$output"
    rm -f "$raw_output"
}

publish_alert_canary_evidence() {
    local source="$1"
    local release_id="$2"
    local backup="$APP_ROOT/backups/$release_id"
    local evidence="$backup/production-alert-canary.json"
    local digest

    install_durable_file "$source" "$evidence" || return 1
    digest="$(sha256sum "$evidence" | awk '{print $1}')" || return 1
    write_atomic_line "$backup/production-alert-canary.sha256" "$digest"
}

candidate_alert_canary_evidence_valid() {
    local target_release="$1"
    local target_project="$2"
    local target_release_id="$3"
    local backup="$APP_ROOT/backups/$target_release_id"
    local evidence="$backup/production-alert-canary.json"
    local digest_file="$backup/production-alert-canary.sha256"
    local worker_id
    local worker_image_id
    local worker_image_name
    local worker_identity
    local -a worker_identity_fields=()
    local worker_identity_field
    local worker_release_id
    local worker_release_commit
    local worker_actor_id
    local worker_role
    local runner_digest

    alert_canary_requires_v1 "$target_release" || return 1
    [ -f "$evidence" ] && [ ! -L "$evidence" ] || return 1
    [ -f "$digest_file" ] && [ ! -L "$digest_file" ] || return 1
    [ "$(sha256sum "$evidence" | awk '{print $1}')" = \
        "$(tr -d '[:space:]' < "$digest_file")" ] || return 1
    assert_single_active_worker \
        "$target_project" "$target_release/.env" \
        "$target_release/$COMPOSE_FILE" "$target_release_id" || return 1
    worker_id="$(
        compose_project \
            "$target_project" "$target_release/.env" \
            "$target_release/$COMPOSE_FILE" ps -q worker
    )" || return 1
    worker_image_id="$(docker inspect -f '{{.Image}}' "$worker_id")" || return 1
    worker_image_name="$(docker inspect -f '{{.Config.Image}}' "$worker_id")" || return 1
    worker_identity="$(inspect_worker_runtime_identity "$worker_id")" || return 1
    while IFS= read -r worker_identity_field; do
        worker_identity_fields+=("$worker_identity_field")
    done <<< "$worker_identity"
    [ "${#worker_identity_fields[@]}" = "4" ] || return 1
    worker_release_id="${worker_identity_fields[0]}"
    worker_release_commit="${worker_identity_fields[1]}"
    worker_actor_id="${worker_identity_fields[2]}"
    worker_role="${worker_identity_fields[3]}"
    [ "$worker_release_id" = "$target_release_id" ] || return 1
    case ",$worker_role," in *,worker,*) ;; *) return 1 ;; esac
    runner_digest="$(alert_canary_runner_digest "$target_release")" || return 1
    python3 - \
        "$evidence" "$target_release/VERSION" "$target_release/COMMIT" \
        "$target_release_id" "$target_project" "$worker_image_id" \
        "$worker_image_name" "$runner_digest" \
        "$worker_actor_id" "$worker_release_commit" \
        "$backup/production-alert-preflight.json" \
        <<'PY_ALERT_CANARY_VALIDATE'
import json
from pathlib import Path
import re
import sys

evidence = Path(sys.argv[1])
version = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
commit = Path(sys.argv[3]).read_text(encoding="utf-8").strip()
payload = json.loads(evidence.read_text(encoding="utf-8"))
preflight_path = Path(sys.argv[11])
if not preflight_path.is_file() or preflight_path.is_symlink():
    raise SystemExit(1)
preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
worker = payload.get("worker_identity")
deliveries = payload.get("deliveries")
issue_id = str(payload.get("issue_id", ""))
epoch = payload.get("alert_epoch")
if (
    payload.get("ok") is not True
    or payload.get("schema_version") != 1
    or payload.get("mode") != "delivery"
    or payload.get("release_id") != sys.argv[4]
    or payload.get("release_version") != version
    or payload.get("release_commit") != commit
    or preflight.get("ok") is not True
    or preflight.get("schema_version") != 1
    or preflight.get("mode") != "preflight"
    or preflight.get("release_id") != sys.argv[4]
    or preflight.get("release_version") != version
    or preflight.get("release_commit") != commit
    or payload.get("sink_config_fingerprint")
    != preflight.get("sink_config_fingerprint")
    or payload.get("status") != "resolved"
    or not payload.get("alerted_at")
    or not payload.get("resolved_at")
    or re.fullmatch(
        r"hmac-sha256:[0-9a-f]{64}",
        str(payload.get("sink_config_fingerprint", "")),
    )
    is None
    or re.fullmatch(
        r"sha256:[0-9a-f]{64}", str(payload.get("runner_sha256", ""))
    )
    is None
    or payload.get("runner_sha256") != sys.argv[8]
    or not isinstance(worker, dict)
    or worker.get("compose_project") != sys.argv[5]
    or worker.get("image_id") != sys.argv[6]
    or worker.get("image_name") != sys.argv[7]
    or worker.get("worker_actor_id") != sys.argv[9]
    or worker.get("release_id") != sys.argv[4]
    or worker.get("release_commit") != sys.argv[10]
    or sys.argv[10] != commit
    or re.fullmatch(r"[0-9a-f-]{36}", sys.argv[9]) is None
    or re.fullmatch(r"[0-9a-f]{64}", str(worker.get("container_id", ""))) is None
    or not isinstance(epoch, int)
    or epoch < 1
    or re.fullmatch(r"[0-9a-f-]{36}", issue_id) is None
    or not isinstance(deliveries, dict)
    or not deliveries
    or set(deliveries) - {"notification", "webhook"}
    or set(deliveries) != set(preflight.get("configured_sinks") or [])
):
    raise SystemExit(1)
for sink, delivery in deliveries.items():
    delivered_by = delivery.get("delivered_by") if isinstance(delivery, dict) else None
    if (
        not isinstance(delivery, dict)
        or delivery.get("status") != "delivered"
        or delivery.get("attribution_version") != 1
        or not isinstance(delivery.get("attempts"), int)
        or delivery["attempts"] < 1
        or delivery.get("error_code") is not None
        or not delivery.get("delivered_at")
        or delivery.get("idempotency_key")
        != f"production-issue:{issue_id}:{epoch}:{sink}"
        or not isinstance(delivered_by, dict)
        or delivered_by.get("worker_actor_id") != sys.argv[9]
        or delivered_by.get("release_id") != sys.argv[4]
        or delivered_by.get("release_commit") != commit
    ):
        raise SystemExit(1)
PY_ALERT_CANARY_VALIDATE
}

run_candidate_business_smoke() {
    local target_release="$1"
    local target_project="$2"
    local target_release_id="$3"
    local target_version="$4"
    local target_commit="$5"
    local target_port="$6"
    local output="$7"
    local target_backup="$APP_ROOT/backups/$target_release_id"
    local image="astra-browser-smoke:${target_release_id}"
    local frontend_id
    local frontend_health=""
    local api_evidence
    local ui_evidence
    local browser_credentials
    local evidence_nonce
    local runner_bundle_digest
    local browser_image_id
    local host_uid
    local host_gid
    local canonical_api_base="http://127.0.0.1:${target_port}/api"
    local canonical_frontend_url="http://127.0.0.1:${target_port}"

    [ "$RUN_REMOTE_SMOKE" = "1" ] || return 1
    browser_smoke_requires_v2 "$target_release" || return 1
    [ -f "$SMOKE_ENV_FILE" ] && [ ! -L "$SMOKE_ENV_FILE" ] || return 1
    for runner in \
        subscription_production_smoke.py merge_subscription_smoke_evidence.py; do
        [ -f "$target_release/scripts/$runner" ] && \
            [ ! -L "$target_release/scripts/$runner" ] || return 1
    done
    runner_bundle_digest="$(browser_smoke_bundle_digest "$target_release")" || return 1
    browser_image_id="$(docker image inspect --format '{{.Id}}' "$image")" || return 1
    case "$runner_bundle_digest" in sha256:*) ;; *) return 1 ;; esac
    case "$browser_image_id" in sha256:*) ;; *) return 1 ;; esac
    host_uid="$(id -u)" || return 1
    host_gid="$(id -g)" || return 1
    [ "$host_uid" != "0" ] || return 1

    frontend_id="$(
        compose_project \
            "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
            ps -q frontend
    )" || return 1
    if [ -z "$frontend_id" ] || [[ "$frontend_id" == *$'\n'* ]]; then
        return 1
    fi
    for _ in $(seq 1 30); do
        frontend_health="$(
            docker inspect \
                --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                "$frontend_id" 2>/dev/null || true
        )"
        [ "$frontend_health" = "healthy" ] && break
        sleep 1
    done
    [ "$frontend_health" = "healthy" ] || return 1

    mkdir -p "$target_backup"
    [ -d "$target_backup" ] && [ ! -L "$target_backup" ] || return 1
    prepare_browser_smoke_runtime_root || return 1
    BROWSER_SMOKE_TEMP_DIR="$(
        mktemp -d "$BROWSER_SMOKE_RUNTIME_ROOT/.candidate-smoke.XXXXXX"
    )" || return 1
    chmod 0700 "$BROWSER_SMOKE_TEMP_DIR"
    api_evidence="$BROWSER_SMOKE_TEMP_DIR/api.json"
    ui_evidence="$BROWSER_SMOKE_TEMP_DIR/ui.json"
    browser_credentials="$BROWSER_SMOKE_TEMP_DIR/browser-credentials.json"
    evidence_nonce="$(python3 -c 'import secrets; print(secrets.token_hex(16))')" || {
        cleanup_browser_smoke_runtime
        return 1
    }
    python3 - "$SMOKE_ENV_FILE" "$browser_credentials" <<'PY_BROWSER_CREDENTIALS'
import json
import os
from pathlib import Path
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
payload = json.loads(source.read_text(encoding="utf-8"))
keys = ("SMOKE_TENANT_EMAIL", "SMOKE_TENANT_PASSWORD")
if set(payload) != {
    "SMOKE_TENANT_EMAIL",
    "SMOKE_TENANT_PASSWORD",
    "SMOKE_PLATFORM_ADMIN_EMAIL",
    "SMOKE_PLATFORM_ADMIN_PASSWORD",
}:
    raise SystemExit(1)
browser_payload = {key: payload[key] for key in keys}
if any(not isinstance(value, str) or not 0 < len(value) <= 4096 for value in browser_payload.values()):
    raise SystemExit(1)
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(browser_payload, handle, ensure_ascii=False, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY_BROWSER_CREDENTIALS

    if ! python3 "$target_release/scripts/subscription_production_smoke.py" \
        --credentials-file "$SMOKE_ENV_FILE" \
        --api-base "$canonical_api_base" \
        --frontend-url "$canonical_frontend_url" \
        --expected-version "$target_version" \
        --expected-commit "$target_commit" \
        --expected-release-id "$target_release_id" \
        --evidence-nonce "$evidence_nonce" \
        > "$api_evidence"; then
        cleanup_browser_smoke_runtime
        return 1
    fi

    BROWSER_SMOKE_NETWORK="astra-browser-smoke-${evidence_nonce}"
    BROWSER_SMOKE_CONTAINER="astra-browser-smoke-${evidence_nonce}"
    BROWSER_SMOKE_FRONTEND_ID="$frontend_id"
    docker network create \
        --internal \
        --label "ai.reeftotem.astra.browser-smoke-release=$target_release_id" \
        "$BROWSER_SMOKE_NETWORK" >/dev/null || {
        cleanup_browser_smoke_runtime
        return 1
    }
    docker network connect \
        --alias candidate-frontend \
        "$BROWSER_SMOKE_NETWORK" "$frontend_id" || {
        cleanup_browser_smoke_runtime
        return 1
    }
    if ! timeout --signal=TERM --kill-after=10s 150s \
        docker run --rm \
        --name "$BROWSER_SMOKE_CONTAINER" \
        --label "ai.reeftotem.astra.browser-smoke-release=$target_release_id" \
        --init \
        --user "${host_uid}:${host_gid}" \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --security-opt "seccomp=$target_release/deploy/browser-smoke/seccomp_profile.json" \
        --pids-limit 256 \
        --ulimit nofile=1024:1024 \
        --memory 1g \
        --cpus 1.0 \
        --shm-size 256m \
        --tmpfs /tmp:rw,nosuid,nodev,size=256m \
        --tmpfs /home/pwuser:rw,nosuid,nodev,size=64m \
        --env HOME=/home/pwuser \
        --network "$BROWSER_SMOKE_NETWORK" \
        --mount \
            "type=bind,src=$browser_credentials,dst=/run/secrets/smoke-credentials.json,readonly" \
        "$image" \
        --frontend-url "http://candidate-frontend:3000" \
        --evidence-frontend-url "$canonical_frontend_url" \
        --credentials-file /run/secrets/smoke-credentials.json \
        --expected-version "$target_version" \
        --expected-commit "$target_commit" \
        --expected-release-id "$target_release_id" \
        --evidence-nonce "$evidence_nonce" \
        > "$ui_evidence"; then
        cleanup_browser_smoke_runtime
        return 1
    fi
    cleanup_browser_smoke_runtime 0
    if ! python3 "$target_release/scripts/merge_subscription_smoke_evidence.py" \
        --api-evidence "$api_evidence" \
        --ui-evidence "$ui_evidence" \
        --api-base "$canonical_api_base" \
        --frontend-url "$canonical_frontend_url" \
        --expected-version "$target_version" \
        --expected-commit "$target_commit" \
        --expected-release-id "$target_release_id" \
        --evidence-nonce "$evidence_nonce" \
        --runner-bundle-sha256 "$runner_bundle_digest" \
        --browser-image-id "$browser_image_id" \
        > "$output"; then
        cleanup_browser_smoke_runtime
        rm -f "$output"
        return 1
    fi
    chmod 0600 "$output"
    cleanup_browser_smoke_runtime
}

candidate_business_evidence_valid() {
    local release_id="$1"
    local candidate_port="$2"
    local target_release="$APP_ROOT/releases/$release_id"
    local evidence_mode="legacy"
    local runner_bundle_sha256="none"

    [ -d "$target_release" ] && [ ! -L "$target_release" ] || return 1
    if browser_smoke_requires_v2 "$target_release"; then
        evidence_mode="v2"
        runner_bundle_sha256="$(browser_smoke_bundle_digest "$target_release")" || return 1
    fi
    python3 - \
        "$APP_ROOT" "$release_id" "$candidate_port" "$evidence_mode" \
        "$runner_bundle_sha256" <<'PY_CANDIDATE_EVIDENCE'
import hashlib
import json
from pathlib import Path
import re
import sys

app_root = Path(sys.argv[1])
release_id = sys.argv[2]
candidate_port = sys.argv[3]
evidence_mode = sys.argv[4]
runner_bundle_sha256 = sys.argv[5]
backup = app_root / "backups" / release_id
release = app_root / "releases" / release_id
marker = backup / "candidate-business-verification"

if not release.is_dir() or release.is_symlink() or not backup.is_dir() or backup.is_symlink():
    raise SystemExit(1)
version_path = release / "VERSION"
commit_path = release / "COMMIT"
if (
    not version_path.is_file()
    or version_path.is_symlink()
    or not commit_path.is_file()
    or commit_path.is_symlink()
    or not marker.is_file()
    or marker.is_symlink()
):
    raise SystemExit(1)
version = version_path.read_text(encoding="utf-8").strip()
commit = commit_path.read_text(encoding="utf-8").strip()
if not version or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
    raise SystemExit(1)
value = marker.read_text(encoding="utf-8").strip()
kind, separator, digest = value.partition(":")
if separator != ":" or not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit(1)

if kind in {"smoke", "smoke-v2"}:
    evidence = backup / "subscription-smoke.candidate.json"
    if not evidence.is_file() or evidence.is_symlink():
        raise SystemExit(1)
    raw = evidence.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SystemExit(1)
    payload = json.loads(raw)
    if evidence_mode == "v2":
        required_checks = {
            "candidate_release_identity_ok",
            "tenant_login_ok",
            "tenant_me_ok",
            "client_plans_ok",
            "client_subscription_summary_ok",
            "client_credit_transactions_ok",
            "client_orders_ok",
            "client_credit_packs_ok",
            "platform_admin_login_ok",
            "saas_ledger_reconciliation_ok",
            "saas_payment_reconciliation_ok",
            "orders_csv_export_ok",
            "credit_transactions_csv_export_ok",
            "ui_release_identity_ok",
            "ui_tenant_login_ok",
            "ui_subscription_summary_api_ok",
            "ui_subscription_balance_rendered_ok",
            "ui_subscription_page_ok",
            "ui_no_server_error_ok",
        }
        expected_api_base = f"http://127.0.0.1:{candidate_port}/api"
        expected_frontend_url = f"http://127.0.0.1:{candidate_port}"
        if kind != "smoke-v2":
            raise SystemExit(1)
        if (
            payload.get("evidence_schema_version") != 2
            or payload.get("evidence_kind") != "subscription_composite"
            or payload.get("ok") is not True
            or payload.get("api_base") != expected_api_base
            or payload.get("frontend_url") != expected_frontend_url
            or payload.get("release_identity")
            != {"version": version, "commit": commit, "release_id": release_id}
            or re.fullmatch(r"[0-9a-f]{32}", str(payload.get("evidence_nonce", ""))) is None
        ):
            raise SystemExit(1)
        browser_gate = payload.get("browser_gate")
        if (
            not isinstance(browser_gate, dict)
            or browser_gate.get("runner_bundle_sha256") != runner_bundle_sha256
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(browser_gate.get("image_id", ""))) is None
        ):
            raise SystemExit(1)
        ui = payload.get("ui")
        if not isinstance(ui, dict) or ui.get("final_path") != "/account/subscription" or \
                ui.get("browser_target") != "isolated_candidate_frontend_network":
            raise SystemExit(1)
        summary = payload.get("subscription_summary")
        if not isinstance(summary, dict) or not {
            "plan_code", "balance", "available_balance", "reserved"
        }.issubset(summary):
            raise SystemExit(1)
        for field in ("balance", "available_balance", "reserved"):
            value = summary.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SystemExit(1)
        for key, checked_field in (
            ("saas_ledger_reconciliation", "checked_tenants"),
            ("saas_payment_reconciliation", "checked_orders"),
        ):
            reconciliation = payload.get(key)
            if (
                not isinstance(reconciliation, dict)
                or set(reconciliation) != {checked_field, "issue_count"}
                or isinstance(reconciliation.get(checked_field), bool)
                or not isinstance(reconciliation.get(checked_field), int)
                or reconciliation[checked_field] < 0
                or reconciliation.get("issue_count") != 0
            ):
                raise SystemExit(1)
        if not required_checks.issubset(set(payload.get("checks") or [])):
            raise SystemExit(1)
    else:
        required_checks = {
            "tenant_login_ok",
            "tenant_me_ok",
            "client_subscription_summary_ok",
            "client_credit_transactions_ok",
            "client_orders_ok",
            "platform_admin_login_ok",
            "saas_ledger_reconciliation_ok",
            "saas_payment_reconciliation_ok",
            "orders_csv_export_ok",
            "credit_transactions_csv_export_ok",
        }
        if kind != "smoke" or payload.get("ok") is not True:
            raise SystemExit(1)
        if payload.get("api_base") != f"http://127.0.0.1:{candidate_port}/api":
            raise SystemExit(1)
        if not required_checks.issubset(set(payload.get("checks") or [])):
            raise SystemExit(1)
elif kind == "break-glass":
    approval = backup / "remote-smoke-break-glass.approval"
    recorded_digest = backup / "remote-smoke-break-glass.sha256"
    if (
        not approval.is_file()
        or approval.is_symlink()
        or not recorded_digest.is_file()
        or recorded_digest.is_symlink()
    ):
        raise SystemExit(1)
    if recorded_digest.read_text(encoding="utf-8").strip() != digest:
        raise SystemExit(1)
    if hashlib.sha256(approval.read_bytes()).hexdigest() != digest:
        raise SystemExit(1)
    if evidence_mode == "v2":
        fields = {}
        for line in approval.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() in fields:
                    raise SystemExit(1)
                fields[key.strip()] = value.strip()
        if {item.strip() for item in fields.get("bypassed_gates", "").split(",") if item.strip()} != {
            "subscription_api",
            "subscription_browser",
        }:
            raise SystemExit(1)
else:
    raise SystemExit(1)
PY_CANDIDATE_EVIDENCE
}

regenerate_candidate_business_evidence() {
    local target_release="$1"
    local target_release_id="$2"
    local target_port="$3"
    local target_project="$4"
    local target_backup="$APP_ROOT/backups/$target_release_id"
    local temporary_evidence
    local evidence_sha256
    local target_version
    local target_commit

    if [ "$RUN_REMOTE_SMOKE" != "1" ]; then
        echo "cannot regenerate recovery evidence without authenticated remote smoke" >&2
        return 1
    fi
    if [ ! -f "$SMOKE_ENV_FILE" ] || [ -L "$SMOKE_ENV_FILE" ]; then
        echo "recovery smoke credential file is missing or unsafe" >&2
        return 1
    fi
    if ! browser_smoke_requires_v2 "$target_release"; then
        echo "legacy recovery evidence cannot be regenerated without its original verified artifact" >&2
        return 1
    fi
    target_version="$(tr -d '[:space:]' < "$target_release/VERSION")" || return 1
    target_commit="$(tr -d '[:space:]' < "$target_release/COMMIT")" || return 1
    mkdir -p "$target_backup"
    if [ ! -d "$target_backup" ] || [ -L "$target_backup" ]; then
        echo "recovery evidence directory is unsafe" >&2
        return 1
    fi
    temporary_evidence="$(
        mktemp "$target_backup/.subscription-smoke.candidate.recovery.XXXXXX"
    )" || return 1
    umask 077
    if ! run_candidate_business_smoke \
        "$target_release" "$target_project" "$target_release_id" \
        "$target_version" "$target_commit" "$target_port" \
        "$temporary_evidence"; then
        rm -f "$temporary_evidence"
        echo "authenticated API/browser recovery smoke failed" >&2
        return 1
    fi
    install_durable_file \
        "$temporary_evidence" "$target_backup/subscription-smoke.candidate.json" || return 1
    evidence_sha256="$(
        sha256sum "$target_backup/subscription-smoke.candidate.json" | awk '{print $1}'
    )" || return 1
    write_atomic_line "$target_backup/candidate-business-verification" \
        "smoke-v2:${evidence_sha256}" || return 1
    echo "authenticated recovery API/browser smoke passed"
    candidate_business_evidence_valid "$target_release_id" "$target_port"
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

compose_project_timed() {
    local duration_seconds="$1"
    local kill_after_seconds="$2"
    local project="$3"
    local env_file="$4"
    local compose_file="$5"
    shift 5
    if [ -n "$COMPOSE_PROFILES" ]; then
        timeout --signal=TERM --kill-after="${kill_after_seconds}s" \
            "${duration_seconds}s" \
            docker compose --env-file "$env_file" \
            --profile "$COMPOSE_PROFILES" -p "$project" \
            -f "$compose_file" "$@"
    else
        timeout --signal=TERM --kill-after="${kill_after_seconds}s" \
            "${duration_seconds}s" \
            docker compose --env-file "$env_file" -p "$project" \
            -f "$compose_file" "$@"
    fi
}

approval_schema_forward_state() {
    local release="$1"
    local postgres_user
    local postgres_db
    local result

    postgres_user="$(read_release_env "$release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$release" POSTGRES_DB astra)" || return 1
    result="$(
        compose_project \
            "$COMPOSE_PROJECT" "$release/.env" "$release/$COMPOSE_FILE" \
            exec -T postgres psql -qAt -v ON_ERROR_STOP=1 \
            -U "$postgres_user" -d "$postgres_db" \
            -c "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_approval_execution_state_consistency' AND conrelid = 'approval_requests'::regclass;"
    )" || return 1
    case "$result" in
        0|1) printf '%s' "$result" ;;
        *) return 1 ;;
    esac
}

agentbay_cleanup_required_count() {
    local release="$1"
    local postgres_user
    local postgres_db
    local result

    postgres_user="$(read_release_env "$release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$release" POSTGRES_DB astra)" || return 1
    result="$(
        compose_project \
            "$COMPOSE_PROJECT" "$release/.env" "$release/$COMPOSE_FILE" \
            exec -T postgres psql -qAt -v ON_ERROR_STOP=1 \
            -U "$postgres_user" -d "$postgres_db" \
            -c "SELECT count(*) FROM agentbay_session_ledger WHERE status = 'cleanup_required';"
    )" || return 1
    case "$result" in
        ''|*[!0-9]*) return 1 ;;
        *) printf '%s' "$result" ;;
    esac
}

agentbay_unresolved_count() {
    local release="$1"
    local postgres_user
    local postgres_db
    local result

    postgres_user="$(read_release_env "$release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$release" POSTGRES_DB astra)" || return 1
    result="$(
        compose_project \
            "$COMPOSE_PROJECT" "$release/.env" "$release/$COMPOSE_FILE" \
            exec -T postgres psql -qAt -v ON_ERROR_STOP=1 \
            -U "$postgres_user" -d "$postgres_db" \
            -c "SELECT count(*) FROM agentbay_session_ledger WHERE status IN ('active', 'cleanup_required', 'provider_identity_collision');"
    )" || return 1
    case "$result" in
        ''|*[!0-9]*) return 1 ;;
        *) printf '%s' "$result" ;;
    esac
}

reconcile_agentbay_for_cutover() {
    local project="$1"
    local release="$2"

    compose_project \
        "$project" "$release/.env" "$release/$COMPOSE_FILE" \
        run --rm --no-deps -T \
        -e AGENTBAY_RECONCILE_DEADLINE_SECONDS=120 \
        --entrypoint python backend \
        -m app.scripts.reconcile_agentbay_cleanup < /dev/null || return 1
    [ "$(agentbay_unresolved_count "$release")" = "0" ]
}

verify_public_maintenance() {
    local headers
    headers="$(curl -sS -D - -o /dev/null "$PUBLIC_URL/api/health?maintenance=$RELEASE_ID" | tr -d '\r')" || return 1
    printf '%s\n' "$headers" | head -1 | grep -Eq '^HTTP/[0-9.]+ 503([[:space:]]|$)' || return 1
    printf '%s\n' "$headers" | grep -Eqi '^Retry-After:[[:space:]]*60[[:space:]]*$' || return 1
    printf '%s\n' "$headers" | grep -Eqi '^Cache-Control:[[:space:]]*"?no-store"?[[:space:]]*$' || return 1
}

enable_web_maintenance() {
    # Arm rollback before the first persistent Nginx mutation. If validation
    # fails after maintenance-on writes the files, rollback must never leave a
    # latent 503 configuration that appears on a later reload/restart.
    NGINX_CONFIG_TOUCHED=1
    sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        maintenance-on "$NGINX_SITE" "$NGINX_LOG_FORMAT" || return 1
    sudo nginx -t >/dev/null || return 1
    audit_effective_nginx >/dev/null || return 1
    sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        maintenance-status "$NGINX_SITE" >/dev/null || return 1
    reload_nginx_with_worker_snapshot || return 1
    NGINX_WORKER_GRACE_SECONDS=60 retire_pre_reload_nginx_workers || return 1
    verify_public_maintenance || return 1
    MAINTENANCE_ENABLED=1
}

preserve_forward_only_maintenance() {
    echo "[remote] schema is forward-only; refusing to restore the previous application" >&2
    sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        maintenance-on "$NGINX_SITE" "$NGINX_LOG_FORMAT" || return 1
    sudo nginx -t >/dev/null || return 1
    audit_effective_nginx >/dev/null || return 1
    reload_nginx_with_worker_snapshot || return 1
    compose_project \
        "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" \
        stop --timeout 30 worker frontend backend >/dev/null 2>&1 || true
    MAINTENANCE_ENABLED=1
    write_cutover_state schema_forward_only "$CANDIDATE_SLOT" "$RELEASE_ID"
}

release_has_mcp_assignment_contract() {
    test -f "$1/deploy/security-contracts/mcp-assignment-v1"
}

read_release_env() {
    local release="$1"
    local key="$2"
    local default="$3"
    local value
    value="$(grep -E "^${key}=" "$release/.env" | tail -1 | cut -d= -f2- | sed -E 's/^"//; s/"$//')"
    if [ -z "$value" ]; then
        value="$default"
    fi
    printf '%s' "$value"
}

mcp_endpoint_preflight() {
    local release="$1"
    local postgres_user
    local postgres_db
    local counts
    local non_https
    local userinfo
    local fragments
    local query_secrets

    postgres_user="$(read_release_env "$release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$release" POSTGRES_DB astra)" || return 1
    counts="$(
        compose_project \
            "$COMPOSE_PROJECT" "$release/.env" "$release/$COMPOSE_FILE" \
            exec -T postgres psql -v ON_ERROR_STOP=1 -At \
            -U "$postgres_user" -d "$postgres_db" <<'SQL_MCP_PREFLIGHT'
SELECT
    count(*) FILTER (
        WHERE mcp_server_url IS NOT NULL
          AND lower(mcp_server_url) !~ '^https://'
    ),
    count(*) FILTER (
        WHERE mcp_server_url ~* '^https://[^/?#]*@'
    ),
    count(*) FILTER (
        WHERE position('#' in mcp_server_url) > 0
    ),
    count(*) FILTER (
        WHERE EXISTS (
            SELECT 1
            FROM regexp_split_to_table(
                split_part(split_part(mcp_server_url, '?', 2), '#', 1),
                '&'
            ) AS query_pair
            CROSS JOIN LATERAL (
                SELECT trim(BOTH '_' FROM lower(
                    regexp_replace(
                        regexp_replace(
                            split_part(query_pair, '=', 1),
                            '([a-z0-9])([A-Z])',
                            '\1_\2',
                            'g'
                        ),
                        '[^a-zA-Z0-9]+',
                        '_',
                        'g'
                    )
                )) AS normalized_key
            ) AS query_key
            WHERE query_key.normalized_key IN (
                'api_key', 'apikey', 'auth', 'authorization',
                'credential', 'key', 'password', 'passwd', 'secret',
                'sig', 'signature', 'token'
            )
               OR query_key.normalized_key ~
                    '(_api_key|_access_key|_credential|_key|_password|_secret|_sig|_signature|_token|_apikey)$'
        )
    )
FROM tools
WHERE type = 'mcp';
SQL_MCP_PREFLIGHT
    )" || return 1
    IFS='|' read -r non_https userinfo fragments query_secrets <<< "$counts"
    for count in "$non_https" "$userinfo" "$fragments" "$query_secrets"; do
        case "$count" in
            ''|*[!0-9]*)
                echo "invalid MCP endpoint preflight result" >&2
                return 1
                ;;
        esac
    done
    if [ "$non_https" != "0" ] || [ "$userinfo" != "0" ] || \
        [ "$fragments" != "0" ] || [ "$query_secrets" != "0" ]; then
        echo "MCP endpoint preflight failed: non_https=$non_https userinfo=$userinfo fragments=$fragments query_secrets=$query_secrets" >&2
        return 1
    fi
}

model_route_credential_preflight() {
    local release="$1"
    local postgres_user
    local postgres_db
    local counts
    local ambiguous_routes
    local disabled_models
    local invalid_fallbacks
    local tenant_owned_route_models
    local missing_minimax_capabilities
    local fallback_cycles

    postgres_user="$(read_release_env "$release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$release" POSTGRES_DB astra)" || return 1
    counts="$(
        compose_project \
            "$COMPOSE_PROJECT" "$release/.env" "$release/$COMPOSE_FILE" \
            exec -T postgres psql -v ON_ERROR_STOP=1 -At \
            -U "$postgres_user" -d "$postgres_db" <<'SQL_MODEL_ROUTE_PREFLIGHT'
WITH expected_minimax_capability(modality) AS (
    VALUES ('text'), ('image'), ('video')
), route_duplicates AS (
    SELECT 1
    FROM model_routes
    WHERE enabled IS TRUE
    GROUP BY saas_tier, modality, priority
    HAVING COUNT(*) > 1
), broken_primary AS (
    SELECT 1
    FROM model_routes AS route
    LEFT JOIN llm_models AS model ON model.id = route.llm_model_id
    WHERE route.enabled IS TRUE
      AND (
          model.id IS NULL
          OR model.enabled IS NOT TRUE
          OR model.tenant_id IS NOT NULL
          OR NOT (
              (
                  jsonb_array_length(
                      COALESCE(model.modalities::jsonb, '[]'::jsonb)
                  ) > 0
                  AND (
                      jsonb_exists(model.modalities::jsonb, lower(route.modality))
                      OR jsonb_exists(model.modalities::jsonb, 'multimodal')
                      OR (
                          lower(route.modality) = 'image'
                          AND jsonb_exists(model.modalities::jsonb, 'vision')
                      )
                  )
              )
              OR (
                  jsonb_array_length(
                      COALESCE(model.modalities::jsonb, '[]'::jsonb)
                  ) = 0
                  AND (
                      lower(COALESCE(model.modality, '')) IN (
                          lower(route.modality), 'multimodal'
                      )
                      OR (
                          lower(route.modality) = 'image'
                          AND lower(COALESCE(model.modality, '')) = 'vision'
                      )
                  )
              )
              OR (
                  lower(route.modality) = 'image'
                  AND model.supports_vision IS TRUE
              )
          )
      )
), broken_fallback AS (
    SELECT 1
    FROM model_routes AS route
    LEFT JOIN model_routes AS fallback ON fallback.id = route.fallback_route_id
    LEFT JOIN llm_models AS fallback_model ON fallback_model.id = fallback.llm_model_id
    WHERE route.enabled IS TRUE
      AND route.fallback_route_id IS NOT NULL
      AND (
          fallback.id IS NULL
          OR fallback.enabled IS NOT TRUE
          OR fallback.saas_tier <> route.saas_tier
          OR fallback.modality <> route.modality
          OR fallback_model.id IS NULL
          OR fallback_model.enabled IS NOT TRUE
          OR fallback_model.tenant_id IS NOT NULL
          OR NOT (
              (
                  jsonb_array_length(
                      COALESCE(fallback_model.modalities::jsonb, '[]'::jsonb)
                  ) > 0
                  AND (
                      jsonb_exists(
                          fallback_model.modalities::jsonb,
                          lower(fallback.modality)
                      )
                      OR jsonb_exists(
                          fallback_model.modalities::jsonb,
                          'multimodal'
                      )
                      OR (
                          lower(fallback.modality) = 'image'
                          AND jsonb_exists(
                              fallback_model.modalities::jsonb,
                              'vision'
                          )
                      )
                  )
              )
              OR (
                  jsonb_array_length(
                      COALESCE(fallback_model.modalities::jsonb, '[]'::jsonb)
                  ) = 0
                  AND (
                      lower(COALESCE(fallback_model.modality, '')) IN (
                          lower(fallback.modality), 'multimodal'
                      )
                      OR (
                          lower(fallback.modality) = 'image'
                          AND lower(COALESCE(fallback_model.modality, '')) = 'vision'
                      )
                  )
              )
              OR (
                  lower(fallback.modality) = 'image'
                  AND fallback_model.supports_vision IS TRUE
              )
          )
          OR fallback.id = route.id
          OR fallback.fallback_route_id = route.id
      )
), missing_capability AS (
    SELECT expected.modality
    FROM expected_minimax_capability AS expected
    WHERE NOT EXISTS (
        SELECT 1
        FROM llm_credentials AS credential
        WHERE lower(credential.provider) = 'minimax'
          AND credential.tenant_id IS NULL
          AND credential.enabled IS TRUE
          AND credential.status = 'healthy'
          AND (credential.daily_quota IS NULL OR credential.used_today < credential.daily_quota)
          AND (
              credential.capabilities IS NULL
              OR cast(credential.capabilities AS jsonb) @> to_jsonb(ARRAY[expected.modality]::text[])
              OR cast(credential.capabilities AS jsonb) @> '["multimodal"]'::jsonb
              OR (
                  expected.modality = 'image'
                  AND cast(credential.capabilities AS jsonb) @> '["vision"]'::jsonb
              )
          )
          AND COALESCE(credential.modality_status::jsonb -> 'plan' ->> 'status', '') <> 'quota_exceeded'
    )
), fallback_walk(start_id, id, fallback_route_id, path, cycle) AS (
    SELECT route.id,
           route.id,
           route.fallback_route_id,
           ARRAY[route.id]::uuid[],
           false
    FROM model_routes AS route
    WHERE route.enabled IS TRUE
    UNION ALL
    SELECT fallback_walk.start_id,
           fallback.id,
           fallback.fallback_route_id,
           fallback_walk.path || fallback.id,
           fallback.id = ANY(fallback_walk.path)
    FROM fallback_walk
    JOIN model_routes AS fallback
      ON fallback.id = fallback_walk.fallback_route_id
     AND fallback.enabled IS TRUE
    WHERE fallback_walk.cycle IS FALSE
), fallback_cycle AS (
    SELECT 1 FROM fallback_walk WHERE cycle IS TRUE
), non_platform_route_model AS (
    SELECT 1
    FROM model_routes AS route
    JOIN llm_models AS model ON model.id = route.llm_model_id
    WHERE model.tenant_id IS NOT NULL
)
SELECT
    (SELECT COUNT(*) FROM route_duplicates),
    (SELECT COUNT(*) FROM broken_primary),
    (SELECT COUNT(*) FROM broken_fallback),
    (SELECT COUNT(*) FROM non_platform_route_model),
    (SELECT COUNT(*) FROM missing_capability),
    (SELECT COUNT(*) FROM fallback_cycle);
SQL_MODEL_ROUTE_PREFLIGHT
    )" || return 1
    IFS='|' read -r ambiguous_routes disabled_models invalid_fallbacks \
        tenant_owned_route_models missing_minimax_capabilities \
        fallback_cycles <<< "$counts"
    for count in "$ambiguous_routes" "$disabled_models" \
        "$invalid_fallbacks" "$tenant_owned_route_models" \
        "$missing_minimax_capabilities" "$fallback_cycles"; do
        case "$count" in
            ''|*[!0-9]*)
                echo "invalid model-route credential preflight result" >&2
                return 1
                ;;
        esac
    done
    if [ "$ambiguous_routes" != "0" ] || [ "$disabled_models" != "0" ] || \
        [ "$invalid_fallbacks" != "0" ] || \
        [ "$tenant_owned_route_models" != "0" ] || \
        [ "$missing_minimax_capabilities" != "0" ] || \
        [ "$fallback_cycles" != "0" ]; then
        echo "model-route credential preflight failed: ambiguous_routes=$ambiguous_routes disabled_models=$disabled_models invalid_fallbacks=$invalid_fallbacks tenant_owned_route_models=$tenant_owned_route_models missing_minimax_capabilities=$missing_minimax_capabilities fallback_cycles=$fallback_cycles" >&2
        echo "Verify the platform MiniMax credential and explicitly enable text/image/video capabilities in the SaaS owner console before release." >&2
        return 1
    fi
}

m3_route_post_migration_preflight() {
    local release="$1"
    local postgres_user
    local postgres_db
    local invalid_count

    postgres_user="$(read_release_env "$release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$release" POSTGRES_DB astra)" || return 1
    invalid_count="$(
        compose_project \
            "$COMPOSE_PROJECT" "$release/.env" "$release/$COMPOSE_FILE" \
            exec -T postgres psql -v ON_ERROR_STOP=1 -At \
            -U "$postgres_user" -d "$postgres_db" <<'SQL_M3_ROUTE_POSTFLIGHT'
WITH expected(tier, modality, route_id, model_id) AS (
    VALUES
      ('lite', 'text',  '09300000-0000-4000-8000-000000000101'::uuid, '09300000-0000-4000-8000-000000000001'::uuid),
      ('lite', 'image', '09300000-0000-4000-8000-000000000102'::uuid, '09300000-0000-4000-8000-000000000001'::uuid),
      ('lite', 'video', '09300000-0000-4000-8000-000000000103'::uuid, '09300000-0000-4000-8000-000000000001'::uuid),
      ('pro', 'text',   '09300000-0000-4000-8000-000000000104'::uuid, '09300000-0000-4000-8000-000000000002'::uuid),
      ('pro', 'image',  '09300000-0000-4000-8000-000000000105'::uuid, '09300000-0000-4000-8000-000000000002'::uuid),
      ('pro', 'video',  '09300000-0000-4000-8000-000000000106'::uuid, '09300000-0000-4000-8000-000000000002'::uuid),
      ('ultra', 'text', '09300000-0000-4000-8000-000000000107'::uuid, '09300000-0000-4000-8000-000000000003'::uuid),
      ('ultra', 'image','09300000-0000-4000-8000-000000000108'::uuid, '09300000-0000-4000-8000-000000000003'::uuid),
      ('ultra', 'video','09300000-0000-4000-8000-000000000109'::uuid, '09300000-0000-4000-8000-000000000003'::uuid)
), ranked AS (
    SELECT route.*,
           row_number() OVER (
               PARTITION BY route.saas_tier, route.modality
               ORDER BY route.priority DESC, route.created_at ASC, route.id ASC
           ) AS rank
    FROM model_routes AS route
    WHERE route.enabled IS TRUE
), invalid AS (
    SELECT expected.route_id
    FROM expected
    LEFT JOIN model_routes AS route ON route.id = expected.route_id
    LEFT JOIN llm_models AS model ON model.id = route.llm_model_id
    LEFT JOIN ranked AS top_route
      ON top_route.saas_tier = expected.tier
     AND top_route.modality = expected.modality
     AND top_route.rank = 1
    WHERE route.id IS NULL
       OR route.enabled IS NOT TRUE
       OR route.saas_tier <> expected.tier
       OR route.modality <> expected.modality
       OR route.llm_model_id <> expected.model_id
       OR top_route.id <> expected.route_id
       OR model.provider <> 'minimax'
       OR model.model <> 'MiniMax-M3'
       OR model.enabled IS NOT TRUE
       OR model.tenant_id IS NOT NULL
       OR model.supports_vision IS NOT TRUE
       OR NOT (model.modalities::jsonb @> '["text","image","video"]'::jsonb)
)
SELECT COUNT(*) FROM invalid;
SQL_M3_ROUTE_POSTFLIGHT
    )" || return 1
    case "$invalid_count" in
        ''|*[!0-9]*)
            echo "invalid M3 post-migration preflight result" >&2
            return 1
            ;;
    esac
    if [ "$invalid_count" != "0" ]; then
        echo "M3 post-migration preflight failed: invalid_exact_top_routes=$invalid_count" >&2
        return 1
    fi
}

quarantine_mcp_for_unsafe_release() {
    local target_release="$1"
    local snapshot_id="$2"
    local postgres_user
    local postgres_db
    local effective_snapshot

    if release_has_mcp_assignment_contract "$target_release"; then
        return 0
    fi
    case "$snapshot_id" in
        ''|*[!A-Za-z0-9._-]*)
            echo "invalid MCP quarantine snapshot identity" >&2
            return 1
            ;;
    esac
    postgres_user="$(read_release_env "$target_release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$target_release" POSTGRES_DB astra)" || return 1
    echo "[remote] previous release lacks MCP assignment enforcement; quarantining MCP before rollback" >&2
    effective_snapshot="$(compose_project \
        "$COMPOSE_PROJECT" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        exec -T postgres psql -qAt -v ON_ERROR_STOP=1 \
        -v snapshot_id="$snapshot_id" -U "$postgres_user" -d "$postgres_db" \
        <<'SQL_MCP_QUARANTINE'
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('astra-deploy-mcp-quarantine-v1'));
LOCK TABLE tools IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE agent_tools IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS astra_deploy_mcp_quarantine_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    snapshot_id text NOT NULL UNIQUE,
    quarantined_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS astra_deploy_mcp_quarantine_tools (
    snapshot_id text NOT NULL,
    tool_id uuid NOT NULL,
    enabled boolean NOT NULL,
    mcp_server_url text,
    config jsonb NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id, tool_id)
);
CREATE TABLE IF NOT EXISTS astra_deploy_mcp_quarantine_assignments (
    snapshot_id text NOT NULL,
    agent_tool_id uuid NOT NULL,
    enabled boolean NOT NULL,
    config jsonb NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id, agent_tool_id)
);
CREATE TEMP TABLE astra_new_mcp_quarantine_snapshot
ON COMMIT DROP AS
WITH inserted AS (
    INSERT INTO astra_deploy_mcp_quarantine_state (singleton, snapshot_id)
    VALUES (true, :'snapshot_id')
    ON CONFLICT (singleton) DO NOTHING
    RETURNING snapshot_id
)
SELECT snapshot_id FROM inserted;
INSERT INTO astra_deploy_mcp_quarantine_tools (
    snapshot_id, tool_id, enabled, mcp_server_url, config
)
SELECT snapshot.snapshot_id, tool.id, tool.enabled, tool.mcp_server_url,
       COALESCE(tool.config::jsonb, '{}'::jsonb)
FROM tools AS tool
CROSS JOIN astra_new_mcp_quarantine_snapshot AS snapshot
WHERE tool.type = 'mcp'
ON CONFLICT (snapshot_id, tool_id) DO NOTHING;
INSERT INTO astra_deploy_mcp_quarantine_assignments (
    snapshot_id, agent_tool_id, enabled, config
)
SELECT snapshot.snapshot_id, agent_tools.id, agent_tools.enabled,
       COALESCE(agent_tools.config::jsonb, '{}'::jsonb)
FROM agent_tools
JOIN tools ON tools.id = agent_tools.tool_id
CROSS JOIN astra_new_mcp_quarantine_snapshot AS snapshot
WHERE tools.type = 'mcp'
ON CONFLICT (snapshot_id, agent_tool_id) DO NOTHING;
CREATE OR REPLACE FUNCTION astra_deploy_guard_mcp_tools()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('astra.mcp_quarantine_restore', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM astra_deploy_mcp_quarantine_state WHERE singleton = true
    ) THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.type = 'mcp' THEN
            RETURN NULL;
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.type = 'mcp' OR (TG_OP = 'UPDATE' AND OLD.type = 'mcp') THEN
        IF TG_OP = 'UPDATE' AND OLD.type = 'mcp' THEN
            NEW.type := 'mcp';
            NEW.source := OLD.source;
            NEW.tenant_id := OLD.tenant_id;
            NEW.name := OLD.name;
            NEW.display_name := OLD.display_name;
            NEW.description := OLD.description;
            NEW.category := OLD.category;
            NEW.icon := OLD.icon;
            NEW.parameters_schema := OLD.parameters_schema;
            NEW.mcp_server_name := OLD.mcp_server_name;
            NEW.mcp_tool_name := OLD.mcp_tool_name;
            NEW.config_schema := OLD.config_schema;
            NEW.is_default := OLD.is_default;
        ELSE
            NEW.is_default := false;
        END IF;
        NEW.enabled := false;
        NEW.mcp_server_url := NULL;
        NEW.config := '{}'::json;
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE FUNCTION astra_deploy_guard_mcp_assignments()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    guarded_tool_is_mcp boolean;
    old_tool_is_mcp boolean := false;
BEGIN
    IF current_setting('astra.mcp_quarantine_restore', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM astra_deploy_mcp_quarantine_state WHERE singleton = true
    ) THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT type = 'mcp' INTO guarded_tool_is_mcp
        FROM tools WHERE id = NEW.tool_id;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT type = 'mcp' INTO guarded_tool_is_mcp
        FROM tools WHERE id = OLD.tool_id;
    ELSE
        SELECT EXISTS (
            SELECT 1 FROM tools WHERE id = OLD.tool_id AND type = 'mcp'
        ) INTO old_tool_is_mcp;
        SELECT EXISTS (
            SELECT 1 FROM tools
            WHERE id IN (OLD.tool_id, NEW.tool_id) AND type = 'mcp'
        ) INTO guarded_tool_is_mcp;
    END IF;
    IF COALESCE(guarded_tool_is_mcp, false) THEN
        IF TG_OP = 'DELETE' THEN
            RETURN NULL;
        END IF;
        IF TG_OP = 'UPDATE' AND old_tool_is_mcp THEN
            NEW.agent_id := OLD.agent_id;
            NEW.tool_id := OLD.tool_id;
            NEW.source := OLD.source;
            NEW.installed_by_agent_id := OLD.installed_by_agent_id;
        END IF;
        NEW.enabled := false;
        NEW.config := '{}'::json;
    END IF;
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS astra_deploy_mcp_quarantine_tools_guard ON tools;
CREATE TRIGGER astra_deploy_mcp_quarantine_tools_guard
BEFORE INSERT OR UPDATE OR DELETE ON tools
FOR EACH ROW EXECUTE FUNCTION astra_deploy_guard_mcp_tools();
DROP TRIGGER IF EXISTS astra_deploy_mcp_quarantine_assignments_guard ON agent_tools;
CREATE TRIGGER astra_deploy_mcp_quarantine_assignments_guard
BEFORE INSERT OR UPDATE OR DELETE ON agent_tools
FOR EACH ROW EXECUTE FUNCTION astra_deploy_guard_mcp_assignments();
UPDATE agent_tools
SET enabled = false, config = '{}'::json
FROM tools
WHERE tools.id = agent_tools.tool_id
  AND tools.type = 'mcp';
UPDATE tools
SET enabled = false, mcp_server_url = NULL, config = '{}'::json
WHERE type = 'mcp';
SELECT snapshot_id
FROM astra_deploy_mcp_quarantine_state
WHERE singleton = true;
COMMIT;
SQL_MCP_QUARANTINE
    )" || return 1
    case "$effective_snapshot" in
        ''|*[!A-Za-z0-9._-]*)
            echo "invalid effective MCP quarantine snapshot identity" >&2
            return 1
            ;;
    esac
    MCP_QUARANTINE_SNAPSHOT_ID="$effective_snapshot"
}

secure_mcp_quarantine_snapshot() {
    local target_release="$1"
    local target_project="$2"

    if [ -z "$MCP_QUARANTINE_SNAPSHOT_ID" ]; then
        return 0
    fi
    if ! release_has_mcp_assignment_contract "$target_release"; then
        echo "refusing to sanitize MCP quarantine with an unsafe release" >&2
        return 1
    fi
    compose_project \
        "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        run --rm --no-deps -T --entrypoint python backend \
        -m app.scripts.secure_mcp_quarantine \
        "$MCP_QUARANTINE_SNAPSHOT_ID" < /dev/null
}

restore_mcp_quarantine_for_safe_release() {
    local target_release="$1"
    local target_project="$2"
    local postgres_user
    local postgres_db
    local snapshot_id

    if ! release_has_mcp_assignment_contract "$target_release"; then
        return 0
    fi
    postgres_user="$(read_release_env "$target_release" POSTGRES_USER astra)" || return 1
    postgres_db="$(read_release_env "$target_release" POSTGRES_DB astra)" || return 1
    snapshot_id="$(compose_project \
        "$COMPOSE_PROJECT" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        exec -T postgres psql -qAt -v ON_ERROR_STOP=1 \
        -U "$postgres_user" -d "$postgres_db" <<'SQL_MCP_PENDING_SNAPSHOT'
CREATE TABLE IF NOT EXISTS astra_deploy_mcp_quarantine_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    snapshot_id text NOT NULL UNIQUE,
    quarantined_at timestamptz NOT NULL DEFAULT now()
);
SELECT snapshot_id
FROM astra_deploy_mcp_quarantine_state
WHERE singleton = true;
SQL_MCP_PENDING_SNAPSHOT
    )" || return 1
    if [ -z "$snapshot_id" ]; then
        MCP_QUARANTINE_SNAPSHOT_ID=""
        return 0
    fi
    case "$snapshot_id" in
        *$'\n'*|*[!A-Za-z0-9._-]*)
            echo "invalid pending MCP quarantine snapshot identity" >&2
            return 1
            ;;
    esac

    echo "[remote] sanitizing and restoring MCP quarantine under safe release" >&2
    MCP_QUARANTINE_SNAPSHOT_ID="$snapshot_id"
    secure_mcp_quarantine_snapshot \
        "$target_release" "$target_project" || return 1

    compose_project \
        "$COMPOSE_PROJECT" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        exec -T postgres psql -q -v ON_ERROR_STOP=1 \
        -v snapshot_id="$snapshot_id" -U "$postgres_user" -d "$postgres_db" \
        <<'SQL_MCP_RESTORE'
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('astra-deploy-mcp-quarantine-v1'));
LOCK TABLE tools IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE agent_tools IN SHARE ROW EXCLUSIVE MODE;
SET LOCAL astra.mcp_quarantine_restore = 'on';
CREATE TEMP TABLE astra_expected_mcp_quarantine_snapshot
ON COMMIT DROP AS
SELECT snapshot_id
FROM astra_deploy_mcp_quarantine_state
WHERE singleton = true AND snapshot_id = :'snapshot_id';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM astra_expected_mcp_quarantine_snapshot) THEN
        RAISE EXCEPTION 'MCP quarantine snapshot changed before restore';
    END IF;
END
$$;
UPDATE tools AS tool
SET enabled = snapshot.enabled,
    mcp_server_url = snapshot.mcp_server_url,
    config = snapshot.config::json
FROM astra_deploy_mcp_quarantine_tools AS snapshot
WHERE snapshot.snapshot_id = :'snapshot_id'
  AND snapshot.tool_id = tool.id
  AND tool.type = 'mcp'
  AND tool.source IN ('builtin', 'admin', 'agent')
  AND (tool.source <> 'agent' OR tool.tenant_id IS NOT NULL);
UPDATE agent_tools AS assignment
SET enabled = snapshot.enabled,
    config = snapshot.config::json
FROM astra_deploy_mcp_quarantine_assignments AS snapshot,
     tools AS tool,
     agents AS agent
WHERE snapshot.snapshot_id = :'snapshot_id'
  AND snapshot.agent_tool_id = assignment.id
  AND tool.id = assignment.tool_id
  AND agent.id = assignment.agent_id
  AND tool.type = 'mcp'
  AND (
      (tool.source = 'agent' AND tool.tenant_id IS NOT NULL
       AND tool.tenant_id = agent.tenant_id)
      OR
      (tool.source IN ('builtin', 'admin')
       AND (tool.tenant_id IS NULL OR tool.tenant_id = agent.tenant_id))
  );
DELETE FROM astra_deploy_mcp_quarantine_assignments
WHERE snapshot_id = :'snapshot_id';
DELETE FROM astra_deploy_mcp_quarantine_tools
WHERE snapshot_id = :'snapshot_id';
DELETE FROM astra_deploy_mcp_quarantine_state
WHERE singleton = true AND snapshot_id = :'snapshot_id';
DROP TRIGGER IF EXISTS astra_deploy_mcp_quarantine_assignments_guard ON agent_tools;
DROP TRIGGER IF EXISTS astra_deploy_mcp_quarantine_tools_guard ON tools;
DROP FUNCTION IF EXISTS astra_deploy_guard_mcp_assignments();
DROP FUNCTION IF EXISTS astra_deploy_guard_mcp_tools();
COMMIT;
SQL_MCP_RESTORE
    MCP_QUARANTINE_SNAPSHOT_ID=""
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
    pending_release="$(python3 -c \
        'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' \
        "$pending_release" 2>/dev/null)" || return 1
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

    if [ ! -f "$journal" ] || [ -L "$journal" ] || [ ! -s "$journal" ]; then
        return 1
    fi
    release="$(python3 - "$journal" <<'PY_STRICT_SLOT_JOURNAL'
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags)
except OSError:
    raise SystemExit(1)
try:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise SystemExit(1)
    payload = os.read(descriptor, 4097)
finally:
    os.close(descriptor)

if (
    not payload
    or len(payload) > 4096
    or not payload.endswith(b"\n")
    or payload.count(b"\n") != 1
    or b"\r" in payload
    or b"\x00" in payload
):
    raise SystemExit(1)
try:
    value = payload[:-1].decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit(1)
if not value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
    raise SystemExit(1)
sys.stdout.write(value)
PY_STRICT_SLOT_JOURNAL
)" || return 1
    if [ -z "$release" ] || [ ! -d "$release" ]; then
        return 1
    fi
    release="$(python3 -c \
        'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' \
        "$release" 2>/dev/null)" || return 1
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
    local worker_identity
    local -a worker_identity_fields=()
    local worker_identity_field
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
            worker_identity="$(
                inspect_worker_runtime_identity "$worker_id" 2>/dev/null || true
            )"
            worker_identity_fields=()
            while IFS= read -r worker_identity_field; do
                worker_identity_fields+=("$worker_identity_field")
            done <<< "$worker_identity"
            if [ "${#worker_identity_fields[@]}" = "4" ]; then
                worker_release_id="${worker_identity_fields[0]}"
                worker_role="${worker_identity_fields[3]}"
            else
                worker_release_id=""
                worker_role=""
            fi
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
    # `docker compose ps -q` returns a full container ID. Keep the same form so
    # the exactly-one-worker comparison cannot reject a healthy matching worker.
    docker ps --no-trunc -q \
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
        maintenance_enabled|migration_started|schema_forward_only|candidate_services_ready|\
        candidate_alert_canary_verified|candidate_business_verified|candidate_ready|\
        nginx_reloaded|public_verified|\
        traffic_and_worker_committed|\
        rollback_started|rollback_incomplete|rollback_partial|\
        rollback_recovering_candidate|recovery_started|recovery_incomplete|\
        recovery_started_alert_proof|recovery_incomplete_alert_proof|\
        recovery_started_business_proof|recovery_incomplete_business_proof)
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
    local recorded_journal

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

    canonical_current="$(python3 -c \
        'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' \
        "$CURRENT" 2>/dev/null)" || return 1
    if [ ! -L "$CURRENT" ]; then
        echo "current release is not an atomic symlink" >&2
        return 1
    fi
    if ! recorded_release="$(release_for_slot "$RECORDED_SLOT")"; then
        if [ "$ACTIVE_STATE_SOURCE" != "legacy-pair" ]; then
            echo "committed active slot has no valid release journal" >&2
            return 1
        fi
        recorded_journal="$APP_ROOT/slot-${RECORDED_SLOT}-release"
        if [ -e "$recorded_journal" ] || [ -L "$recorded_journal" ]; then
            echo "legacy active slot has an invalid existing release journal" >&2
            return 1
        fi
        # Pre-v1.10.12 production stored only active-slot/active-release.
        # Accept that format for one migration only when current is a complete
        # managed release whose basename and live Nginx slot both agree.
        recorded_release="$canonical_current"
        case "$recorded_release" in
            "$APP_ROOT"/releases/*) ;;
            *)
                echo "legacy active release is outside the managed release directory" >&2
                return 1
                ;;
        esac
        if [ ! -s "$recorded_release/VERSION" ] || \
            [ ! -s "$recorded_release/COMMIT" ] || \
            [ ! -s "$recorded_release/.env" ] || \
            [ ! -s "$recorded_release/$COMPOSE_FILE" ]; then
            echo "legacy active release is incomplete" >&2
            return 1
        fi
        expected_port="$(port_for_slot "$RECORDED_SLOT")" || return 1
        if [ "$expected_port" != "$NGINX_ACTIVE_PORT" ]; then
            echo "legacy active slot does not match the live Nginx upstream" >&2
            return 1
        fi
    fi
    recorded_release_id="$(basename "$recorded_release")"
    if [ "$recorded_release_id" != "$ACTIVE_RELEASE_ID" ]; then
        echo "active release does not match the active slot journal" >&2
        return 1
    fi
    if [ "$canonical_current" != "$recorded_release" ]; then
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
    local fallback_project
    local fallback_release
    local pre_schema_rollback=0
    local schema_state
    local evidence_must_preexist=0
    local alert_evidence_must_preexist=0
    local recovery_started_phase=recovery_started
    local recovery_incomplete_phase=recovery_incomplete
    local alert_preflight_temp
    local alert_canary_temp

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

    case "${CUTOVER_PHASE:-}" in
        candidate_business_verified|nginx_reloaded|public_verified|\
        traffic_and_worker_committed|recovery_started_business_proof|\
        recovery_incomplete_business_proof)
            evidence_must_preexist=1
            ;;
    esac
    case "${CUTOVER_PHASE:-}" in
        candidate_alert_canary_verified|candidate_business_verified|\
        nginx_reloaded|public_verified|traffic_and_worker_committed|\
        recovery_started_alert_proof|recovery_incomplete_alert_proof|\
        recovery_started_business_proof|recovery_incomplete_business_proof)
            alert_evidence_must_preexist=1
            ;;
    esac
    if [ "$evidence_must_preexist" = "1" ]; then
        recovery_started_phase=recovery_started_business_proof
        recovery_incomplete_phase=recovery_incomplete_business_proof
    elif [ "$alert_evidence_must_preexist" = "1" ]; then
        recovery_started_phase=recovery_started_alert_proof
        recovery_incomplete_phase=recovery_incomplete_alert_proof
    fi

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

    if alert_canary_requires_v1 "$target_release"; then
        mkdir -p "$APP_ROOT/backups/$target_release_id"
        alert_preflight_temp="$(
            mktemp \
                "$APP_ROOT/backups/$target_release_id/.production-alert-preflight.recovery.XXXXXX"
        )" || return 1
        if ! run_alert_canary_preflight \
            "$target_release" "$target_project" "$target_release_id" \
            "$alert_preflight_temp" || \
            ! install_durable_file \
                "$alert_preflight_temp" \
                "$APP_ROOT/backups/$target_release_id/production-alert-preflight.json"; then
            rm -f "$alert_preflight_temp"
            echo "cannot recover cutover: production alert preflight failed" >&2
            return 1
        fi
    fi

    write_cutover_state "$recovery_started_phase" \
        "$target_slot" "$target_release_id" || return 1

    # Recovery first reconstructs the same explicit writer fence as a fresh
    # deployment. It never exposes either schema epoch while old writers can
    # still accept HTTP/WebSocket traffic.
    if ! sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        maintenance-on "$NGINX_SITE" "$NGINX_LOG_FORMAT" || \
        ! sudo nginx -t >/dev/null || ! audit_effective_nginx >/dev/null; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi
    if ! reload_nginx_with_worker_snapshot || \
        ! NGINX_WORKER_GRACE_SECONDS=60 retire_pre_reload_nginx_workers; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi
    if fallback_release="$(release_for_slot "$fallback_slot" 2>/dev/null)"; then
        fallback_project="$(project_for_slot "$fallback_slot")" || return 1
        compose_project \
            "$fallback_project" "$fallback_release/.env" \
            "$fallback_release/$COMPOSE_FILE" \
            stop --timeout 90 worker frontend backend || return 1
    fi
    if ! quarantine_mcp_for_unsafe_release \
        "$target_release" "recovery-${RELEASE_ID}-${target_release_id}"; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi
    case "${CUTOVER_PHASE:-}" in
        rollback_started|rollback_incomplete|rollback_partial)
            pre_schema_rollback=1
            ;;
    esac
    if [ "$pre_schema_rollback" = "1" ]; then
        schema_state="$(approval_schema_forward_state "$target_release" 2>/dev/null)" || \
            schema_state="unknown"
        if [ "$schema_state" != "0" ]; then
            echo "cannot recover pre-schema rollback after the forward-only schema appeared" >&2
            write_cutover_state "$recovery_incomplete_phase" \
                "$target_slot" "$target_release_id" || true
            return 1
        fi
    else
        if ! compose_project \
            "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
            run --rm --no-deps -T --entrypoint alembic backend upgrade head < /dev/null || \
            [ "$(approval_schema_forward_state "$target_release")" != "1" ]; then
            echo "cannot recover cutover: durable approval schema did not converge" >&2
            write_cutover_state "$recovery_incomplete_phase" \
                "$target_slot" "$target_release_id" || true
            return 1
        fi
        if ! reconcile_agentbay_for_cutover "$target_project" "$target_release"; then
            echo "cannot recover cutover: AgentBay provider cleanup remains unverified" >&2
            write_cutover_state "$recovery_incomplete_phase" \
                "$target_slot" "$target_release_id" || true
            return 1
        fi
    fi
    if ! compose_project \
        "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
        up -d --no-deps backend frontend; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi
    if ! wait_for_local_release \
        "$target_port" "$target_version" "$target_commit" \
        "recovery-local-$target_release_id" 30; then
        echo "cannot recover cutover: target slot identity is not healthy" >&2
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi

    if ! restore_mcp_quarantine_for_safe_release \
        "$target_release" "$target_project" || \
        ! activate_worker_release \
            "$target_project" "$target_release/.env" "$target_release/$COMPOSE_FILE" \
            "$target_release_id" 90; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi

    if alert_canary_requires_v1 "$target_release"; then
        if ! candidate_alert_canary_evidence_valid \
            "$target_release" "$target_project" "$target_release_id"; then
            if [ "$alert_evidence_must_preexist" = "1" ]; then
                echo "cannot recover cutover: verified alert canary evidence is missing or invalid" >&2
                write_cutover_state "$recovery_incomplete_phase" \
                    "$target_slot" "$target_release_id" || true
                return 1
            fi
            alert_canary_temp="$(
                mktemp \
                    "$APP_ROOT/backups/$target_release_id/.production-alert-canary.recovery.XXXXXX"
            )" || return 1
            if ! run_candidate_alert_canary \
                "$target_release" "$target_project" "$target_release_id" \
                "$alert_canary_temp" || \
                ! publish_alert_canary_evidence \
                    "$alert_canary_temp" "$target_release_id" || \
                ! candidate_alert_canary_evidence_valid \
                    "$target_release" "$target_project" "$target_release_id"; then
                rm -f "$alert_canary_temp"
                echo "cannot recover cutover: production alert canary failed" >&2
                write_cutover_state "$recovery_incomplete_phase" \
                    "$target_slot" "$target_release_id" || true
                return 1
            fi
        fi
        write_cutover_state candidate_alert_canary_verified \
            "$target_slot" "$target_release_id" || return 1
        recovery_started_phase=recovery_started_alert_proof
        recovery_incomplete_phase=recovery_incomplete_alert_proof
        alert_evidence_must_preexist=1
    fi

    # Before the verified phase, an interruption may legitimately have no
    # evidence yet. Re-run the authenticated money-surface smoke against the
    # isolated candidate while public Nginx remains fenced. At and after the
    # verified phase, missing/tampered evidence is a hard failure and is never
    # silently replaced.
    if ! candidate_business_evidence_valid \
        "$target_release_id" "$target_port"; then
        if [ "$evidence_must_preexist" = "1" ] || \
            ! regenerate_candidate_business_evidence \
                "$target_release" "$target_release_id" "$target_port" \
                "$target_project"; then
            echo "cannot recover cutover: candidate business verification is missing or invalid" >&2
            write_cutover_state "$recovery_incomplete_phase" \
                "$target_slot" "$target_release_id" || true
            return 1
        fi
    fi
    recovery_started_phase=recovery_started_business_proof
    recovery_incomplete_phase=recovery_incomplete_business_proof
    evidence_must_preexist=1
    alert_evidence_must_preexist=1
    write_cutover_state "$recovery_started_phase" \
        "$target_slot" "$target_release_id" || return 1

    # A hard interruption may leave the site file updated but the log-format file
    # stale. Re-running install is idempotent and completes that intended pair
    # before the on-disk target is made authoritative.
    if ! sudo python3 "$RELEASE/scripts/configure_production_nginx.py" \
        install "$NGINX_SITE" "$source_port" "$target_port" \
        "$NGINX_LOG_FORMAT"; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi
    if ! sudo nginx -t >/dev/null || ! audit_effective_nginx >/dev/null; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi
    write_atomic_symlink "$CURRENT" "$target_release" || return 1
    if ! reload_nginx_with_worker_snapshot; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi
    if ! wait_for_public_release \
        "$target_version" "$target_commit" \
        "recovery-public-$target_release_id" 30; then
        echo "cannot recover cutover: public identity did not converge to target slot" >&2
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
        return 1
    fi

    if ! retire_pre_reload_nginx_workers; then
        write_cutover_state "$recovery_incomplete_phase" \
            "$target_slot" "$target_release_id" || true
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

case "$PACKAGE_SHA256" in
    ''|*[!0-9a-f]*)
        echo "release package digest is invalid" >&2
        exit 1
        ;;
    *) ;;
esac
[ "${#PACKAGE_SHA256}" = "64" ] || {
    echo "release package digest must contain 64 hexadecimal characters" >&2
    exit 1
}
[ -f "$PACKAGE" ] && [ ! -L "$PACKAGE" ] || {
    echo "release package is missing or unsafe" >&2
    exit 1
}
ACTUAL_PACKAGE_SHA256="$(sha256sum "$PACKAGE" | awk '{print $1}')"
[ "$ACTUAL_PACKAGE_SHA256" = "$PACKAGE_SHA256" ] || {
    echo "release package digest mismatch" >&2
    exit 1
}

if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    case "$REMOTE_SMOKE_CREDENTIAL_DIGEST" in
        ''|*[!0-9a-f]*)
            echo "remote smoke credential digest is invalid" >&2
            exit 1
            ;;
        *) ;;
    esac
    [ "${#REMOTE_SMOKE_CREDENTIAL_DIGEST}" = "64" ] || {
        echo "remote smoke credential digest must contain 64 hexadecimal characters" >&2
        exit 1
    }
    [ -f "$SMOKE_ENV_FILE" ] && [ ! -L "$SMOKE_ENV_FILE" ] || {
        echo "remote smoke credential file is missing or unsafe" >&2
        exit 1
    }
    [ "$(wc -c < "$SMOKE_ENV_FILE")" -le 16384 ] || {
        echo "remote smoke credential file is too large" >&2
        exit 1
    }
    [ "$(sha256sum "$SMOKE_ENV_FILE" | awk '{print $1}')" = \
        "$REMOTE_SMOKE_CREDENTIAL_DIGEST" ] || {
        echo "remote smoke credential digest mismatch" >&2
        exit 1
    }
    chmod 0600 "$SMOKE_ENV_FILE"
else
    [ -f "$REMOTE_SMOKE_BREAK_GLASS_FILE" ] && \
        [ ! -L "$REMOTE_SMOKE_BREAK_GLASS_FILE" ] || {
        echo "remote break-glass approval artifact is missing or unsafe" >&2
        exit 1
    }
    ACTUAL_BREAK_GLASS_DIGEST="$(sha256sum "$REMOTE_SMOKE_BREAK_GLASS_FILE" | awk '{print $1}')"
    [ "$ACTUAL_BREAK_GLASS_DIGEST" = "$REMOTE_SMOKE_BREAK_GLASS_DIGEST" ] || {
        echo "remote break-glass approval artifact digest mismatch" >&2
        exit 1
    }
    case "$REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH" in
        ''|*[!0-9a-f]*)
            echo "remote break-glass nonce hash is invalid" >&2
            exit 1
            ;;
        *) ;;
    esac
    [ "${#REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH}" = "64" ] || {
        echo "remote break-glass nonce hash must contain 64 hexadecimal characters" >&2
        exit 1
    }
fi

prepare_browser_smoke_runtime_root || {
    echo "browser smoke runtime directory is unsafe" >&2
    exit 1
}

mkdir -p "$RELEASE" "$BACKUP"
tar -xzf "$PACKAGE" -C "$RELEASE"
write_atomic_line "$RELEASE/PACKAGE_SHA256" "$PACKAGE_SHA256"

# Build the digest-pinned browser gate before any recovery, service, Nginx,
# database, or traffic mutation. A missing registry artifact or incompatible
# Docker runtime therefore fails while the active release is untouched.
if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    if ! ensure_browser_smoke_image "$RELEASE" "$RELEASE_ID"; then
        echo "isolated browser smoke image build or launch self-test failed" >&2
        exit 1
    fi
fi

# This must precede every cutover recovery, active-state rewrite, pending-drain
# completion, service action, Nginx change, database backup, or migration.
# Package extraction and state parsing above do not expose or mutate runtime
# traffic. A missing or drifted host contract stops before recovery can act.
EARLY_ENV="$CURRENT_TARGET/.env"
[ -f "$EARLY_ENV" ] && [ ! -L "$EARLY_ENV" ] || {
    echo "current release environment is missing before host egress verification" >&2
    exit 1
}
DOCKER_NETWORK_NAME="$(
    grep -E '^DOCKER_NETWORK=' "$EARLY_ENV" | tail -1 | \
        cut -d= -f2- | sed -E 's/^"//; s/"$//'
)"
if [ -z "$DOCKER_NETWORK_NAME" ]; then
    DOCKER_NETWORK_NAME="astra_network"
fi
echo "[remote] verifying host-level MCP egress contract"
sudo bash "$RELEASE/scripts/manage-production-mcp-egress-guard.sh" verify \
    "$DOCKER_NETWORK_NAME" \
    "$RELEASE/deploy/security-contracts/mcp-egress-v1"

if [ "$RUN_REMOTE_SMOKE" = "0" ]; then
    # Publish one complete, fsynced approval record after the pre-mutation host
    # gate but before recovery, service, Nginx, backup, or migration actions.
    # A crash before atomic publication does not consume the nonce; a crash
    # after publication leaves the full approval artifact and release binding.
    BREAK_GLASS_NONCE_ROOT="$APP_ROOT/break-glass-nonces"
    sudo python3 "$RELEASE/scripts/consume_break_glass_approval.py" \
        --ledger-dir "$BREAK_GLASS_NONCE_ROOT" \
        --artifact "$REMOTE_SMOKE_BREAK_GLASS_FILE" \
        --artifact-sha256 "$REMOTE_SMOKE_BREAK_GLASS_DIGEST" \
        --nonce-sha256 "$REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH" \
        --release-id "$RELEASE_ID" \
        --release-version "$VERSION" \
        --release-commit "$COMMIT"

    install -m 0600 "$REMOTE_SMOKE_BREAK_GLASS_FILE" \
        "$BACKUP/remote-smoke-break-glass.approval"
    printf '%s\n' "$REMOTE_SMOKE_BREAK_GLASS_DIGEST" > \
        "$BACKUP/remote-smoke-break-glass.sha256"
    printf '%s\n' "$REMOTE_SMOKE_BREAK_GLASS_NONCE_HASH" > \
        "$BACKUP/remote-smoke-break-glass.nonce-sha256"
    chmod 0600 \
        "$BACKUP/remote-smoke-break-glass.sha256" \
        "$BACKUP/remote-smoke-break-glass.nonce-sha256"
    rm -f "$REMOTE_SMOKE_BREAK_GLASS_FILE"
fi

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
if [ "$RECOVERY_REQUIRED" = "1" ] && [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    RECOVERY_BROWSER_RELEASE="$(release_for_slot "$RECOVERY_TARGET_SLOT")" || {
        echo "cannot resolve the browser-gated recovery release" >&2
        exit 1
    }
    if browser_smoke_requires_v2 "$RECOVERY_BROWSER_RELEASE" && \
        ! ensure_browser_smoke_image \
            "$RECOVERY_BROWSER_RELEASE" "$RECOVERY_TARGET_RELEASE_ID"; then
        echo "recovery release browser smoke image failed its launch preflight" >&2
        exit 1
    fi
fi
early_recovery_signal() {
    local signal_name="$1"
    local signal_status="$2"
    trap - HUP INT TERM
    set +e
    cleanup_browser_smoke_runtime
    write_cutover_state \
        recovery_incomplete "$RECOVERY_TARGET_SLOT" "$RECOVERY_TARGET_RELEASE_ID" || true
    echo "indeterminate cutover recovery interrupted by $signal_name" >&2
    exit "$signal_status"
}
if [ "$RECOVERY_REQUIRED" = "1" ]; then
    echo "[remote] indeterminate cutover: recorded=$RECORDED_SLOT disk=$DISK_SLOT target=$RECOVERY_TARGET_SLOT; preserving both slots"
    trap 'early_recovery_signal HUP 129' HUP
    trap 'early_recovery_signal INT 130' INT
    trap 'early_recovery_signal TERM 143' TERM
    if ! recover_indeterminate_cutover \
        "$RECORDED_SLOT" "$RECOVERY_TARGET_SLOT" "$RECOVERY_TARGET_PORT" \
        "$RECOVERY_TARGET_RELEASE_ID"; then
        echo "indeterminate cutover recovery failed; both slots were preserved" >&2
        exit 1
    fi
    trap - HUP INT TERM
    DISK_SLOT="$RECOVERY_TARGET_SLOT"
    NGINX_ACTIVE_PORT="$RECOVERY_TARGET_PORT"
    CURRENT_TARGET="$(python3 -c \
        'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' \
        "$CURRENT" 2>/dev/null)"
    if ! load_active_state || ! parse_cutover_state || ! validate_stable_state; then
        echo "cutover recovery did not produce a consistent terminal state" >&2
        exit 1
    fi
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
    if PREVIOUS="$(release_for_slot "$ACTIVE_SLOT")"; then
        :
    elif [ -e "$ACTIVE_SLOT_RELEASE_FILE" ] || [ -L "$ACTIVE_SLOT_RELEASE_FILE" ]; then
        echo "cannot reconcile live slot $ACTIVE_SLOT from an invalid release journal" >&2
        exit 1
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
PREVIOUS="$(python3 -c \
    'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' \
    "$PREVIOUS" 2>/dev/null)"
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
# The slot journal must be durable before canonical active-state references it.
# Rewriting an existing atomic state is also intentional: it heals either
# compatibility mirror after an interrupted prior commit.
commit_active_state "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || {
    echo "cannot persist canonical active release state" >&2
    exit 1
}
ACTIVE_STATE_PRESENT=1
ACTIVE_STATE_SOURCE="atomic"
ACTIVE_RELEASE_ID="$PREVIOUS_RELEASE_ID"
RECORDED_SLOT="$ACTIVE_SLOT"
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
import uuid

path = Path(sys.argv[1])
updates = {
    "ASTRA_RELEASE_VERSION": sys.argv[2],
    "ASTRA_RELEASE_COMMIT": sys.argv[3],
    "ASTRA_RELEASE_ID": sys.argv[4],
    "ASTRA_RELEASE": sys.argv[4],
    "ASTRA_ALERT_WORKER_ACTOR_ID": str(uuid.uuid4()),
    "COMMIT": sys.argv[3],
    "ALLOW_MIGRATION_FAILURE": "false",
    "HEARTBEAT_ENABLED": "false",
    "TRIGGER_DAEMON_ENABLED": "true",
    "USER_AUTOMATION_EXECUTION_ENABLED": "true",
    "USER_SCHEDULE_EXECUTION_ENABLED": "false",
    "USER_TASK_EXECUTION_ENABLED": "false",
    "APPROVAL_EXECUTION_ENABLED": "true",
    "SUPERVISION_EXECUTION_ENABLED": "false",
    "OKR_AUTOMATION_ENABLED": "false",
    "FEISHU_WEBHOOK_ENABLED": "false",
    "TEAMS_WEBHOOK_ENABLED": "false",
    "PRODUCTION_ISSUE_MONITOR_ENABLED": "true",
    "TRIGGER_MAX_CONCURRENCY": "8",
    "TRIGGER_CLAIM_BATCH_SIZE": "16",
    "COMPANY_ASSIGNMENT_RUNNER_ENABLED": "false",
    # A normal version deployment is never a Code capability activation.
    # Activation requires a separately authorized, provider-specific workflow.
    "CODE_EXECUTION_ENABLED": "false",
    "CODE_EXECUTION_ALLOWED_TENANT_IDS": "",
    "CODE_EXECUTION_ALLOWED_TOOL_NAMES": "",
    "CODE_EXECUTION_ALLOWED_SANDBOX_TYPES": "",
    "CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS": "",
    "CODE_EXECUTION_REQUIRE_APPROVAL": "true",
    "SANDBOX_ALLOW_NETWORK": "false",
    "SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING": "false",
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

echo "[remote] validating persisted MCP endpoint policy"
mcp_endpoint_preflight "$PREVIOUS"

echo "[remote] validating deterministic model routes and MiniMax credential capabilities"
model_route_credential_preflight "$PREVIOUS"

printf '%s\n' "$VERSION" > "$RELEASE/VERSION"
printf '%s\n' "$COMMIT" > "$RELEASE/COMMIT"
printf '%s\n' "$RELEASE_BASE_COMMIT" > "$RELEASE/BASE_COMMIT"
printf '%s\n' "$PREVIOUS" > "$RELEASE/PREVIOUS_RELEASE"

NGINX_BACKUP="$BACKUP/astra-poc.nginx.before.conf"
NGINX_LOG_FORMAT_BACKUP="$BACKUP/00-astra-log-redaction.before.conf"
NGINX_CONFIG_TOUCHED=0
CANDIDATE_READY_FOR_FALLBACK=0
sudo cp "$NGINX_SITE" "$NGINX_BACKUP"
if sudo test -f "$NGINX_LOG_FORMAT"; then
    sudo cp "$NGINX_LOG_FORMAT" "$NGINX_LOG_FORMAT_BACKUP"
fi

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
    restore_mcp_quarantine_for_safe_release \
        "$RELEASE" "$CANDIDATE_PROJECT" || return 1
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
    local schema_state
    echo "[remote] rollback to $PREVIOUS" >&2
    trap - ERR HUP INT TERM
    set +e
    cleanup_browser_smoke_runtime
    rm -f "$SMOKE_ENV_FILE"
    # Once the durable approval consistency constraint exists, the old API and
    # worker are no longer schema-compatible. A failed/unknown probe is also
    # treated as forward-only: availability must never win over duplicate
    # external side effects or silent MCP configuration loss.
    schema_state="$(approval_schema_forward_state "$PREVIOUS" 2>/dev/null)" || schema_state="unknown"
    if [ "$schema_state" != "0" ]; then
        SCHEMA_FORWARD_ONLY=1
        write_cutover_state \
            schema_forward_only "$CANDIDATE_SLOT" "$RELEASE_ID" || true
        preserve_forward_only_maintenance || true
        return 1
    fi
    if ! write_cutover_state \
        rollback_started "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID"; then
        echo "[remote] rollback refused: cannot persist rollback intent" >&2
        return 1
    fi

    if [ "$ROLLBACK_REQUIRES_MCP_QUARANTINE" = "1" ]; then
        if ! quarantine_mcp_for_unsafe_release \
            "$PREVIOUS" "rollback-${RELEASE_ID}"; then
            echo "[remote] rollback refused: MCP quarantine failed" >&2
            write_cutover_state \
                rollback_incomplete "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID" || true
            return 1
        fi
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

# Fail before maintenance if the candidate cannot identify at least one real
# alert sink or the configured in-app owner does not exist. This preflight is
# read-only; the delivery canary runs only after the candidate worker is live.
echo "[remote] validating production alert sink configuration"
ALERT_PREFLIGHT_TEMP="$(
    mktemp "$BACKUP/.production-alert-preflight.XXXXXX"
)" || abort_release "cannot allocate production alert preflight evidence"
if ! run_alert_canary_preflight \
    "$RELEASE" "$CANDIDATE_PROJECT" "$RELEASE_ID" \
    "$ALERT_PREFLIGHT_TEMP"; then
    rm -f "$ALERT_PREFLIGHT_TEMP"
    abort_release "production alert sink configuration preflight failed"
fi
install_durable_file \
    "$ALERT_PREFLIGHT_TEMP" "$BACKUP/production-alert-preflight.json" || \
    abort_release "production alert preflight evidence could not be published"

# The candidate journal is durable before the first traffic mutation, so an
# interrupted forward-only migration can only recover toward this exact code.
write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"

echo "[remote] checking AgentBay session ledger before maintenance"
AGENTBAY_UNRESOLVED_BEFORE_MAINTENANCE="$(agentbay_unresolved_count "$PREVIOUS")" || \
    abort_release "cannot verify the AgentBay ledger before maintenance"
echo "[remote] AgentBay unresolved before maintenance: $AGENTBAY_UNRESOLVED_BEFORE_MAINTENANCE"
if [ "$AGENTBAY_UNRESOLVED_BEFORE_MAINTENANCE" != "0" ]; then
    abort_release "AgentBay has unresolved provider sessions; reconcile them out of band before starting maintenance"
fi

echo "[remote] enabling explicit Web/API/WebSocket maintenance fence"
if ! enable_web_maintenance; then
    abort_release "cannot establish and verify the production maintenance fence"
fi
write_cutover_state maintenance_enabled "$CANDIDATE_SLOT" "$RELEASE_ID"

echo "[remote] stopping every old application writer before quarantine/migration"
compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" \
    stop --timeout 90 worker frontend backend

echo "[remote] auditing all media Credits bindings before migration"
if ! compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
    run --rm --no-deps -T --entrypoint python backend \
    -m app.scripts.inventory_legacy_media_reservations \
    --fail-on-blocking \
    > "$BACKUP/media-credit-inventory.pre-migration.json"; then
    chmod 0600 "$BACKUP/media-credit-inventory.pre-migration.json" || true
    abort_release "media Credits inventory found a blocking pre-migration inconsistency"
fi
chmod 0600 "$BACKUP/media-credit-inventory.pre-migration.json"
test -s "$BACKUP/media-credit-inventory.pre-migration.json"

if ! quarantine_mcp_for_unsafe_release \
    "$PREVIOUS" "migration-${RELEASE_ID}"; then
    abort_release "cannot install the MCP rollback guard before migrations"
fi
ROLLBACK_REQUIRES_MCP_QUARANTINE=1
if ! secure_mcp_quarantine_snapshot "$RELEASE" "$CANDIDATE_PROJECT"; then
    abort_release "cannot encrypt and sanitize the MCP rollback snapshot"
fi
echo "[remote] applying migrations before candidate startup"
write_cutover_state migration_started "$CANDIDATE_SLOT" "$RELEASE_ID"
compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
    run --rm --no-deps -T --entrypoint alembic backend upgrade head < /dev/null
echo "[remote] verifying all media Credits bindings after migration"
if ! compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
    run --rm --no-deps -T --entrypoint python backend \
    -m app.scripts.inventory_legacy_media_reservations \
    --fail-on-blocking --require-no-legacy \
    > "$BACKUP/media-credit-inventory.post-migration.json"; then
    chmod 0600 "$BACKUP/media-credit-inventory.post-migration.json" || true
    abort_release "media Credits inventory is not canonical after migration"
fi
chmod 0600 "$BACKUP/media-credit-inventory.post-migration.json"
test -s "$BACKUP/media-credit-inventory.post-migration.json"
if [ "$(approval_schema_forward_state "$RELEASE")" != "1" ]; then
    abort_release "durable approval schema constraint is missing after migration"
fi
if ! model_route_credential_preflight "$RELEASE"; then
    abort_release "model-route or MiniMax credential contract failed after migration"
fi
if ! m3_route_post_migration_preflight "$RELEASE"; then
    abort_release "MiniMax-M3 exact 3x3 top-route contract failed after migration"
fi
if ! compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
    run --rm --no-deps -T --entrypoint python backend \
    -m app.scripts.verify_channel_secrets < /dev/null; then
    abort_release "ChannelConfig secret encryption or authentication failed after migration"
fi
if ! reconcile_agentbay_for_cutover "$CANDIDATE_PROJECT" "$RELEASE"; then
    abort_release "AgentBay provider cleanup remains unverified after migration"
fi
SCHEMA_FORWARD_ONLY=1
write_cutover_state schema_forward_only "$CANDIDATE_SLOT" "$RELEASE_ID"

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

# Restore sanitized MCP assignments while all public API writers remain fenced,
# then bring up the one candidate worker/connector on the new schema.
restore_mcp_quarantine_for_safe_release "$RELEASE" "$CANDIDATE_PROJECT"
if ! activate_worker_release \
    "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" \
    "$RELEASE_ID" 90; then
    abort_release "candidate worker did not become healthy on release $RELEASE_ID"
fi
write_cutover_state candidate_services_ready "$CANDIDATE_SLOT" "$RELEASE_ID"

# Prove that the same durable outbox used by real production incidents can
# reach every configured sink. The release-bound canary is resolved only after
# all delivery rows are committed as delivered under the issue lock.
echo "[remote] verifying production alert delivery pipeline"
ALERT_CANARY_TEMP="$(
    mktemp "$BACKUP/.production-alert-canary.XXXXXX"
)" || abort_release "cannot allocate production alert canary evidence"
if ! run_candidate_alert_canary \
    "$RELEASE" "$CANDIDATE_PROJECT" "$RELEASE_ID" \
    "$ALERT_CANARY_TEMP"; then
    rm -f "$ALERT_CANARY_TEMP"
    abort_release "production alert delivery canary failed"
fi
publish_alert_canary_evidence "$ALERT_CANARY_TEMP" "$RELEASE_ID" || \
    abort_release "production alert canary evidence could not be published"
if ! candidate_alert_canary_evidence_valid \
    "$RELEASE" "$CANDIDATE_PROJECT" "$RELEASE_ID"; then
    abort_release "production alert canary evidence is invalid"
fi
write_cutover_state candidate_alert_canary_verified \
    "$CANDIDATE_SLOT" "$RELEASE_ID"

# Exercise the authenticated money surface against the isolated candidate
# before changing the public upstream. A failure here keeps the maintenance
# fence in place and never exposes an unverified API/worker pair to customers.
if [ "$RUN_REMOTE_SMOKE" = "1" ]; then
    if [ "$BROWSER_SMOKE_READY" != "1" ]; then
        abort_release "browser smoke image did not pass its pre-mutation launch self-test"
    fi
    CANDIDATE_SMOKE_TEMP="$(
        mktemp "$BACKUP/.subscription-smoke.candidate.XXXXXX"
    )" || abort_release "cannot allocate candidate smoke evidence"
    if ! run_candidate_business_smoke \
        "$RELEASE" "$CANDIDATE_PROJECT" "$RELEASE_ID" \
        "$VERSION" "$COMMIT" "$CANDIDATE_PORT" "$CANDIDATE_SMOKE_TEMP"; then
        rm -f "$CANDIDATE_SMOKE_TEMP"
        abort_release "authenticated candidate API/browser smoke failed"
    fi
    if ! install_durable_file \
        "$CANDIDATE_SMOKE_TEMP" "$BACKUP/subscription-smoke.candidate.json"; then
        abort_release "candidate smoke evidence could not be durably published"
    fi
    rm -f "$SMOKE_ENV_FILE"
    CANDIDATE_SMOKE_SHA256="$(
        sha256sum "$BACKUP/subscription-smoke.candidate.json" | awk '{print $1}'
    )"
    write_atomic_line "$BACKUP/candidate-business-verification" \
        "smoke-v2:${CANDIDATE_SMOKE_SHA256}"
    echo "[remote] candidate API/browser smoke passed"
else
    write_atomic_line "$BACKUP/candidate-business-verification" \
        "break-glass:${REMOTE_SMOKE_BREAK_GLASS_DIGEST}"
fi
if ! candidate_business_evidence_valid "$RELEASE_ID" "$CANDIDATE_PORT"; then
    abort_release "candidate business verification evidence is invalid"
fi
write_cutover_state candidate_business_verified "$CANDIDATE_SLOT" "$RELEASE_ID"

echo "[remote] switching the verified maintenance fence to candidate traffic"
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

if [ "$ROTATE_JWT" = "1" ]; then
    touch "$JWT_ROTATION_MARKER"
fi
write_cutover_state complete "$CANDIDATE_SLOT" "$RELEASE_ID"

cleanup_browser_smoke_runtime
trap - ERR HUP INT TERM
sudo find /var/log/nginx -maxdepth 1 -type f -name 'access.log.*' -exec chmod 600 {} + >/dev/null 2>&1 || true
rm -f "$PACKAGE" "$SMOKE_ENV_FILE"
echo "[remote] release $RELEASE_ID deployed on slot $CANDIDATE_SLOT"
REMOTE_SCRIPT

echo "[done] deployed $RELEASE_ID to $PUBLIC_URL"
