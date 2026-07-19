"""Security policy for tenant-configurable MCP network endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import (
    SOCKET_OPTION,
    AsyncNetworkBackend,
    AsyncNetworkStream,
)


class MCPURLPolicyError(ValueError):
    """Raised when an MCP endpoint violates the public-egress policy."""


_SENSITIVE_QUERY_KEYS = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "credential",
    "key",
    "password",
    "passwd",
    "secret",
    "sig",
    "signature",
    "token",
}
_SENSITIVE_QUERY_SUFFIXES = (
    "_api_key",
    "_access_key",
    "_credential",
    "_key",
    "_password",
    "_secret",
    "_sig",
    "_signature",
    "_token",
)
_SMITHERY_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SMITHERY_CONNECT_ORIGIN = "https://api.smithery.ai"


def is_sensitive_mcp_query_key(key: str) -> bool:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key or ""))
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", snake_case).strip("_").lower()
    return normalized in _SENSITIVE_QUERY_KEYS or normalized.endswith(
        (*_SENSITIVE_QUERY_SUFFIXES, "_apikey")
    )


def split_mcp_url_secrets(url: str) -> tuple[str, str | None]:
    """Remove credential-like query values and return an encrypted-field payload."""

    raw = str(url or "").strip()
    # Keep the persisted endpoint contract identical to the runtime contract.
    # In particular, legacy userinfo and fragment credentials are rejected
    # instead of being silently retained in the public URL.
    _endpoint_parts(raw)
    parsed = urlsplit(raw)
    public_query: list[tuple[str, str]] = []
    secret_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        target = secret_query if is_sensitive_mcp_query_key(key) and value else public_query
        target.append((key, value))
    public_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(public_query, doseq=True),
            "",
        )
    )
    secret_payload = json.dumps(secret_query, separators=(",", ":")) if secret_query else None
    return public_url, secret_payload


def restore_mcp_url_secrets(url: str, secret_payload: str | None) -> str:
    """Restore decrypted query credentials immediately before network use."""

    _endpoint_parts(url)
    if not secret_payload:
        return url
    try:
        raw_pairs = json.loads(secret_payload)
        secret_pairs = [
            (str(item[0]), str(item[1]))
            for item in raw_pairs
            if isinstance(item, list) and len(item) == 2
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        raise MCPURLPolicyError("Stored MCP URL credentials are invalid")
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True) + secret_pairs
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), "")
    )


def _endpoint_parts(url: str) -> tuple[str, str, int, str]:
    raw = str(url or "").strip()
    if not raw or len(raw) > 2048:
        raise MCPURLPolicyError("MCP endpoint is missing or too long")
    try:
        parsed = urlsplit(raw)
        port = parsed.port or 443
    except ValueError as exc:
        raise MCPURLPolicyError("MCP endpoint is invalid") from exc
    if parsed.scheme.lower() != "https":
        raise MCPURLPolicyError("MCP endpoints must use HTTPS")
    if not parsed.hostname:
        raise MCPURLPolicyError("MCP endpoint must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise MCPURLPolicyError("MCP endpoint URL credentials are not allowed")
    if parsed.fragment:
        raise MCPURLPolicyError("MCP endpoint fragments are not allowed")
    return parsed.scheme.lower(), parsed.hostname.rstrip(".").lower(), port, parsed.path


def normalized_mcp_endpoint(url: str | None) -> str:
    """Return a credential-free endpoint identity for naming and matching."""

    if not url:
        return ""
    scheme, hostname, port, path = _endpoint_parts(url)
    parsed = urlsplit(url)
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port != 443:
        host = f"{host}:{port}"
    normalized_path = path.rstrip("/") or "/"
    # Non-secret query values can select a different workspace/tenant on the
    # same MCP service and therefore belong to server identity. Credential-like
    # values never do and must not enter names, logs, or grouping keys.
    public_query = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not is_sensitive_mcp_query_key(key)
    )
    return urlunsplit(
        (scheme, host, normalized_path, urlencode(public_query, doseq=True), "")
    )


def mcp_server_namespace(
    server_name: str | None,
    server_url: str | None,
) -> str | None:
    """Combine display identity and normalized endpoint without credentials."""

    name = str(server_name or "").strip().casefold()
    endpoint = normalized_mcp_endpoint(server_url) if server_url else ""
    if not name and not endpoint:
        return None
    return f"name:{name}|endpoint:{endpoint}"


def smithery_connect_url(
    namespace: str,
    connection_id: str | None = None,
) -> str:
    """Build a fixed-origin Smithery Connect URL from strict path segments."""

    values = [str(namespace or "")]
    if connection_id is not None:
        values.append(str(connection_id or ""))
    if any(not _SMITHERY_SEGMENT.fullmatch(value) for value in values):
        raise MCPURLPolicyError("Smithery connection identifier is invalid")
    path = f"/connect/{values[0]}"
    if connection_id is not None:
        path += f"/{values[1]}/mcp"
    return f"{SMITHERY_CONNECT_ORIGIN}{path}"


def is_smithery_runtime_url(url: str) -> bool:
    """Return true only for a valid HTTPS host below Smithery's run.tools."""

    try:
        _, hostname, _, _ = _endpoint_parts(url)
    except MCPURLPolicyError:
        return False
    return hostname.endswith(".run.tools") and hostname != "run.tools"


