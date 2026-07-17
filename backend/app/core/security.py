"""Security utilities: JWT, password hashing, and authentication dependencies."""

import asyncio
import base64
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import bcrypt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from fastapi import Depends, HTTPException, Request, Response, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db

settings = get_settings()

BROWSER_SESSION_COOKIE = "astra_session"
WEBSOCKET_APP_PROTOCOL = "astra-chat"
WEBSOCKET_TOKEN_PROTOCOL_PREFIX = "astra-token."

# Bearer token scheme
security = HTTPBearer()

# Thread pool for CPU-intensive bcrypt operations (avoids blocking the event loop)
_bcrypt_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bcrypt")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (sync, for use in background tasks)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash (sync, for use in background tasks)."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


async def hash_password_async(password: str) -> str:
    """Hash a password using bcrypt without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_bcrypt_executor, hash_password, password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_bcrypt_executor, verify_password, plain_password, hashed_password)


def encrypt_data(plaintext: str, key: str) -> str:
    """Encrypt a string using AES-256-CBC with the given key.

    Args:
        plaintext: The string to encrypt
        key: The encryption key (will be hashed to 32 bytes)

    Returns:
        Base64-encoded encrypted string with IV prefix
    """
    if not plaintext:
        return ""

    # Derive 32-byte key from the secret key
    key_bytes = key.encode("utf-8")
    # Use SHA-256 hash to get exactly 32 bytes for AES-256
    import hashlib

    aes_key = hashlib.sha256(key_bytes).digest()

    # Generate random 16-byte IV
    iv = os.urandom(16)

    # Create cipher and encrypt
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    padded_data = pad(plaintext.encode("utf-8"), AES.block_size)
    encrypted = cipher.encrypt(padded_data)

    # Prepend IV to ciphertext and encode as base64
    result = base64.b64encode(iv + encrypted).decode("utf-8")
    return result


def decrypt_data(ciphertext: str, key: str) -> str:
    """Decrypt a string encrypted with encrypt_data.

    Args:
        ciphertext: Base64-encoded encrypted string with IV prefix
        key: The encryption key (must match the key used for encryption)

    Returns:
        Decrypted plaintext string

    Raises:
        ValueError: If decryption fails (wrong key, corrupted data, etc.)
    """
    if not ciphertext:
        return ""

    try:
        # Decode base64
        raw = base64.b64decode(ciphertext)

        # Extract IV (first 16 bytes) and ciphertext
        iv = raw[:16]
        encrypted = raw[16:]

        # Derive key
        import hashlib

        aes_key = hashlib.sha256(key.encode("utf-8")).digest()

        # Decrypt
        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        padded_data = cipher.decrypt(encrypted)
        plaintext = unpad(padded_data, AES.block_size).decode("utf-8")

        return plaintext
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}") from e




def create_access_token(
    user_id: str,
    role: str,
    expires_delta: timedelta | None = None,
    *,
    auth_version: int,
) -> str:
    """Create a JWT access token."""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {
        "sub": user_id,
        "role": role,
        "av": max(0, int(auth_version)),
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


def access_token_matches_identity(payload: dict, identity: object) -> bool:
    """Return whether a JWT predates the Identity's latest revocation event."""

    try:
        if "av" not in payload or not hasattr(identity, "auth_version"):
            return False
        token_version = int(payload["av"])
        identity_version = int(identity.auth_version)
    except (TypeError, ValueError):
        return False
    return token_version >= 0 and identity_version >= 0 and token_version == identity_version


