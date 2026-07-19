import { afterEach, describe, expect, it, vi } from 'vitest';
import {
    buildWorkspaceDownloadUrl,
    consumeSessionTokenFromUrl,
    establishBrowserSession,
    normalizeTenantRedirectUrl,
    resolveBootstrapToken,
    websocketAuthProtocols,
} from './authTransport';

describe('credential-safe browser transports', () => {
    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('keeps workspace download credentials out of URLs', () => {
        const url = buildWorkspaceDownloadUrl('agent-id', 'workspace/videos/demo.mp4', { inline: true });

        expect(url).toContain('path=workspace%2Fvideos%2Fdemo.mp4');
        expect(url).toContain('inline=1');
        expect(url).not.toContain('token=');
    });

    it('passes websocket credentials through a protocol, not a URL', () => {
        expect(websocketAuthProtocols('header.payload.signature')).toEqual([
            'astra-chat',
            'astra-token.header.payload.signature',
        ]);
    });

    it('consumes cross-domain session fragments and removes them', () => {
        const url = new URL('https://tenant.example/?view=home#session_token=header.payload.signature');

        expect(consumeSessionTokenFromUrl(url, ['/reset-password', '/verify-email'])).toBe('header.payload.signature');
        expect(url.href).toBe('https://tenant.example/?view=home');
    });

    it('does not consume reset-password query tokens as session JWTs', () => {
        const url = new URL('https://tenant.example/reset-password?token=one-time');

        expect(consumeSessionTokenFromUrl(url, ['/reset-password', '/verify-email'])).toBeNull();
        expect(url.searchParams.get('token')).toBe('one-time');
    });

    it('preserves the fragment credential during cross-origin tenant switching', () => {
        const redirect = normalizeTenantRedirectUrl(
            'https://tenant.example/base#session_token=target.jwt.token',
            'https://platform.example/current',
        );

        expect(redirect).toBe('https://tenant.example/#session_token=target.jwt.token');
    });

    it('keeps loopback tenant switching on the active frontend origin', () => {
        const redirect = normalizeTenantRedirectUrl(
            'http://localhost:8008/#session_token=target.jwt.token',
            'http://127.0.0.1:3008/admin/platform-settings',
        );

        expect(redirect).toBe('http://127.0.0.1:3008/#session_token=target.jwt.token');
    });

    it('keeps the tenant-scoped token during a StrictMode auth bootstrap replay', () => {
        const platformToken = 'platform-token';
        const tenantToken = 'tenant-token';

        expect(resolveBootstrapToken(tenantToken, platformToken, platformToken)).toBe(tenantToken);
        expect(resolveBootstrapToken(null, tenantToken, platformToken)).toBe(tenantToken);
    });

    it('does not complete browser-session establishment before the cookie response', async () => {
        let resolveResponse!: (response: Pick<Response, 'ok' | 'status'>) => void;
        const response = new Promise<Pick<Response, 'ok' | 'status'>>((resolve) => {
            resolveResponse = resolve;
        });
        const fetchMock = vi.fn(() => response);
        vi.stubGlobal('fetch', fetchMock);

        let established = false;
        const pending = establishBrowserSession('header.payload.signature').then(() => {
            established = true;
        });
        await Promise.resolve();

        expect(established).toBe(false);
        resolveResponse({ ok: true, status: 204 });
        await pending;
        expect(established).toBe(true);
        expect(fetchMock).toHaveBeenCalledWith('/api/auth/browser-session', {
            method: 'POST',
            headers: { Authorization: 'Bearer header.payload.signature' },
            credentials: 'same-origin',
        });
    });

    it('fails closed when the browser-session cookie endpoint rejects the token', async () => {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401 }));

        await expect(establishBrowserSession('expired-token')).rejects.toThrow('HTTP 401');
    });
});
