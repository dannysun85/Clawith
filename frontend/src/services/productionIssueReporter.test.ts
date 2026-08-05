import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
    installClientIssueReporting,
    reportClientIssue,
    shouldReportWebSocketClose,
} from './productionIssueReporter';

const windowEvents = new EventTarget();

describe('production issue reporter', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.setSystemTime(new Date('2026-07-13T06:00:00Z'));
        vi.stubGlobal('window', windowEvents);
        const values = new Map<string, string>();
        vi.stubGlobal('localStorage', {
            getItem: (key: string) => values.get(key) ?? null,
            setItem: (key: string, value: string) => values.set(key, value),
            removeItem: (key: string) => values.delete(key),
            clear: () => values.clear(),
        });
        localStorage.clear();
        localStorage.setItem('token', 'header.payload.signature');
        window.dispatchEvent(new Event('pageshow'));
    });

    afterEach(() => {
        localStorage.clear();
        window.dispatchEvent(new Event('pageshow'));
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
            agent_id: '123',
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

    it('keeps Agent occurrences distinct inside the client dedupe window', () => {
        const fetchMock = vi.fn().mockResolvedValue({ ok: true });
        vi.stubGlobal('fetch', fetchMock);

        reportClientIssue({
            category: 'websocket',
            error_code: 'close_1006',
            route: '/ws/chat/{agent_id}',
            operation: 'chat',
            agent_id: 'agent-a',
        });
        reportClientIssue({
            category: 'websocket',
            error_code: 'close_1006',
            route: '/ws/chat/{agent_id}',
            operation: 'chat',
            agent_id: 'agent-b',
        });

        expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('does not classify intentional socket shutdown as a product error', () => {
        expect(shouldReportWebSocketClose(1005, true)).toBe(false);
        expect(shouldReportWebSocketClose(1006, true)).toBe(false);
        expect(shouldReportWebSocketClose(1000, false)).toBe(false);
        expect(shouldReportWebSocketClose(1001, false)).toBe(false);
        expect(shouldReportWebSocketClose(4002, false)).toBe(false);
        expect(shouldReportWebSocketClose(4003, false)).toBe(false);
        expect(shouldReportWebSocketClose(1005, false)).toBe(false);
        expect(shouldReportWebSocketClose(1006, false)).toBe(false);
    });

    it('suppresses page-teardown failures but resumes reporting after bfcache restore', () => {
        const fetchMock = vi.fn().mockResolvedValue({ ok: true });
        vi.stubGlobal('fetch', fetchMock);
        const addEventListenerSpy = vi.spyOn(window, 'addEventListener');
        installClientIssueReporting();
        const installedListenerCount = addEventListenerSpy.mock.calls.length;
        installClientIssueReporting();
        expect(addEventListenerSpy).toHaveBeenCalledTimes(installedListenerCount);
        const report = {
            category: 'api' as const,
            error_code: 'TypeError',
            route: '/api/groups/123/sessions',
            operation: 'GET',
            metadata: { component: 'fetch' },
        };

        window.dispatchEvent(new Event('pagehide'));
        reportClientIssue(report);
        expect(fetchMock).not.toHaveBeenCalled();

        reportClientIssue({
            ...report,
            error_code: 'http_503',
            metadata: { component: 'fetch', status_code: 503 },
        });
        reportClientIssue({
            category: 'websocket',
            error_code: 'close_1006',
            route: '/ws/chat/agent-123',
            operation: 'chat',
            metadata: { component: 'AgentDetailPage', close_code: 1006 },
        });
        reportClientIssue({
            category: 'runtime',
            error_code: 'WindowError',
            route: '/groups/123',
            operation: 'render',
            metadata: { component: 'window' },
        });
        expect(fetchMock).toHaveBeenCalledTimes(3);

        window.dispatchEvent(new Event('pageshow'));
        reportClientIssue(report);
        expect(fetchMock).toHaveBeenCalledTimes(4);
    });
});
