from __future__ import annotations

import pytest

from app.services import mcp_security


class _Stream:
    def __init__(self, peer):
        self.peer = peer
        self.closed = False

    def get_extra_info(self, name):
        assert name == "server_addr"
        return self.peer

    async def aclose(self):
        self.closed = True


class _Backend:
    def __init__(self, stream):
        self.stream = stream
        self.connects = []

    async def connect_tcp(self, host, port, **kwargs):
        self.connects.append((host, port, kwargs))
        return self.stream

    async def sleep(self, _seconds):
        return None


@pytest.mark.asyncio
async def test_mcp_transport_pins_numeric_public_ip_before_connect(monkeypatch):
    stream = _Stream(("93.184.216.34", 443))
    backend = _Backend(stream)

    async def resolve(host, port):
        assert (host, port) == ("rebind.example", 443)
        return ("93.184.216.34",)

    monkeypatch.setattr(mcp_security, "_resolve_public_addresses", resolve)
    network = mcp_security.PublicIPNetworkBackend(backend)

    result = await network.connect_tcp("rebind.example", 443)

    assert result is stream
    assert backend.connects[0][0] == "93.184.216.34"
    assert backend.connects[0][0] != "rebind.example"


@pytest.mark.asyncio
async def test_mcp_transport_closes_rebound_private_peer_before_http(monkeypatch):
    stream = _Stream(("127.0.0.1", 443))
    backend = _Backend(stream)

    async def resolve(_host, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr(mcp_security, "_resolve_public_addresses", resolve)
    network = mcp_security.PublicIPNetworkBackend(backend)

    with pytest.raises(mcp_security.MCPURLPolicyError):
        await network.connect_tcp("rebind.example", 443)

    assert stream.closed is True


def test_mcp_http_guard_installs_pinned_transport():
    kwargs = mcp_security.MCPHTTPGuard("https://mcp.example/mcp").client_kwargs()

    assert isinstance(kwargs["transport"], mcp_security.PublicOnlyAsyncHTTPTransport)


@pytest.mark.asyncio
async def test_public_artifact_guard_allows_cross_origin_redirects_but_validates_every_hop(
    monkeypatch,
):
    validated: list[str] = []

    async def validate(url: str):
        validated.append(url)
        return url

    monkeypatch.setattr(mcp_security, "validate_public_mcp_url", validate)
    guard = mcp_security.PublicArtifactHTTPGuard()

    await guard.validate_request(
        mcp_security.httpx.Request("GET", "https://provider.example/object")
    )
    await guard.validate_request(
        mcp_security.httpx.Request("GET", "https://cdn.example/signed-object")
    )

    assert validated == [
        "https://provider.example/object",
        "https://cdn.example/signed-object",
    ]
    kwargs = guard.client_kwargs()
    assert kwargs["follow_redirects"] is True
    assert kwargs["max_redirects"] == 5
    assert isinstance(kwargs["transport"], mcp_security.PublicOnlyAsyncHTTPTransport)


@pytest.mark.asyncio
async def test_public_artifact_guard_rejects_a_private_redirect_hop(monkeypatch):
    async def validate(url: str):
        if "private" in url:
            raise mcp_security.MCPURLPolicyError("private destination")
        return url

    monkeypatch.setattr(mcp_security, "validate_public_mcp_url", validate)
    guard = mcp_security.PublicArtifactHTTPGuard()

    with pytest.raises(mcp_security.MCPURLPolicyError, match="private destination"):
        await guard.validate_request(
            mcp_security.httpx.Request("GET", "https://private.example/object")
        )


@pytest.mark.asyncio
async def test_public_artifact_guard_rejects_private_connected_peer():
    guard = mcp_security.PublicArtifactHTTPGuard()
    stream = _Stream(("127.0.0.1", 443))
    response = mcp_security.httpx.Response(
        302,
        request=mcp_security.httpx.Request("GET", "https://cdn.example/object"),
        extensions={"network_stream": stream},
    )

    with pytest.raises(mcp_security.MCPURLPolicyError):
        await guard.validate_response(response)


@pytest.mark.asyncio
async def test_trusted_provider_proxy_guard_rejects_unreviewed_and_cross_origin_hosts():
    with pytest.raises(
        mcp_security.MCPURLPolicyError,
        match="not allowlisted",
    ):
        mcp_security.TrustedProviderProxyHTTPGuard(
            "https://tenant.invalid/v1/image_generation",
            allowed_hostnames={"api.minimaxi.com"},
        )

    guard = mcp_security.TrustedProviderProxyHTTPGuard(
        "https://api.minimaxi.com/v1/image_generation",
        allowed_hostnames={"api.minimaxi.com"},
    )
    with pytest.raises(
        mcp_security.MCPURLPolicyError,
        match="Cross-origin",
    ):
        await guard.validate_request(
            mcp_security.httpx.Request(
                "GET",
                "https://internal.invalid/object",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/object",
        "https://localhost/object",
        "https://service.local/object",
        "https://127.0.0.1/object",
        "https://intranet/object",
    ],
)
async def test_public_artifact_proxy_guard_rejects_unsafe_destinations(url):
    guard = mcp_security.PublicArtifactProxyHTTPGuard()

    with pytest.raises(mcp_security.MCPURLPolicyError):
        await guard.validate_request(mcp_security.httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_public_artifact_proxy_guard_allows_public_https_hostname():
    guard = mcp_security.PublicArtifactProxyHTTPGuard()

    await guard.validate_request(
        mcp_security.httpx.Request(
            "GET",
            "https://cdn.example.com/signed-object",
        )
    )
    kwargs = guard.client_kwargs(proxy_url="http://127.0.0.1:7890")
    assert kwargs["proxy"] == "http://127.0.0.1:7890"
    assert kwargs["follow_redirects"] is True
