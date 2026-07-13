import { describe, expect, it } from 'vitest';
import {
    buildWorkspaceDownloadUrl,
    consumeSessionTokenFromUrl,
    websocketAuthProtocols,
} from './authTransport';

describe('credential-safe browser transports', () => {
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
});
