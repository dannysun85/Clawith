import { describe, expect, it } from 'vitest';

import {
    availablePrimarySurfaces,
    hasEffectiveCapability,
    hasProductSurface,
    isCompanyOwner,
    isPlatformOperator,
    productAccessSignature,
    resolveProductEntry,
} from './productAccess';

const access = (overrides: Record<string, unknown> = {}) => ({
    membership_id: 'membership-1',
    membership_role: 'member',
    global_roles: [],
    effective_capabilities: ['work.use'],
    available_surfaces: ['work'],
    pending_invitation_count: 0,
    ...overrides,
} as any);

describe('product access contract', () => {
    it('uses server surfaces and capabilities without inferring from the legacy role', () => {
        const legacyPlatformRole = access({
            role: 'platform_admin',
            available_surfaces: ['work'],
            global_roles: [],
        });

        expect(hasProductSurface(legacyPlatformRole, 'work')).toBe(true);
        expect(hasProductSurface(legacyPlatformRole, 'platform_admin')).toBe(false);
        expect(isPlatformOperator(legacyPlatformRole)).toBe(false);
        expect(hasEffectiveCapability(legacyPlatformRole, 'work.use')).toBe(true);
    });

    it('keeps owner, company admin, and platform authority independent', () => {
        const user = access({
            membership_role: 'org_owner',
            global_roles: ['platform_operator'],
            available_surfaces: ['work', 'company_admin', 'platform_admin'],
        });

        expect(isCompanyOwner(user)).toBe(true);
        expect(isPlatformOperator(user)).toBe(true);
        expect(availablePrimarySurfaces(user)).toEqual(['work', 'platform_admin']);
    });

    it('requires an explicit first choice when work and platform surfaces coexist', () => {
        const user = access({ available_surfaces: ['work', 'platform_admin'] });

        expect(resolveProductEntry(user)).toBe('/choose-surface');
        expect(resolveProductEntry(user, 'work')).toBe('/work');
        expect(resolveProductEntry(user, 'platform_admin')).toBe('/admin/platform');
    });

    it('sends identities without an active product surface to company access', () => {
        expect(resolveProductEntry(access({ available_surfaces: [] }))).toBe('/setup-company');
    });

    it('changes the access signature when hot permissions are revoked', () => {
        expect(productAccessSignature(access()))
            .not.toBe(productAccessSignature(access({ effective_capabilities: [] })));
    });
});
