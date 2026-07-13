import type { User } from '../types';

const DEFAULT_SAAS_ADMIN_EMAIL = 'admin@reeftotem.ai';

export function getSaasAdminEmail() {
    return (import.meta.env.VITE_SAAS_ADMIN_EMAIL || DEFAULT_SAAS_ADMIN_EMAIL).trim().toLowerCase();
}

export function canAccessSaasAdmin(user?: Pick<User, 'email' | 'role' | 'is_platform_admin'> | null) {
    if (!user) return false;
    const isPlatformAdmin = user.role === 'platform_admin' || !!user.is_platform_admin;
    return isPlatformAdmin && (user.email || '').trim().toLowerCase() === getSaasAdminEmail();
}
