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

export type TenantSwitchSession = {
    token: string;
    targetTenantId: string | null;
};

export function consumeTenantSwitchSessionFromUrl(
    url: URL,
    pathsWithOwnToken: string[],
): TenantSwitchSession | null {
    if (pathsWithOwnToken.includes(url.pathname)) return null;

    const fragment = new URLSearchParams(url.hash.replace(/^#/, ''));
    let token = fragment.get('session_token');
    let targetTenantId: string | null = null;
    if (token) {
        targetTenantId = fragment.get('target_tenant_id');
        fragment.delete('session_token');
        fragment.delete('target_tenant_id');
        const remaining = fragment.toString();
        url.hash = remaining ? `#${remaining}` : '';
    }

    // Compatibility for pre-release links. Current servers never generate it.
    if (!token) {
        token = url.searchParams.get('token');
        if (token) url.searchParams.delete('token');
    }
    return token ? { token, targetTenantId } : null;
}

export function consumeSessionTokenFromUrl(url: URL, pathsWithOwnToken: string[]): string | null {
    return consumeTenantSwitchSessionFromUrl(url, pathsWithOwnToken)?.token || null;
}

export function resolveBootstrapToken(
    urlToken: string | null,
    storedToken: string | null,
    capturedToken: string | null,
): string | null {
    // React.StrictMode replays mount effects in development. A replay must read
    // the token written by the first pass instead of restoring the render-time
    // token captured before a tenant switch fragment was consumed.
    return urlToken || storedToken || capturedToken;
}

export function normalizeTenantRedirectUrl(
    redirectUrl: string,
    currentHref: string,
    expectedTargetTenantId?: string,
): string {
    const currentUrl = new URL(currentHref);
    const targetUrl = new URL(redirectUrl, currentUrl.origin);
    if (targetUrl.protocol !== 'http:' && targetUrl.protocol !== 'https:') {
        throw new Error('Tenant redirect must use http or https');
    }
    if (targetUrl.username || targetUrl.password) {
        throw new Error('Tenant redirect must not contain URL credentials');
    }
    const fragment = new URLSearchParams(targetUrl.hash.replace(/^#/, ''));
    const declaredTargetTenantId = fragment.get('target_tenant_id');
    if (
        expectedTargetTenantId
        && declaredTargetTenantId
        && declaredTargetTenantId !== expectedTargetTenantId
    ) {
        throw new Error('Tenant redirect does not match the requested company');
    }
    if (expectedTargetTenantId) {
        fragment.set('target_tenant_id', expectedTargetTenantId);
        targetUrl.hash = fragment.toString();
    }
    const loopbackHosts = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);
    const isSameBrowserHost =
        targetUrl.hostname === currentUrl.hostname ||
        (loopbackHosts.has(targetUrl.hostname) && loopbackHosts.has(currentUrl.hostname));
    if (isSameBrowserHost) {
        // In local development the API may advertise localhost while the browser
        // was opened through 127.0.0.1 (or vice versa). Keep the redirect on the
        // active frontend origin so it cannot accidentally land on the API port.
        targetUrl.hostname = currentUrl.hostname;
        targetUrl.protocol = currentUrl.protocol;
        targetUrl.port = currentUrl.port;
    }
    targetUrl.pathname = '/';
    // Deliberately preserve #session_token: it is the only credential transport
    // available to a different origin and is consumed before protected UI renders.
    return targetUrl.toString();
}
