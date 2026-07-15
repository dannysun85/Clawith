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
