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

export async function establishBrowserSession(token: string): Promise<void> {
    const response = await fetch(`${API_BASE}/auth/browser-session`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
        credentials: 'same-origin',
    });
    if (!response.ok) {
        throw new Error(`Browser session could not be established (HTTP ${response.status})`);
    }
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

export function normalizeTenantRedirectUrl(redirectUrl: string, currentHref: string): string {
    const currentUrl = new URL(currentHref);
    const targetUrl = new URL(redirectUrl, currentUrl.origin);
    if (targetUrl.hostname === currentUrl.hostname) {
        targetUrl.protocol = currentUrl.protocol;
        targetUrl.port = currentUrl.port;
    }
    targetUrl.pathname = '/';
    // Deliberately preserve #session_token: it is the only credential transport
    // available to a different origin and is consumed before protected UI renders.
    return targetUrl.toString();
}