def _require_public_ip(raw_ip: str) -> None:
    try:
        address = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError as exc:
        raise MCPURLPolicyError("MCP endpoint resolved to an invalid address") from exc
    if not address.is_global:
        raise MCPURLPolicyError("MCP endpoint must resolve only to public addresses")


async def validate_public_mcp_url(url: str) -> str:
    """Require HTTPS and public DNS results before any MCP request."""

    _, hostname, port, _ = _endpoint_parts(url)
    await _resolve_public_addresses(hostname, port)
    return str(url).strip()


async def _resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve once, reject mixed/private answers, and return numeric peers."""

    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                0,
                socket.SOCK_STREAM,
            ),
            timeout=5,
        )
    except (OSError, TimeoutError) as exc:
        raise MCPURLPolicyError("MCP endpoint hostname could not be resolved") from exc
    addresses = {str(info[4][0]) for info in infos if info[4]}
    if not addresses:
        raise MCPURLPolicyError("MCP endpoint hostname could not be resolved")
    for address in addresses:
        _require_public_ip(address)
    return tuple(sorted(addresses, key=lambda value: (":" in value, value)))


class PublicIPNetworkBackend(AsyncNetworkBackend):
    """Resolve then connect to a validated numeric IP before HTTP bytes exist.

    Passing the original hostname to the socket layer would leave a DNS
    rebinding gap between an application preflight and ``connect(2)``.  TLS
    still receives the original hostname from httpcore, so certificate/SNI
    verification remains intact while the TCP destination is pinned.
    """

    def __init__(self, backend: AsyncNetworkBackend | None = None):
        self._backend = backend or AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        addresses = await _resolve_public_addresses(host, port)
        stream = await self._backend.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        peer = stream.get_extra_info("server_addr")
        try:
            if not peer:
                raise MCPURLPolicyError("MCP connection peer could not be verified")
            _require_public_ip(str(peer[0]))
        except Exception:
            await stream.aclose()
            raise
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        del path, timeout, socket_options
        raise MCPURLPolicyError("MCP Unix socket transport is not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PublicOnlyAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose socket backend pins public DNS answers."""

    def __init__(self) -> None:
        super().__init__(trust_env=False)
        pool: Any = getattr(self, "_pool", None)
        if pool is None or not hasattr(pool, "_network_backend"):
            raise RuntimeError("Installed httpx/httpcore cannot enforce MCP peer pinning")
        pool._network_backend = PublicIPNetworkBackend()


def _origin(url: str) -> tuple[str, str, int]:
    scheme, hostname, port, _ = _endpoint_parts(url)
    return scheme, hostname, port


class MCPHTTPGuard:
    """Layered httpx SSRF guard for tenant-configurable MCP endpoints.

    DNS is checked before each request, redirects and SSE endpoints stay on the
    original origin, and the connected peer is checked before a response is
    consumed. Production must also enforce an outbound network policy because
    application-level DNS checks alone are not a complete rebinding boundary.
    """

    def __init__(self, base_url: str):
        self._origin = _origin(base_url)

    async def validate_url(self, url: str) -> None:
        if _origin(url) != self._origin:
            raise MCPURLPolicyError("Cross-origin MCP redirects are not allowed")
        await validate_public_mcp_url(url)

    async def validate_request(self, request: httpx.Request) -> None:
        await self.validate_url(str(request.url))

    async def validate_response(self, response: httpx.Response) -> None:
        network_stream = response.extensions.get("network_stream")
        peer = (
            network_stream.get_extra_info("server_addr")
            if network_stream is not None
            else None
        )
        if not peer:
            raise MCPURLPolicyError("MCP connection peer could not be verified")
        _require_public_ip(str(peer[0]))

    def client_kwargs(self) -> dict:
        return {
            "follow_redirects": True,
            "trust_env": False,
            "transport": PublicOnlyAsyncHTTPTransport(),
            "event_hooks": {
                "request": [self.validate_request],
                "response": [self.validate_response],
            },
        }


