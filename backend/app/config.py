"""Application configuration."""

from functools import lru_cache
import os
from pathlib import Path
import socket
import uuid

from pydantic_settings import BaseSettings

from app.services.sandbox.config import SandboxConfig, SandboxType


def _running_in_container() -> bool:
    """Best-effort container runtime detection."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True

    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False

    try:
        content = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    return any(token in content for token in ("docker", "containerd", "kubepods", "podman"))


def _default_agent_data_dir() -> str:
    """Use Docker path in containers, user-writable path on local hosts."""
    if _running_in_container():
        return "/data/agents"
    return str(Path.home() / ".clawith" / "data" / "agents")


def _default_instance_id() -> str:
    """Generate a stable-enough per-process instance identifier."""
    host = socket.gethostname() or "unknown"
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    return f"{host}-{pid}-{suffix}"


def _default_agent_template_dir() -> str:
    """Locate the agent template directory for both Docker and source deployments.

    In a Docker container the backend source is copied to /app, so the template
    lives at /app/agent_template.  In a source deployment it sits next to the
    backend/ package root, i.e. <repo>/backend/agent_template.
    """
    if _running_in_container():
        return "/app/agent_template"
    # Source layout: backend/app/config.py -> ../.. = backend/ -> agent_template
    source_path = Path(__file__).resolve().parent.parent / "agent_template"
    return str(source_path)


def _default_allow_unsafe_bwrap_fallback() -> bool:
    """Allow local source runs to work without bubblewrap by default."""
    return not _running_in_container()


def _read_version() -> str:
    """Read version from local VERSION file, fallback to root."""
    for candidate in [Path(__file__).resolve().parent.parent / "VERSION",
                      Path(__file__).resolve().parent.parent.parent / "VERSION",
                      Path("/app/VERSION"), Path("/VERSION")]:
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App
    APP_NAME: str = "Astra"
    APP_VERSION: str = _read_version()
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://clawith:clawith@localhost:5432/clawith"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    INSTANCE_ID: str = _default_instance_id()

    # JWT
    JWT_SECRET_KEY: str = "change-me-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 60
    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 60  # 1 hour
    EMAIL_VERIFICATION_REQUIRED: bool = False  # Require email verification for login
    # Password signup must never turn a missing production mail transport into
    # proof of mailbox ownership.  This escape hatch is intentionally limited
    # to development/test by ``unverified_local_signup_allowed`` below.
    ALLOW_UNVERIFIED_LOCAL_SIGNUP: bool = False
    SSO_SESSION_CREATE_IP_LIMIT_PER_MINUTE: int = 30
    SSO_SESSION_CREATE_TENANT_LIMIT_PER_MINUTE: int = 120
    SSO_SESSION_CREATE_GLOBAL_LIMIT_PER_MINUTE: int = 300
    SSO_SESSION_CLEANUP_INTERVAL_SECONDS: int = 60
    SSO_SESSION_RETENTION_MINUTES: int = 60
    # Anonymous auth work is protected in the application so every deployment
    # shape receives the same client + target + global quotas. Global bcrypt
    # limits use short windows to bound bursts into the four-worker pool.
    # Client quotas are deliberately NAT-tolerant: enterprise users commonly
    # share one public address. Per-identity and global work buckets remain the
    # primary credential/provider abuse controls.
    # Login lookup has a separate, wider pre-query budget because unresolved
    # identifiers can require up to three indexed namespace probes before an
    # Identity is known.  The global value is expressed in worst-case query
    # units; each admitted request reserves three units.
    AUTH_LOGIN_LOOKUP_CLIENT_LIMIT_PER_MINUTE: int = 120
    AUTH_LOGIN_LOOKUP_IDENTIFIER_LIMIT_PER_MINUTE: int = 20
    AUTH_LOGIN_LOOKUP_GLOBAL_QUERY_UNITS_PER_MINUTE: int = 1800
    AUTH_LOGIN_CLIENT_LIMIT_PER_MINUTE: int = 60
    AUTH_LOGIN_IDENTITY_LIMIT_PER_MINUTE: int = 8
    AUTH_PASSWORD_REGISTER_CLIENT_LIMIT_PER_MINUTE: int = 30
    AUTH_PASSWORD_REGISTER_IDENTITY_LIMIT_PER_MINUTE: int = 3
    AUTH_PASSWORD_CHANGE_CLIENT_LIMIT_PER_MINUTE: int = 30
    AUTH_PASSWORD_CHANGE_IDENTITY_LIMIT_PER_MINUTE: int = 8
    AUTH_PASSWORD_REAUTH_CLIENT_LIMIT_PER_MINUTE: int = 30
    AUTH_PASSWORD_REAUTH_IDENTITY_LIMIT_PER_MINUTE: int = 8
    # One unit is one worst-case bcrypt operation. Registration and password
    # change reserve two units per request; login reserves one. At the default
    # bcrypt cost this keeps admitted work below half of the measured pool rate.
    AUTH_BCRYPT_GLOBAL_WORK_UNITS_PER_10_SECONDS: int = 80
    AUTH_EMAIL_ACTION_CLIENT_LIMIT_PER_15_MINUTES: int = 30
    AUTH_EMAIL_ACTION_IDENTITY_LIMIT_PER_15_MINUTES: int = 3
    AUTH_EMAIL_ACTION_GLOBAL_LIMIT_PER_MINUTE: int = 120
    AUTH_DISCOVERY_CLIENT_LIMIT_PER_MINUTE: int = 120
    AUTH_DISCOVERY_IDENTITY_LIMIT_PER_MINUTE: int = 10
    AUTH_DISCOVERY_GLOBAL_LIMIT_PER_MINUTE: int = 300
    AUTH_OAUTH_START_CLIENT_LIMIT_PER_MINUTE: int = 60
    AUTH_OAUTH_START_PROVIDER_LIMIT_PER_MINUTE: int = 120
    AUTH_OAUTH_START_GLOBAL_LIMIT_PER_MINUTE: int = 300
    AUTH_OAUTH_EXCHANGE_CLIENT_LIMIT_PER_MINUTE: int = 60
    AUTH_OAUTH_EXCHANGE_PROVIDER_LIMIT_PER_MINUTE: int = 60
    AUTH_OAUTH_EXCHANGE_GLOBAL_LIMIT_PER_MINUTE: int = 120

    # File Storage
    STORAGE_BACKEND: str = "local"
    AGENT_DATA_DIR: str = _default_agent_data_dir()
    AGENT_TEMPLATE_DIR: str = _default_agent_template_dir()
    STORAGE_LOCAL_ROOT: str = _default_agent_data_dir()
    STORAGE_LOCAL_FALLBACK_ENABLED: bool = True
    S3_BUCKET: str = ""
    S3_REGION: str = ""
    S3_ENDPOINT_URL: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_PREFIX: str = "agents"
    S3_PRESIGN_TTL_SECONDS: int = 3600
    S3_MAX_POOL_CONNECTIONS: int = 50
    S3_WRITE_WORKERS: int = 32

    # Process role
    PROCESS_ROLE: str = "all"

    # Emergency kill switch for proactive Agent heartbeat execution. Explicit
    # cron/interval/webhook/on_message triggers remain independent.
    HEARTBEAT_ENABLED: bool = False

    # Trigger runtime controls. The daemon stays available for explicit user
    # workflows, while the costly platform-seeded OKR automation is opt-in.
    # Claim and concurrency limits are process-local safety bounds; durable
    # executions remain queued until a worker slot is available.
    TRIGGER_DAEMON_ENABLED: bool = True
    # Durable, user-created triggers are distinct from platform heartbeat/OKR
    # automation. Keep this operator kill switch independent so disabling the
    # CEO loop never silently disables explicit customer work.
    USER_AUTOMATION_EXECUTION_ENABLED: bool = True
    # Legacy schedule/task executors still use fire-and-forget delivery and do
    # not yet have a durable lease/recovery contract. Keep them fail-closed in
    # every environment until they are moved onto the durable trigger queue.
    USER_SCHEDULE_EXECUTION_ENABLED: bool = False
    USER_TASK_EXECUTION_ENABLED: bool = False
    APPROVAL_EXECUTION_ENABLED: bool = True
    # The legacy supervision reminder has no durable exactly-once delivery
    # claim yet and remains separately quarantined.
    SUPERVISION_EXECUTION_ENABLED: bool = False
    OKR_AUTOMATION_ENABLED: bool = False
    TRIGGER_MAX_CONCURRENCY: int = 8
    TRIGGER_CLAIM_BATCH_SIZE: int = 16

    # Docker (for Agent containers)
    DOCKER_NETWORK: str = "clawith_network"
    OPENCLAW_IMAGE: str = "openclaw:local"
    OPENCLAW_GATEWAY_PORT: int = 18789

    # Feishu OAuth
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    PUBLIC_BASE_URL: str = ""
    HTTP_PROXY: str = ""
    # Public webhook transports stay fail-closed until their provider-native
    # authentication contract is implemented and regression-tested. Feishu
    # websocket and other authenticated connector modes remain available.
    FEISHU_WEBHOOK_ENABLED: bool = False
    TEAMS_WEBHOOK_ENABLED: bool = False

    # Douyin official OpenAPI. In hosted SaaS mode the platform owns this app,
    # while each company only OAuth-connects its own Douyin account.
    DOUYIN_CLIENT_KEY: str = ""
    DOUYIN_CLIENT_SECRET: str = ""
    DOUYIN_REDIRECT_URI: str = ""
    DOUYIN_SCOPES: str = "user_info,h5.share,aweme.share,aweme.forward,open.get.ticket,video.comment,data.external.user,data.external.item"
    DOUYIN_API_BASE_URL: str = "https://open.douyin.com"
    DOUYIN_AUTHORIZE_URL: str = "https://open.douyin.com/platform/oauth/connect"
    DOUYIN_REQUEST_TIMEOUT_SECONDS: int = 15
    DOUYIN_DIRECT_PUBLISH_ENABLED: bool = False

    # Billing checkout provider. "manual" keeps local/admin-only orders; "stripe"
    # creates real Checkout Sessions and requires signed webhooks.
    BILLING_PROVIDER: str = "manual"
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_API_BASE_URL: str = "https://api.stripe.com"
    STRIPE_SUCCESS_URL: str = ""
    STRIPE_CANCEL_URL: str = ""
    BILLING_SUCCESS_URL: str = ""
    BILLING_CANCEL_URL: str = ""
    BILLING_RECONCILIATION_INTERVAL_SECONDS: int = 60 * 60 * 24
    BILLING_RESERVATION_EXPIRY_SWEEP_SECONDS: int = 60 * 10
    MEDIA_GENERATION_POLL_INTERVAL_SECONDS: int = 15
    MEDIA_GENERATION_BATCH_SIZE: int = 20
    MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS: int = 60 * 10
    MEDIA_GENERATION_MAX_AGE_SECONDS: int = 60 * 60 * 48
    MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS: int = 12
    # Keep production recovery serial until measured memory/CPU/disk pressure
    # proves that concurrent provider downloads and ffmpeg work are safe.
    MEDIA_GENERATION_RECONCILIATION_CONCURRENCY: int = 1
    MEDIA_GENERATION_TASK_LEASE_SECONDS: int = 30 * 60
    MEDIA_GENERATION_BRAND_RECOVERY_RETENTION_DAYS: int = 30
    PRODUCTION_ISSUE_MONITOR_ENABLED: bool = True
    PRODUCTION_ISSUE_MONITOR_INTERVAL_SECONDS: int = 30
    PRODUCTION_ISSUE_ALERT_THRESHOLD: int = 1
    PRODUCTION_ISSUE_RETENTION_DAYS: int = 30
    PRODUCTION_ISSUE_ALERT_WEBHOOK_URL: str = ""
    MINIMAX_QUOTA_MONITOR_INTERVAL_SECONDS: int = 300

    # Code execution is a separate high-risk product capability. Production
    # requires an explicit platform switch, an explicit tenant allowlist, and
    # an isolated external sandbox backend; it is never inferred from a model
    # plan or from an AgentTool row alone.
    CODE_EXECUTION_ENABLED: bool = False
    CODE_EXECUTION_ALLOWED_TENANT_IDS: str = ""
    CODE_EXECUTION_ALLOWED_TOOL_NAMES: str = ""
    CODE_EXECUTION_ALLOWED_SANDBOX_TYPES: str = ""
    CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS: str = ""
    CODE_EXECUTION_REQUIRE_APPROVAL: bool = True

    # SaaS console owner. This is intentionally narrower than platform_admin:
    # production SaaS billing/model/quota configuration belongs to this account.
    SAAS_ADMIN_EMAIL: str = "admin@reeftotem.ai"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Jina AI (Reader + Search APIs)
    JINA_API_KEY: str = ""

    # Exa AI (Search API)
    EXA_API_KEY: str = ""


    # Sandbox configuration
    SANDBOX_TYPE: SandboxType = SandboxType.SUBPROCESS
    SANDBOX_API_KEY: str = ""
    SANDBOX_API_URL: str = ""
    SANDBOX_CPU_LIMIT: str = "0.5"
    SANDBOX_MEMORY_LIMIT: str = "256m"
    SANDBOX_ALLOW_NETWORK: bool = False
    SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING: bool = _default_allow_unsafe_bwrap_fallback()
    SANDBOX_DEFAULT_TIMEOUT: int = 30
    SANDBOX_MAX_TIMEOUT: int = 60

    model_config = {
        "env_file": [".env", "../.env"],
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }

    def validate_runtime_secrets(self) -> None:
        """Reject known development secrets whenever the app runs in production."""
        if self.ENVIRONMENT.strip().lower() not in {"production", "prod"}:
            return

        insecure = []
        if "change-me" in self.SECRET_KEY.lower():
            insecure.append("SECRET_KEY")
        if "change-me" in self.JWT_SECRET_KEY.lower():
            insecure.append("JWT_SECRET_KEY")
        if insecure:
            raise RuntimeError(
                "Production startup refused: replace insecure default values for "
                + ", ".join(insecure)
            )


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()


def unverified_local_signup_allowed(settings: Settings | None = None) -> bool:
    """Return whether a local-only no-SMTP password flow may auto-verify.

    The environment check is deliberately part of the decision instead of
    relying on deployment configuration alone.  Setting the flag in production
    therefore remains fail-closed.
    """
    effective = settings or get_settings()
    environment = effective.ENVIRONMENT.strip().lower()
    return bool(
        effective.ALLOW_UNVERIFIED_LOCAL_SIGNUP
        and environment in {"development", "test", "testing"}
    )


def get_sandbox_config() -> SandboxConfig:
    """Create SandboxConfig from application settings."""
    settings = get_settings()
    return SandboxConfig(
        type=settings.SANDBOX_TYPE,
        enabled=settings.CODE_EXECUTION_ENABLED,
        api_key=settings.SANDBOX_API_KEY,
        api_url=settings.SANDBOX_API_URL,
        cpu_limit=settings.SANDBOX_CPU_LIMIT,
        memory_limit=settings.SANDBOX_MEMORY_LIMIT,
        allow_network=settings.SANDBOX_ALLOW_NETWORK,
        allow_unsafe_fallback_when_bwrap_missing=settings.SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING,
        default_timeout=settings.SANDBOX_DEFAULT_TIMEOUT,
        max_timeout=settings.SANDBOX_MAX_TIMEOUT,
    )
