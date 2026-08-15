import type { User } from '../types';
import { hasProductSurface, productAccessSignature } from './productAccess';

type WorkspaceIdentity = Pick<
    User,
    | 'id'
    | 'tenant_id'
    | 'membership_id'
    | 'membership_role'
    | 'global_roles'
    | 'effective_capabilities'
    | 'available_surfaces'
>;

export function authQueryScopeKey(user: WorkspaceIdentity | null): string {
    if (!user) return 'anonymous';
    return `${user.id}:${user.tenant_id || 'no-membership'}:${productAccessSignature(user)}`;
}

export function tenantWorkspaceRedirect(user: WorkspaceIdentity | null): string | null {
    if (!user) return '/login';
    if (user.tenant_id && hasProductSurface(user, 'work')) return null;
    if (hasProductSurface(user, 'platform_admin')) return '/admin/platform';
    return '/setup-company';
}
