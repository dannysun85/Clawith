import { describe, expect, it, vi } from 'vitest';

import { commitSameOriginTenantSwitch, validateCrossOriginTenantSwitch } from './tenantSwitch';

describe('commitSameOriginTenantSwitch', () => {
    it('does not commit tenant state when candidate-token validation fails', async () => {
        const establishAuth = vi.fn();
        const persistTenantId = vi.fn();
        const clearTenantId = vi.fn();

        await expect(commitSameOriginTenantSwitch({
            tenantId: 'target-tenant',
            accessToken: 'candidate-token',
            validateToken: async () => { throw new Error('candidate rejected'); },
            establishAuth,
            persistTenantId,
            clearTenantId,
            currentTenantId: () => 'current-tenant',
            resolvedTenantId: (user: { tenant_id?: string }) => user.tenant_id,
        })).rejects.toThrow('candidate rejected');

        expect(establishAuth).not.toHaveBeenCalled();
        expect(persistTenantId).not.toHaveBeenCalled();
        expect(clearTenantId).not.toHaveBeenCalled();
    });

    it('commits only after validation and auth establishment succeed', async () => {
        const calls: string[] = [];
        const user = { tenant_id: 'validated-tenant' };

        await commitSameOriginTenantSwitch({
            tenantId: 'validated-tenant',
            accessToken: 'candidate-token',
            validateToken: async () => {
                calls.push('validate');
                return user;
            },
            establishAuth: async () => { calls.push('auth'); },
            persistTenantId: (tenantId) => { calls.push(`persist:${tenantId}`); },
            clearTenantId: () => { calls.push('clear'); },
            currentTenantId: () => 'previous-tenant',
            resolvedTenantId: (value) => value.tenant_id,
        });

        expect(calls).toEqual(['validate', 'persist:validated-tenant', 'auth']);
    });

    it('rejects a valid token for a different tenant before committing state', async () => {
        const establishAuth = vi.fn();
        const persistTenantId = vi.fn();

        await expect(commitSameOriginTenantSwitch({
            tenantId: 'requested-tenant',
            accessToken: 'candidate-token',
            validateToken: async () => ({ tenant_id: 'different-tenant' }),
            establishAuth,
            persistTenantId,
            clearTenantId: vi.fn(),
            currentTenantId: () => 'previous-tenant',
            resolvedTenantId: (value) => value.tenant_id,
        })).rejects.toThrow('does not match');

        expect(establishAuth).not.toHaveBeenCalled();
        expect(persistTenantId).not.toHaveBeenCalled();
    });

    it('rolls the staged tenant identifier back when auth establishment fails', async () => {
        const persisted: string[] = [];

        await expect(commitSameOriginTenantSwitch({
            tenantId: 'target-tenant',
            accessToken: 'candidate-token',
            validateToken: async () => ({ tenant_id: 'target-tenant' }),
            establishAuth: async () => { throw new Error('cookie rejected'); },
            persistTenantId: (value) => { persisted.push(value); },
            clearTenantId: vi.fn(),
            currentTenantId: () => 'previous-tenant',
            resolvedTenantId: (value) => value.tenant_id,
        })).rejects.toThrow('cookie rejected');

        expect(persisted).toEqual(['target-tenant', 'previous-tenant']);
    });
});

describe('validateCrossOriginTenantSwitch', () => {
    it('requires token tenant, declared tenant, and current origin tenant to agree', async () => {
        await expect(validateCrossOriginTenantSwitch({
            tenantId: 'target-tenant',
            accessToken: 'candidate-token',
            validateToken: async () => ({ tenant_id: 'target-tenant' }),
            resolvedTenantId: (user) => user.tenant_id,
            resolveCurrentOriginTenant: async () => ({ id: 'target-tenant' }),
        })).resolves.toEqual({ tenant_id: 'target-tenant' });
    });

    it('rejects a valid tenant token on an unrelated browser origin', async () => {
        await expect(validateCrossOriginTenantSwitch({
            tenantId: 'target-tenant',
            accessToken: 'candidate-token',
            validateToken: async () => ({ tenant_id: 'target-tenant' }),
            resolvedTenantId: (user) => user.tenant_id,
            resolveCurrentOriginTenant: async () => ({ id: 'lookalike-tenant' }),
        })).rejects.toThrow('browser origin');
    });

    it('supports legacy fragments only when token tenant and exact origin still agree', async () => {
        await expect(validateCrossOriginTenantSwitch({
            tenantId: null,
            accessToken: 'legacy-candidate-token',
            validateToken: async () => ({ tenant_id: 'token-tenant' }),
            resolvedTenantId: (user) => user.tenant_id,
            resolveCurrentOriginTenant: async () => ({ id: 'token-tenant' }),
        })).resolves.toEqual({ tenant_id: 'token-tenant' });
    });
});
