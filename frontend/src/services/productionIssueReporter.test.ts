import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { reportClientIssue } from './productionIssueReporter';


describe('production issue reporter', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.setSystemTime(new Date('2026-07-13T06:00:00Z'));
        const values = new Map<string, string>();
        vi.stubGlobal('localStorage', {
            getItem: (key: string) => values.get(key) ?? null,
            setItem: (key: string, value: string) => values.set(key, value),
            removeItem: (key: string) => values.delete(key),
            clear: () => values.clear(),
        });
        localStorage.clear();
        localStorage.setItem('token', 'header.payload.signature');
    });

    afterEach(() => {
        localStorage.clear();
        vi.unstubAllGlobals();
        vi.useRealTimers();
    });

    it('deduplicates an error storm and strips query parameters', () => {
        const fetchMock = vi.fn().mockResolvedValue({ ok: true });
        vi.stubGlobal('fetch', fetchMock);
        const report = {
            category: 'api' as const,
            error_code: 'http_503',
            route: '/api/agents/123?token=must-not-survive',
            operation: 'GET',
            metadata: { status_code: 503, component: 'fetch' },
        };

        reportClientIssue(report);
        reportClientIssue(report);

        expect(fetchMock).toHaveBeenCalledTimes(1);
        const request = fetchMock.mock.calls[0];
        expect(request[0]).toBe('/api/production-issues/client-report');
        expect(JSON.parse(String(request[1]?.body))).toEqual({
            ...report,
            route: '/api/agents/123',
        });

        vi.advanceTimersByTime(30_000);
        reportClientIssue(report);
        expect(fetchMock).toHaveBeenCalledTimes(2);
    });
});
