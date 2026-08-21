import type { User } from '../types';

export function canAccessSaasAdmin(user?: Pick<User, 'global_roles' | 'effective_capabilities'> | null) {
    if (!user) return false;
    return Boolean(user.global_roles?.includes('platform_operator')
        && user.effective_capabilities?.includes('platform.billing.manage')
    );
}
