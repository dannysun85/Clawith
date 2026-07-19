import { describe, expect, it } from 'vitest';

import { authQueryScopeKey, tenantWorkspaceRedirect } from './workspaceAccess';

const user = (overrides: Record<string, unknown> = {}) => ({
    id: 'user-1',
    tenant_id: 'tenant-1',
    role: 'member',
    is_platform_admin: false,
    ...overrides,
} as any);

describe('tenantWorkspaceRedirect', () => {
    it('allows a tenant-scoped membership into the workspace', () => {
        expect(tenantWorkspaceRedirect(user())).toBeNull();
    });

    it('keeps a global platform identity in the platform console', () => {
        expect(tenantWorkspaceRedirect(user({
            tenant_id: null,
            role: 'platform_admin',
            is_platform_admin: true,
        }))).toBe('/admin/platform-settings');
    });

    it('sends a tenantless non-platform identity to company setup', () => {
        expect(tenantWorkspaceRedirect(user({ tenant_id: null }))).toBe('/setup-company');
    });
});

describe('authQueryScopeKey', () => {
    it('changes between global and tenant-scoped memberships of one identity', () => {
        expect(authQueryScopeKey(user({ id: 'membership-user', tenant_id: null })))
            .not.toBe(authQueryScopeKey(user({ id: 'membership-user', tenant_id: 'tenant-1' })));
    });
});
