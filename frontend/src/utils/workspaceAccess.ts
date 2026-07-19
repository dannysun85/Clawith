import type { User } from '../types';

type WorkspaceIdentity = Pick<User, 'id' | 'tenant_id' | 'role' | 'is_platform_admin'>;

export function authQueryScopeKey(user: WorkspaceIdentity | null): string {
    if (!user) return 'anonymous';
    return `${user.id}:${user.tenant_id || 'platform'}`;
}

export function tenantWorkspaceRedirect(user: WorkspaceIdentity | null): string | null {
    if (!user) return '/login';
    if (user.tenant_id) return null;
    if (user.role === 'platform_admin' || user.is_platform_admin) {
        return '/admin/platform-settings';
    }
    return '/setup-company';
}
