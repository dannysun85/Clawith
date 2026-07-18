"""MCP (Model Context Protocol) Client — connects to external MCP servers.

Supports two transport modes:
1. Streamable HTTP (modern) — single URL, POST JSON-RPC, response as JSON or SSE
2. SSE Transport (legacy but widely used) — GET /sse for event stream, POST /messages for requests

Transport is auto-detected with read-only MCP requests before a business
``tools/call`` is dispatched.  A business request is never replayed merely
because its response was lost on one transport.
Reference: https://modelcontextprotocol.io/docs
"""

import httpx
import json
import asyncio
import uuid
from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from loguru import logger

from app.services.mcp_security import MCPHTTPGuard


MAX_MCP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MCP_SSE_LINE_BYTES = 64 * 1024
MAX_MCP_SSE_LINES = 4096
MAX_MCP_TOOL_RESULT_BYTES = 128 * 1024
MAX_MCP_TOOLS = 100
MAX_MCP_TOOL_NAME_CHARS = 200
MAX_MCP_TOOL_DESCRIPTION_CHARS = 4096
MAX_MCP_TOOL_SCHEMA_BYTES = 64 * 1024
MAX_MCP_LIST_SECONDS = 45
MAX_MCP_CALL_SECONDS = 90
_MCP_GLOBAL_CONCURRENCY = asyncio.Semaphore(32)


class MCPTransportDetectionError(RuntimeError):
    """Neither transport accepted a read-only MCP probe."""


