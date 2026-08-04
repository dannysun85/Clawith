import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('API candidate tenant token boundary', () => {
    const values = new Map<string, string>();
    const removeItem = vi.fn((key: string) => values.delete(key));

    beforeEach(() => {
        vi.resetModules();
        values.clear();
        removeItem.mockClear();
        values.set('token', 'current-platform-token');
        values.set('user', JSON.stringify({ id: 'platform-admin' }));
        values.set('current_tenant_id', 'platform');
        vi.stubGlobal('localStorage', {
            getItem: (key: string) => values.get(key) ?? null,
            setItem: (key: string, value: string) => values.set(key, value),
            removeItem,
            clear: () => values.clear(),
        });
        vi.stubGlobal('window', { location: { href: '/admin/companies' } });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('validates with the candidate token without clearing the current session on 401', async () => {
        const fetchMock = vi.fn().mockResolvedValue(
            new Response(JSON.stringify({ detail: 'candidate token rejected' }), {
                status: 401,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
        vi.stubGlobal('fetch', fetchMock);
        const { authApi } = await import('./api');

        await expect(authApi.me('candidate-tenant-token')).rejects.toBeDefined();

        const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
        expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/me');
        expect(new Headers(options.headers).get('Authorization')).toBe(
            'Bearer candidate-tenant-token',
        );
        expect(removeItem).not.toHaveBeenCalled();
        expect(localStorage.getItem('token')).toBe('current-platform-token');
        expect(localStorage.getItem('user')).toContain('platform-admin');
        expect(window.location.href).toBe('/admin/companies');
    });
});
