import { describe, expect, it } from 'vitest';

import { authQueryScopeKey, tenantWorkspaceRedirect } from './workspaceAccess';

const user = (overrides: Record<string, unknown> = {}) => ({
    id: 'user-1',
    tenant_id: 'tenant-1',
    membership_id: 'user-1',
    membership_role: 'member',
    global_roles: [],
    effective_capabilities: ['work.use'],
    available_surfaces: ['work'],
    ...overrides,
} as any);

describe('tenantWorkspaceRedirect', () => {
    it('allows a tenant-scoped membership into the workspace', () => {
        expect(tenantWorkspaceRedirect(user())).toBeNull();
    });

    it('keeps a global platform identity in the platform console', () => {
        expect(tenantWorkspaceRedirect(user({
            tenant_id: null,
            membership_id: null,
            membership_role: null,
            global_roles: ['platform_operator'],
            effective_capabilities: ['platform.tenants.manage'],
            available_surfaces: ['platform_admin'],
        }))).toBe('/admin/platform');
    });

    it('sends a tenantless non-platform identity to company setup', () => {
        expect(tenantWorkspaceRedirect(user({ tenant_id: null, membership_id: null, membership_role: null, available_surfaces: [] }))).toBe('/setup-company');
    });
});

describe('authQueryScopeKey', () => {
    it('changes between global and tenant-scoped memberships of one identity', () => {
        expect(authQueryScopeKey(user({ id: 'membership-user', tenant_id: null })))
            .not.toBe(authQueryScopeKey(user({ id: 'membership-user', tenant_id: 'tenant-1' })));
    });
});
