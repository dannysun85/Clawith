type MCPToolLike = {
    id?: string;
    available?: boolean;
    type?: string;
    category?: string;
    mcp_server_name?: string | null;
    mcp_server_url?: string | null;
};

export const bulkEligibleToolIds = (tools: MCPToolLike[]) => tools
    .filter((tool) => tool.available !== false && Boolean(tool.id))
    .map((tool) => String(tool.id));

const sensitiveQueryKey = (key: string) => {
    const snake = key.replace(/([a-z0-9])([A-Z])/g, '$1_$2');
    const normalized = snake.replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase();
    return [
        'api_key', 'apikey', 'auth', 'authorization', 'credential', 'key',
        'password', 'passwd', 'secret', 'sig', 'signature', 'token',
    ].includes(normalized) || /_(api_key|apikey|access_key|credential|key|password|secret|sig|signature|token)$/.test(normalized);
};

export const normalizedMcpEndpointForGrouping = (rawUrl: string | null | undefined) => {
    const value = String(rawUrl || '').trim();
    if (!value) return 'endpoint:none';
    try {
        const parsed = new URL(value);
        const protocol = parsed.protocol.toLowerCase();
        const hostname = parsed.hostname.toLowerCase().replace(/\.$/, '');
        const port = parsed.port && parsed.port !== '443' ? `:${parsed.port}` : '';
        const path = parsed.pathname.replace(/\/+$/, '') || '/';
        const query = [...parsed.searchParams.entries()]
            .filter(([key]) => !sensitiveQueryKey(key))
            .sort(([leftKey, leftValue], [rightKey, rightValue]) => (
                leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue)
            ));
        const encoded = new URLSearchParams(query).toString();
        return `${protocol}//${hostname}${port}${path}${encoded ? `?${encoded}` : ''}`;
    } catch {
        // API responses mask legacy credentials before they reach the UI. Keep
        // malformed legacy endpoints separate until an admin remediates them.
        return `legacy:${value}`;
    }
};

export const mcpToolGroupKey = (tool: MCPToolLike) => {
    const serverName = String(tool.mcp_server_name || '').trim();
    if (tool.type !== 'mcp' || !serverName) return tool.category || 'general';
    return `mcp:${serverName.toLowerCase()}|${normalizedMcpEndpointForGrouping(tool.mcp_server_url)}`;
};

export const defaultMcpServerName = (rawUrl: string) => {
    try {
        return new URL(rawUrl).hostname || 'MCP Server';
    } catch {
        return 'MCP Server';
    }
};