class MCPClient:
    """Client for connecting to MCP servers via Streamable HTTP or SSE transport.

    Auto-detects the transport mode on first request.
    """

    def __init__(self, server_url: str, api_key: str | None = None):
        # Extract apiKey from URL query params and move to Authorization header
        parsed = urlparse(server_url)
        self.api_key = api_key
        remaining_pairs: list[tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower().replace("-", "_") in {"apikey", "api_key"}:
                if not self.api_key:
                    self.api_key = value
                continue
            remaining_pairs.append((key, value))

        # Rebuild URL without apiKey in query string
        remaining_qs = urlencode(remaining_pairs, doseq=True)
        self.server_url = urlunparse(parsed._replace(query=remaining_qs)).rstrip("/")
        self._http_guard = MCPHTTPGuard(self.server_url)

        # Transport state
        self._transport: str | None = None  # "streamable" or "sse"
        self._session_id: str | None = None
        self._sse_messages_url: str | None = None  # POST endpoint for SSE transport

    def _headers(self) -> dict:
        """Build request headers with proper MCP and auth headers."""
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    @staticmethod
    def _bounded_tool_result(value: str) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= MAX_MCP_TOOL_RESULT_BYTES:
            return value
        prefix = encoded[:MAX_MCP_TOOL_RESULT_BYTES].decode(
            "utf-8",
            errors="ignore",
        )
        return f"{prefix}\n...[MCP tool result truncated]"

    def _parse_response(self, resp: httpx.Response) -> dict:
        """Parse response — handles both JSON and SSE (text/event-stream) formats."""
        content_type = resp.headers.get("content-type", "")

        # Save session ID if the server returns one
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        if "text/event-stream" in content_type:
            return self._parse_sse_response(resp.text)
        else:
            return resp.json()

    def _parse_sse_response(self, text: str) -> dict:
        """Extract the last JSON-RPC result from an SSE stream."""
        last_data = None
        lines = text.splitlines()
        if len(lines) > MAX_MCP_SSE_LINES:
            raise ValueError("MCP SSE response exceeded the line limit")
        for line in lines:
            if len(line.encode("utf-8")) > MAX_MCP_SSE_LINE_BYTES:
                raise ValueError("MCP SSE line exceeded the 64 KiB limit")
            if line.startswith("data:"):
                raw = line[5:].strip()
                if raw and raw != "[DONE]":
                    try:
                        last_data = json.loads(raw)
                    except json.JSONDecodeError:
                        pass
        if last_data is None:
            raise Exception("No valid JSON found in SSE response")
        return last_data

    async def _read_bounded_response(
        self,
        response: httpx.Response,
    ) -> httpx.Response:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_MCP_RESPONSE_BYTES:
                raise ValueError("MCP response exceeded the 2 MiB limit")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )

    async def _post_bounded(
        self,
        client: httpx.AsyncClient,
        url: str,
        **kwargs,
    ) -> httpx.Response:
        async with client.stream("POST", url, **kwargs) as response:
            return await self._read_bounded_response(response)

    async def _iter_bounded_sse_lines(
        self,
        response: httpx.Response,
    ) -> AsyncIterator[str]:
        total = 0
        line_count = 0
        pending = bytearray()
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_MCP_RESPONSE_BYTES:
                raise ValueError("MCP SSE response exceeded the 2 MiB limit")
            pending.extend(chunk)
            if len(pending) > MAX_MCP_SSE_LINE_BYTES and b"\n" not in pending:
                raise ValueError("MCP SSE line exceeded the 64 KiB limit")
            while b"\n" in pending:
                raw_line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                line_count += 1
                if line_count > MAX_MCP_SSE_LINES:
                    raise ValueError("MCP SSE response exceeded the line limit")
                if len(raw_line) > MAX_MCP_SSE_LINE_BYTES:
                    raise ValueError("MCP SSE line exceeded the 64 KiB limit")
                yield raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
        if pending:
            if len(pending) > MAX_MCP_SSE_LINE_BYTES:
                raise ValueError("MCP SSE line exceeded the 64 KiB limit")
            yield bytes(pending).rstrip(b"\r").decode("utf-8", errors="replace")

    # ── Streamable HTTP Transport ────────────────────────────────

    async def _streamable_initialize(self, client: httpx.AsyncClient) -> None:
        """Send MCP initialize + initialized handshake (Streamable HTTP)."""
        try:
            resp = await self._post_bounded(
                client,
                self.server_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "astra", "version": "1.0"},
                    },
                },
                headers=self._headers(),
            )
            if resp.status_code == 200:
                self._parse_response(resp)  # captures Mcp-Session-Id if present
            # Send initialized notification (required by MCP spec before other requests)
            await self._post_bounded(
                client,
                self.server_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=self._headers(),
            )
        except Exception:
            pass  # initialization failure is non-fatal — server may be stateless

    async def _streamable_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request via Streamable HTTP transport."""
        async with httpx.AsyncClient(
            timeout=30,
            **self._http_guard.client_kwargs(),
        ) as client:
            if not self._session_id:
                await self._streamable_initialize(client)

            body: dict = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}

            resp = await self._post_bounded(
                client,
                self.server_url,
                json=body,
                headers=self._headers(),
            )
            if resp.status_code not in (200, 201):
                resp.raise_for_status()
            return self._parse_response(resp)

    # ── SSE Transport ────────────────────────────────────────────

    async def _sse_connect(self) -> str:
        """Connect to SSE endpoint (GET /sse) and extract the messages URL.

        Returns the full POST URL for sending JSON-RPC messages.
        """
        # Determine SSE URL: if server_url ends with /sse use it directly,
        # otherwise append /sse
        sse_url = self.server_url if self.server_url.endswith("/sse") else f"{self.server_url}/sse"
        parsed = urlparse(sse_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        headers = {"Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        messages_url = None

        async with httpx.AsyncClient(
            timeout=15,
            **self._http_guard.client_kwargs(),
        ) as client:
            async with client.stream("GET", sse_url, headers=headers) as resp:
                if resp.status_code != 200:
                    raise Exception(f"SSE connect failed: HTTP {resp.status_code}")

                # Read SSE events until we get the endpoint event
                event_type = ""
                async for line in self._iter_bounded_sse_lines(resp):
                    line = line.strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if event_type == "endpoint" and data:
                            # data is typically a relative URL like /messages?sessionId=xxx
                            if data.startswith("http"):
                                messages_url = data
                            else:
                                messages_url = base_url + data
                            break
                    elif line == "":
                        # Empty line = end of SSE event block
                        pass

        if not messages_url:
            raise Exception("SSE endpoint did not return a messages URL")

        await self._http_guard.validate_url(messages_url)
        return messages_url

    async def _sse_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request via SSE transport.

        Opens a fresh SSE connection each call to get the messages endpoint,
        sends the JSON-RPC request, then reads responses from the SSE stream.
        """
        # Connect to SSE to get the messages endpoint
        sse_url = self.server_url if self.server_url.endswith("/sse") else f"{self.server_url}/sse"
        parsed = urlparse(sse_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        headers_sse = {"Accept": "text/event-stream"}
        headers_post = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.api_key:
            headers_sse["Authorization"] = f"Bearer {self.api_key}"
            headers_post["Authorization"] = f"Bearer {self.api_key}"

        body: dict = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}

        timeout = 60 if method == "tools/call" else 30

        async with httpx.AsyncClient(
            timeout=timeout,
            **self._http_guard.client_kwargs(),
        ) as client:
            # Open the SSE stream
            async with client.stream("GET", sse_url, headers=headers_sse) as sse_resp:
                if sse_resp.status_code != 200:
                    raise Exception(f"SSE connect failed: HTTP {sse_resp.status_code}")

                messages_url = None
                event_type = ""

                # Phase 1: Read until we get the endpoint event
                line_iter = self._iter_bounded_sse_lines(sse_resp)
                async for line in line_iter:
                    line = line.strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if event_type == "endpoint" and data:
                            if data.startswith("http"):
                                messages_url = data
                            else:
                                messages_url = base_url + data
                            break

                if not messages_url:
                    raise Exception("SSE endpoint did not return a messages URL")
                await self._http_guard.validate_url(messages_url)

                # Phase 2: MCP handshake — initialize + initialized notification
                init_body = {
                    "jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "astra", "version": "1.0"},
                    },
                }
                await self._post_bounded(
                    client,
                    messages_url,
                    json=init_body,
                    headers=headers_post,
                )
                # Send initialized notification (required before other requests)
                await self._post_bounded(
                    client,
                    messages_url,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers=headers_post,
                )

                # Send the actual request
                post_resp = await self._post_bounded(
                    client,
                    messages_url,
                    json=body,
                    headers=headers_post,
                )

                if post_resp.status_code >= 400:
                    post_resp.raise_for_status()

                # Phase 3: Read the response — either from POST response or from SSE stream
                if post_resp.status_code == 200:
                    ct = post_resp.headers.get("content-type", "")
                    if "application/json" in ct:
                        return post_resp.json()

                # Read response from SSE stream
                result = None
                async for line in line_iter:
                    line = line.strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if event_type == "message" and data:
                            try:
                                parsed_data = json.loads(data)
                                # Match our request ID
                                if isinstance(parsed_data, dict) and parsed_data.get("id") in (0, 1):
                                    result = parsed_data
                                    if parsed_data.get("id") == 1:
                                        break  # Got our actual request response
                            except json.JSONDecodeError:
                                pass

                if result is None:
                    raise Exception("No response received from SSE transport")
                return result

    # ── Auto-detect Transport ────────────────────────────────────

    async def _detect_and_request(self, method: str, params: dict | None = None) -> dict:
        """Auto-detect transport and send request.

        Strategy: If transport is already known, use it directly.
        Otherwise try Streamable HTTP first, fall back to SSE.
        """
        if self._transport == "sse":
            return await self._sse_request(method, params)
        if self._transport == "streamable":
            return await self._streamable_request(method, params)

        # ``tools/call`` is a business operation and may already have produced
        # a side effect when its response is lost.  Detect the transport with a
        # read-only catalog request first, then dispatch the business call once
        # on the selected transport.  Never use a failed business response as a
        # signal to replay it through the other transport.
        if method == "tools/call":
            streamable_error_type = "unknown"
            try:
                await self._streamable_request("tools/list")
                self._transport = "streamable"
            except Exception as streamable_err:
                streamable_error_type = type(streamable_err).__name__
                logger.info(
                    "[MCPClient] Streamable HTTP probe failed error_type={}; trying SSE",
                    streamable_error_type,
                )
                try:
                    await self._sse_request("tools/list")
                    self._transport = "sse"
                except Exception as sse_err:
                    raise MCPTransportDetectionError(
                        "Both MCP transport probes failed "
                        f"(streamable={streamable_error_type}, "
                        f"sse={type(sse_err).__name__})"
                    ) from sse_err

            if self._transport == "streamable":
                return await self._streamable_request(method, params)
            return await self._sse_request(method, params)

        # Auto-detect: try Streamable HTTP first. Python clears exception
        # variables after an `except ... as name` block exits, so keep a stable
        # string copy for the later SSE fallback error.
        streamable_error_type = "unknown"
        try:
            result = await self._streamable_request(method, params)
            self._transport = "streamable"
            return result
        except Exception as streamable_err:
            streamable_error_type = type(streamable_err).__name__
            logger.info(
                "[MCPClient] Streamable HTTP failed error_type={}; trying SSE",
                streamable_error_type,
            )

        try:
            result = await self._sse_request(method, params)
            self._transport = "sse"
            return result
        except Exception as sse_err:
            raise Exception(
                "Both MCP transports failed "
                f"(streamable={streamable_error_type}, sse={type(sse_err).__name__})"
            )

    async def _bounded_request(
        self,
        method: str,
        params: dict | None,
        total_seconds: int,
    ) -> dict:
        async with asyncio.timeout(total_seconds):
            async with _MCP_GLOBAL_CONCURRENCY:
                return await self._detect_and_request(method, params)

    # ── Public API ───────────────────────────────────────────────

    @staticmethod
    def _validated_tool_catalog(tools: object) -> list[dict]:
        if not isinstance(tools, list):
            raise ValueError("MCP tools/list returned a non-list catalog")
        if len(tools) > MAX_MCP_TOOLS:
            raise ValueError("MCP tools/list exceeded the tool count limit")

        catalog: list[dict] = []
        seen_names: set[str] = set()
        for raw_tool in tools:
            if not isinstance(raw_tool, dict):
                raise ValueError("MCP tools/list returned an invalid tool")
            name = raw_tool.get("name")
            if not isinstance(name, str):
                raise ValueError("MCP tool name must be a string")
            name = name.strip()
            if not name or len(name) > MAX_MCP_TOOL_NAME_CHARS:
                raise ValueError("MCP tool name is missing or too long")
            if name in seen_names:
                raise ValueError("MCP tools/list returned duplicate tool names")
            seen_names.add(name)

            description = raw_tool.get("description", "")
            if not isinstance(description, str):
                raise ValueError("MCP tool description must be a string")
            if len(description) > MAX_MCP_TOOL_DESCRIPTION_CHARS:
                description = (
                    description[:MAX_MCP_TOOL_DESCRIPTION_CHARS]
                    + "...[description truncated]"
                )

            schema = raw_tool.get("inputSchema", {})
            if not isinstance(schema, dict):
                raise ValueError("MCP tool inputSchema must be an object")
            serialized_schema = json.dumps(
                schema,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(serialized_schema) > MAX_MCP_TOOL_SCHEMA_BYTES:
                raise ValueError("MCP tool inputSchema exceeded the size limit")
            catalog.append(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": schema,
                }
            )
        return catalog

    async def list_tools(self) -> list[dict]:
        """Fetch available tools from the MCP server."""
        try:
            data = await self._bounded_request(
                "tools/list",
                None,
                MAX_MCP_LIST_SECONDS,
            )

            if "error" in data:
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise Exception(f"MCP error: {msg}")

            result = data.get("result", {})
            tools = result.get("tools", []) if isinstance(result, dict) else []
            return self._validated_tool_catalog(tools)
        except Exception as exc:
            raise Exception(
                f"MCP connection failed ({type(exc).__name__})"
            ) from None

    async def call_tool_result(self, tool_name: str, arguments: dict) -> dict:
        """Execute once and preserve the complete JSON-RPC response."""
        data = await self._bounded_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            MAX_MCP_CALL_SECONDS,
        )
        if not isinstance(data, dict):
            raise ValueError("MCP tools/call returned a non-object response")
        return data

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Legacy text adapter for callers outside Durable Runtime."""
        try:
            data = await self._bounded_request(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
                MAX_MCP_CALL_SECONDS,
            )

            if "error" in data:
                incident_id = uuid.uuid4().hex[:12]
                error = data["error"]
                error_code = error.get("code") if isinstance(error, dict) else None
                logger.warning(
                    "[MCPClient] Tool returned an error error_code={} incident_id={}",
                    error_code if isinstance(error_code, (int, str)) else "unknown",
                    incident_id,
                )
                return f"❌ MCP tool execution failed (incident {incident_id})"

            result = data.get("result", {})
            if isinstance(result, str):
                return self._bounded_tool_result(result)

            # MCP returns content as list of content blocks
            content_blocks = result.get("content", []) if isinstance(result, dict) else []
            texts = []
            for block in content_blocks:
                if isinstance(block, str):
                    texts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "image":
                        texts.append(f"[Image: {block.get('mimeType', 'image')}]")
                    else:
                        texts.append(str(block))
                else:
                    texts.append(str(block))

            rendered = "\n".join(texts) if texts else str(result)
            return self._bounded_tool_result(rendered)

        except Exception as exc:
            logger.warning(
                "[MCPClient] Tool call failed error_type={}",
                type(exc).__name__,
            )
            return "❌ MCP connection failed"
