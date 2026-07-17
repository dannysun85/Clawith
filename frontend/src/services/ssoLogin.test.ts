import { describe, expect, it, vi } from 'vitest';

import {
    createTenantSsoAuthorization,
    loadTenantSsoProviders,
} from './ssoLogin';

describe('tenant SSO initiation', () => {
    it('loads public provider metadata without creating a relay session', async () => {
        const fetcher = vi.fn(async () => ([
            { provider_type: 'google_workspace', name: 'Google Workspace' },
        ]));

        const providers = await loadTenantSsoProviders('tenant/id', fetcher);

        expect(providers).toHaveLength(1);
        expect(fetcher).toHaveBeenCalledOnce();
        expect(fetcher).toHaveBeenCalledWith('/sso/providers?tenant_id=tenant%2Fid');
    });

    it('allocates a relay session only when a concrete provider is selected', async () => {
        const responses: unknown[] = [
            { session_id: 'session/id' },
            [
                {
                    provider_type: 'google_workspace',
                    name: 'Google Workspace',
                    url: 'https://accounts.example/authorize',
                },
            ],
        ];
        const fetcher = vi.fn(async () => responses.shift());

        const url = await createTenantSsoAuthorization(
            'tenant/id',
            'google_workspace',
            fetcher,
        );

        expect(url).toBe('https://accounts.example/authorize');
        expect(fetcher.mock.calls).toEqual([
            ['/sso/session?tenant_id=tenant%2Fid', { method: 'POST' }],
            ['/sso/config?sid=session%2Fid'],
        ]);
    });
});
