import { describe, expect, it } from 'vitest';

import { normalizeApprovals } from './ApprovalsTab';

describe('normalizeApprovals', () => {
    it('preserves the current list response', () => {
        const approvals = [{ id: 'approval-1' }];
        expect(normalizeApprovals(approvals)).toBe(approvals);
    });

    it('accepts a paginated items response', () => {
        const approvals = [{ id: 'approval-1' }];
        expect(normalizeApprovals({ items: approvals })).toBe(approvals);
    });

    it.each([undefined, null, {}, { items: null }, 'unauthorized'])(
        'returns an empty list for malformed data: %s',
        (value) => {
            expect(normalizeApprovals(value)).toEqual([]);
        },
    );
});
