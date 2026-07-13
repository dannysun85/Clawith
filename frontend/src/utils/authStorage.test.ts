import { describe, expect, it, vi } from 'vitest';

import { clearAuthStorage } from './authStorage';

describe('clearAuthStorage', () => {
    it('clears identity and tenant scoped state on logout', () => {
        const removeItem = vi.fn();

        clearAuthStorage({ removeItem });

        expect(removeItem.mock.calls.map(([key]) => key)).toEqual([
            'token',
            'user',
            'current_tenant_id',
            'pinned_agents',
        ]);
    });
});
