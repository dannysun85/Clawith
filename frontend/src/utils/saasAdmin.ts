import type { User } from '../types';

const DEFAULT_SAAS_ADMIN_EMAIL = 'admin@reeftotem.ai';

export function getSaasAdminEmail() {
    return (import.meta.env.VITE_SAAS_ADMIN_EMAIL || DEFAULT_SAAS_ADMIN_EMAIL).trim().toLowerCase();
}

export function canAccessSaasAdmin(user?: Pick<User, 'email' | 'global_roles' | 'effective_capabilities'> | null) {
    if (!user) return false;
    const hasPlatformBilling = user.global_roles?.includes('platform_operator')
        && user.effective_capabilities?.includes('platform.billing.manage');
    return Boolean(hasPlatformBilling)
        && (user.email || '').trim().toLowerCase() === getSaasAdminEmail();
}
