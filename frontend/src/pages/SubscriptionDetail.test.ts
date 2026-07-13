import { describe, expect, test } from 'vitest';

import { transactionActionLabel } from './SubscriptionDetail';


describe('subscription incident refunds', () => {
    test('uses an explicit incident label instead of a raw refund code', () => {
        expect(transactionActionLabel({
            id: 'tx-1',
            delta: 490,
            balance_after: 1490,
            reason: 'refund',
            ref_type: 'product_incident',
            created_at: '2026-07-13T00:00:00Z',
        })).toBe('事故退款');
    });
});
