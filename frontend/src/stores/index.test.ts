import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('auth store browser-session gate', () => {
    const values = new Map<string, string>();

    beforeEach(() => {
        vi.resetModules();
        values.clear();
        vi.stubGlobal('localStorage', {
            getItem: (key: string) => values.get(key) ?? null,
            setItem: (key: string, value: string) => values.set(key, value),
            removeItem: (key: string) => values.delete(key),
            clear: () => values.clear(),
        });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('keeps authenticated UI state hidden until the media cookie is confirmed', async () => {
        let resolveResponse!: (response: Pick<Response, 'ok' | 'status'>) => void;
        vi.stubGlobal(
            'fetch',
            vi.fn(
                () => new Promise<Pick<Response, 'ok' | 'status'>>((resolve) => {
                    resolveResponse = resolve;
                }),
            ),
        );
        const { useAuthStore } = await import('./index');
        const user = { id: 'user-id', role: 'member', tenant_id: 'tenant-id' } as any;

        const pending = useAuthStore.getState().setAuth(user, 'new-token');
        await Promise.resolve();

        expect(useAuthStore.getState().user).toBeNull();
        expect(localStorage.getItem('token')).toBeNull();

        resolveResponse({ ok: true, status: 204 });
        await pending;

        expect(useAuthStore.getState().user).toBe(user);
        expect(useAuthStore.getState().token).toBe('new-token');
        expect(localStorage.getItem('token')).toBe('new-token');
    });
});