def identity_auth_version(user_or_identity: object) -> int:
    """Read an Identity revocation version from an Identity or loaded User."""

    identity = getattr(user_or_identity, "identity", None) or user_or_identity
    if not hasattr(identity, "auth_version"):
        raise ValueError("Identity auth_version is required before issuing an access token")
    try:
        version = int(identity.auth_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Identity auth_version must be a non-negative integer") from exc
    if version < 0:
        raise ValueError("Identity auth_version must be a non-negative integer")
    return version


def _request_is_secure(request: Request | None) -> bool:
    """Return whether browser cookies must be marked Secure."""
    if settings.ENVIRONMENT.strip().lower() in {"production", "prod"}:
        return True
    if request is None:
        return False
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return forwarded_proto == "https" or request.url.scheme == "https"


def set_browser_session_cookie(response: Response, token: str, request: Request | None = None) -> None:
    """Set the same-origin, HttpOnly browser credential used by media URLs."""
    response.set_cookie(
        key=BROWSER_SESSION_COOKIE,
        value=token,
        max_age=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
        secure=_request_is_secure(request),
        httponly=True,
        samesite="lax",
    )


def clear_browser_session_cookie(response: Response, request: Request | None = None) -> None:
    """Delete the browser credential without exposing its value to JavaScript."""
    response.delete_cookie(
        key=BROWSER_SESSION_COOKIE,
        path="/",
        secure=_request_is_secure(request),
        httponly=True,
        samesite="lax",
    )


def _websocket_protocols(websocket: WebSocket) -> list[str]:
    raw = websocket.headers.get("sec-websocket-protocol") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def extract_websocket_access_token(websocket: WebSocket, legacy_query_token: str | None = None) -> str | None:
    """Resolve WebSocket auth without putting bearer credentials in the URL.

    The query-string fallback is temporary compatibility for already-cached
    frontend bundles. New clients use a dedicated Sec-WebSocket-Protocol
    value, while the HttpOnly browser cookie is a secondary fallback.
    """
    for protocol in _websocket_protocols(websocket):
        if protocol.startswith(WEBSOCKET_TOKEN_PROTOCOL_PREFIX):
            token = protocol[len(WEBSOCKET_TOKEN_PROTOCOL_PREFIX):].strip()
            if token:
                return token
    cookie_token = websocket.cookies.get(BROWSER_SESSION_COOKIE)
    if cookie_token:
        return cookie_token
    return (legacy_query_token or "").strip() or None


def websocket_response_subprotocol(websocket: WebSocket) -> str | None:
    """Negotiate only the non-secret application protocol back to the client."""
    protocols = _websocket_protocols(websocket)
    return WEBSOCKET_APP_PROTOCOL if WEBSOCKET_APP_PROTOCOL in protocols else None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Require an active account in an active tenant for business APIs."""
    from app.models.tenant import Tenant
    from app.models.user import User

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        parsed_user_id = uuid.UUID(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    result = await db.execute(
        select(User)
        .where(User.id == parsed_user_id)
        .options(selectinload(User.identity))
    )
    user = result.scalar_one_or_none()
    if (
        not user
        or not user.is_active
        or not user.identity
        or not user.identity.is_active
        or not access_token_matches_identity(payload, user.identity)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    if user.tenant_id is not None:
        tenant = await db.get(Tenant, user.tenant_id)
        if not tenant or not tenant.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization is unavailable",
            )
    # Authentication is a read-only preflight. Release the pooled connection
    # before the endpoint performs CPU work or external network I/O.
    await db.commit()
    return user


async def get_authenticated_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Require an active account, without requiring the current tenant to be active.

    This narrow dependency exists for account recovery operations such as
    listing and switching memberships. Business APIs must use
    ``get_current_user`` so tenant suspension remains authoritative.
    """
    from app.models.user import User

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        parsed_user_id = uuid.UUID(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    result = await db.execute(
        select(User)
        .where(User.id == parsed_user_id)
        .options(selectinload(User.identity))
    )
    user = result.scalar_one_or_none()
    if (
        not user
        or not user.is_active
        or not user.identity
        or not user.identity.is_active
        or not access_token_matches_identity(payload, user.identity)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    await db.commit()
    return user


async def get_verification_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Allow only active users or explicit email-verification-pending users."""
    from app.models.user import User

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    try:
        parsed_user_id = uuid.UUID(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    result = await db.execute(
        select(User)
        .where(User.id == parsed_user_id)
        .options(selectinload(User.identity))
    )
    user = result.scalar_one_or_none()
    if (
        not user
        or not user.identity
        or not user.identity.is_active
        or not access_token_matches_identity(payload, user.identity)
        or not (user.is_active or user.activation_pending_email_verification)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    await db.commit()
    return user


async def get_current_admin(current_user=Depends(get_current_user)):
    """Dependency to require admin role (platform_admin or org_admin)."""
    identity_is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
    if current_user.role not in ("platform_admin", "org_admin") and not identity_is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


async def get_saas_admin(current_user=Depends(get_current_user)):
    """Require the configured owner of the platform-wide SaaS console.

    Global billing catalogs, cross-tenant subscription assignments, credential
    pools, and model routes share this boundary. A tenant ``org_admin`` must
    never be able to mutate those platform-wide resources.
    """
    identity_is_platform_admin = bool(
        getattr(getattr(current_user, "identity", None), "is_platform_admin", False)
    )
    is_platform_admin = current_user.role == "platform_admin" or identity_is_platform_admin
    expected_email = (settings.SAAS_ADMIN_EMAIL or "").strip().lower()
    user_email = (getattr(current_user, "email", None) or "").strip().lower()
    if not is_platform_admin or not expected_email or user_email != expected_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SaaS admin access is restricted to the configured owner account.",
        )
    return current_user


# Role hierarchy: higher index = more privileges
ROLE_HIERARCHY = ["member", "agent_admin", "org_admin", "platform_admin"]


def require_role(*allowed_roles: str):
    """Factory to create a dependency that checks if the user has one of the allowed roles.

    Usage:
        @router.post("/", dependencies=[Depends(require_role("org_admin", "platform_admin"))])
        async def my_endpoint(...):
    """
    async def _check(current_user=Depends(get_current_user)):
        identity_is_platform_admin = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
        if current_user.role not in allowed_roles and not ("platform_admin" in allowed_roles and identity_is_platform_admin):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要以下角色之一: {', '.join(allowed_roles)}",
            )
        return current_user
    return _check
