import { describe, expect, it } from 'vitest';
import {
    AuthBootstrapTimeoutError,
    isDefinitiveAuthRejection,
    isTransientAuthBootstrapFailure,
    withAuthBootstrapTimeout,
} from './authBootstrapRecovery';

describe('auth bootstrap recovery', () => {
    it('classifies only invalid authorization as definitive rejection', () => {
        expect(isDefinitiveAuthRejection({ status: 401 })).toBe(true);
        expect(isDefinitiveAuthRejection({ status: 403 })).toBe(true);
        expect(isDefinitiveAuthRejection({ status: 502 })).toBe(false);
        expect(isDefinitiveAuthRejection(new TypeError('Failed to fetch'))).toBe(false);
    });

    it('keeps network, timeout, and upstream failures retryable', () => {
        expect(isTransientAuthBootstrapFailure(new AuthBootstrapTimeoutError())).toBe(true);
        expect(isTransientAuthBootstrapFailure(new TypeError('Failed to fetch'))).toBe(true);
        expect(isTransientAuthBootstrapFailure({ status: 503 })).toBe(true);
        expect(isTransientAuthBootstrapFailure({ retryable: true })).toBe(true);
        expect(isTransientAuthBootstrapFailure(new Error('Tenant mismatch'))).toBe(false);
    });

    it('aborts a bootstrap request that exceeds the deadline', async () => {
        let aborted = false;
        await expect(withAuthBootstrapTimeout(
            (signal) => new Promise<never>((_resolve, reject) => {
                signal.addEventListener('abort', () => {
                    aborted = true;
                    reject(new DOMException('Aborted', 'AbortError'));
                });
            }),
            5,
        )).rejects.toBeInstanceOf(AuthBootstrapTimeoutError);
        expect(aborted).toBe(true);
    });
});
