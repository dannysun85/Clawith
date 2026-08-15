import type { User } from '../types';

export type ProductSurface = 'work' | 'company_admin' | 'platform_admin';

export const PRODUCT_SURFACE_PATHS: Record<ProductSurface, string> = {
    work: '/work',
    company_admin: '/company-admin',
    platform_admin: '/admin/platform',
};

export function hasProductSurface(
    user: Pick<User, 'available_surfaces'> | null | undefined,
    surface: ProductSurface,
): boolean {
    return Boolean(user?.available_surfaces?.includes(surface));
}

export function hasEffectiveCapability(
    user: Pick<User, 'effective_capabilities'> | null | undefined,
    capability: string,
): boolean {
    return Boolean(user?.effective_capabilities?.includes(capability));
}

export function isPlatformOperator(
    user: Pick<User, 'global_roles'> | null | undefined,
): boolean {
    return Boolean(user?.global_roles?.includes('platform_operator'));
}

export function isCompanyOwner(
    user: Pick<User, 'membership_role'> | null | undefined,
): boolean {
    return user?.membership_role === 'org_owner';
}

export function availablePrimarySurfaces(
    user: Pick<User, 'available_surfaces'> | null | undefined,
): ProductSurface[] {
    const surfaces = user?.available_surfaces || [];
    return (['work', 'platform_admin'] as ProductSurface[]).filter((surface) => (
        surfaces.includes(surface)
    ));
}

export function resolveProductEntry(
    user: Pick<User, 'available_surfaces' | 'effective_capabilities' | 'pending_invitation_count'> | null,
    preferredSurface?: ProductSurface | null,
): string {
    if (!user) return '/login';
    const primarySurfaces = availablePrimarySurfaces(user);
    if (preferredSurface && primarySurfaces.includes(preferredSurface)) {
        return PRODUCT_SURFACE_PATHS[preferredSurface];
    }
    if (primarySurfaces.length > 1) return '/choose-surface';
    if (primarySurfaces.length === 1) return PRODUCT_SURFACE_PATHS[primarySurfaces[0]];
    return '/setup-company';
}

export function productAccessSignature(
    user: Pick<
        User,
        'membership_id' | 'membership_role' | 'global_roles' | 'effective_capabilities' | 'available_surfaces'
    > | null,
): string {
    if (!user) return 'anonymous';
    return JSON.stringify({
        membership_id: user.membership_id || null,
        membership_role: user.membership_role || null,
        global_roles: [...(user.global_roles || [])].sort(),
        effective_capabilities: [...(user.effective_capabilities || [])].sort(),
        available_surfaces: [...(user.available_surfaces || [])].sort(),
    });
}
