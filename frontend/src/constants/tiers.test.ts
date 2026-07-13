import { describe, expect, it } from 'vitest';

import { resolveAllowedTier } from './tiers';

describe('SaaS tier selection', () => {
    it('clamps a legacy preference to the first tier allowed by the active plan', () => {
        expect(resolveAllowedTier('pro', ['lite'])).toBe('lite');
    });

    it('preserves an allowed explicit preference', () => {
        expect(resolveAllowedTier('pro', ['lite', 'pro'])).toBe('pro');
    });

    it('returns no usable tier when a plan contains only unsupported legacy values', () => {
        expect(resolveAllowedTier('pro', ['standard'])).toBeNull();
    });

    it('keeps the legacy fallback for tenants without tier restrictions', () => {
        expect(resolveAllowedTier(undefined, [])).toBe('pro');
    });
});
