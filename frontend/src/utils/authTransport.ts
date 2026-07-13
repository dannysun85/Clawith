const API_BASE = '/api';

export const WEBSOCKET_APP_PROTOCOL = 'astra-chat';
export const WEBSOCKET_TOKEN_PROTOCOL_PREFIX = 'astra-token.';

export function websocketAuthProtocols(token: string): string[] {
    return [WEBSOCKET_APP_PROTOCOL, `${WEBSOCKET_TOKEN_PROTOCOL_PREFIX}${token}`];
}

export function buildWorkspaceDownloadUrl(
    agentId: string,
    path: string,
    options?: { inline?: boolean },
): string {
    const params = new URLSearchParams({ path });
    if (options?.inline) params.set('inline', '1');
    return `${API_BASE}/agents/${agentId}/files/download?${params.toString()}`;
}

export function establishBrowserSession(token: string): void {
    void fetch(`${API_BASE}/auth/browser-session`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
        credentials: 'same-origin',
    }).catch(() => {
        // API Bearer auth and the WebSocket subprotocol remain available. A
        // later setAuth call retries cookie establishment.
    });
}

export function clearBrowserSession(): void {
    void fetch(`${API_BASE}/auth/browser-session`, {
        method: 'DELETE',
        credentials: 'same-origin',
    }).catch(() => {
        // The cookie expires with its JWT even when the network is unavailable.
    });
}

export function consumeSessionTokenFromUrl(url: URL, pathsWithOwnToken: string[]): string | null {
    if (pathsWithOwnToken.includes(url.pathname)) return null;

    const fragment = new URLSearchParams(url.hash.replace(/^#/, ''));
    let token = fragment.get('session_token');
    if (token) {
        fragment.delete('session_token');
        const remaining = fragment.toString();
        url.hash = remaining ? `#${remaining}` : '';
    }

    // Compatibility for pre-release links. Current servers never generate it.
    if (!token) {
        token = url.searchParams.get('token');
        if (token) url.searchParams.delete('token');
    }
    return token;
}
