import { describe, expect, it } from 'vitest';

import { summarizeCredentialQuota } from './credentialQuotaStatus';

describe('summarizeCredentialQuota', () => {
    it('treats the shared plan circuit as a global credential block', () => {
        expect(summarizeCredentialQuota({
            plan: { status: 'quota_exceeded' },
            'video:minimax-hailuo-02': {
                status: 'quota_exceeded',
                model: 'MiniMax-Hailuo-02',
            },
        })).toEqual({
            blockedLabels: ['plan', 'video (MiniMax-Hailuo-02)'],
            sharedPlanBlocked: true,
            unsupportedModelLabels: [],
        });
    });

    it('keeps a model-scoped media circuit local', () => {
        expect(summarizeCredentialQuota({
            'video:minimax-hailuo-02': {
                status: 'quota_exceeded',
                model: 'MiniMax-Hailuo-02',
            },
        }).sharedPlanBlocked).toBe(false);
    });

    it('distinguishes an unentitled model from exhausted provider quota', () => {
        expect(summarizeCredentialQuota({
            'video:doubao-seedance-2.0': {
                status: 'quota_exceeded',
                model: 'doubao-seedance-2.0',
                error_code: 'UnsupportedModel',
            },
        })).toEqual({
            blockedLabels: ['video (doubao-seedance-2.0)'],
            sharedPlanBlocked: false,
            unsupportedModelLabels: ['video (doubao-seedance-2.0)'],
        });
    });
});