class PublicArtifactHTTPGuard:
    """SSRF guard for credential-free public artifact downloads.

    Provider APIs commonly return a short-lived URL which redirects to a
    different public CDN origin.  Unlike MCP/API traffic, these GET requests
    contain no credentials and may follow such redirects.  Every redirect hop
    is still required to use HTTPS, resolve only to public addresses, and
    connect to a public peer through :class:`PublicOnlyAsyncHTTPTransport`.
    """

    async def validate_request(self, request: httpx.Request) -> None:
        await validate_public_mcp_url(str(request.url))

    async def validate_response(self, response: httpx.Response) -> None:
        network_stream = response.extensions.get("network_stream")
        peer = (
            network_stream.get_extra_info("server_addr")
            if network_stream is not None
            else None
        )
        if not peer:
            raise MCPURLPolicyError("Artifact connection peer could not be verified")
        _require_public_ip(str(peer[0]))

    def client_kwargs(self) -> dict:
        return {
            "follow_redirects": True,
            "max_redirects": 5,
            "trust_env": False,
            "transport": PublicOnlyAsyncHTTPTransport(),
            "event_hooks": {
                "request": [self.validate_request],
                "response": [self.validate_response],
            },
        }


class TrustedProviderProxyHTTPGuard:
    """Constrain credentialed provider traffic sent through an explicit proxy.

    Some operator networks use a TUN/fake-IP DNS range (for example
    ``198.18.0.0/15``). A local DNS preflight correctly rejects those
    synthetic addresses, even though the configured HTTP proxy resolves and
    connects to the public provider. This narrow opt-in path only permits an
    exact, reviewed HTTPS provider hostname. Normal MCP traffic never uses it.
    """

    def __init__(
        self,
        base_url: str,
        *,
        allowed_hostnames: Iterable[str],
    ) -> None:
        self._origin = _origin(base_url)
        allowed = {str(value).strip().lower() for value in allowed_hostnames}
        if self._origin[1] not in allowed:
            raise MCPURLPolicyError(
                "Provider proxy endpoint hostname is not allowlisted"
            )

    async def validate_request(self, request: httpx.Request) -> None:
        if _origin(str(request.url)) != self._origin:
            raise MCPURLPolicyError(
                "Cross-origin provider proxy redirects are not allowed"
            )

    def client_kwargs(self, *, proxy_url: str) -> dict:
        proxy = str(proxy_url or "").strip()
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MCPURLPolicyError("Configured HTTP proxy URL is invalid")
        return {
            "follow_redirects": True,
            "trust_env": False,
            "proxy": proxy,
            "event_hooks": {"request": [self.validate_request]},
        }


class PublicArtifactProxyHTTPGuard:
    """Validate credential-free HTTPS artifact downloads through a proxy.

    DNS and the connected peer are owned by the explicitly configured proxy,
    so application-side public-IP pinning is unavailable. We retain the
    boundaries enforceable locally: HTTPS/TLS, no URL credentials, no local or
    intranet host syntax, no non-public IP literals, and bounded redirects.
    """

    async def validate_request(self, request: httpx.Request) -> None:
        _, hostname, _, _ = _endpoint_parts(str(request.url))
        normalized = hostname.rstrip(".").lower()
        if (
            normalized == "localhost"
            or normalized.endswith(".localhost")
            or normalized.endswith(".local")
            or "." not in normalized
        ):
            raise MCPURLPolicyError(
                "Artifact proxy endpoint must use a public DNS hostname"
            )
        try:
            ipaddress.ip_address(normalized.split("%", 1)[0])
        except ValueError:
            pass
        else:
            _require_public_ip(normalized)

    def client_kwargs(self, *, proxy_url: str) -> dict:
        proxy = str(proxy_url or "").strip()
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MCPURLPolicyError("Configured HTTP proxy URL is invalid")
        return {
            "follow_redirects": True,
            "max_redirects": 5,
            "trust_env": False,
            "proxy": proxy,
            "event_hooks": {"request": [self.validate_request]},
        }
