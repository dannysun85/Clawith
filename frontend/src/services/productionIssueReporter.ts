type ClientIssueCategory = 'api' | 'runtime' | 'websocket';

type ClientIssueReport = {
    category: ClientIssueCategory;
    severity?: 'warning' | 'error';
    error_code: string;
    route?: string;
    operation?: string;
    agent_id?: string;
    metadata?: Record<string, string | number | boolean | null>;
};

const REPORT_URL = '/api/production-issues/client-report';
const REPORT_DEDUPE_WINDOW_MS = 30_000;
const MAX_RECENT_FINGERPRINTS = 200;
const recentReports = new Map<string, number>();
let reportingInstalled = false;
let pageLifecycleEnding = false;

const routeWithoutQuery = (value: string) => value.split('?', 1)[0].slice(0, 500);

const reportFingerprint = (report: ClientIssueReport) => [
    report.category,
    report.error_code,
    routeWithoutQuery(report.route || ''),
    report.operation || '',
    report.agent_id || '',
].join('|');

export function shouldReportWebSocketClose(code: number, intentional: boolean): boolean {
    return !intentional && ![1000, 1001, 4002, 4003].includes(code);
}

function isDuplicateReport(report: ClientIssueReport, now = Date.now()): boolean {
    const fingerprint = reportFingerprint(report);
    const lastReportedAt = recentReports.get(fingerprint);
    if (lastReportedAt !== undefined && now - lastReportedAt < REPORT_DEDUPE_WINDOW_MS) {
        return true;
    }
    recentReports.set(fingerprint, now);
    if (recentReports.size > MAX_RECENT_FINGERPRINTS) {
        const cutoff = now - REPORT_DEDUPE_WINDOW_MS;
        for (const [key, timestamp] of recentReports) {
            if (timestamp < cutoff || recentReports.size > MAX_RECENT_FINGERPRINTS) {
                recentReports.delete(key);
            }
        }
    }
    return false;
}

function isPageLifecycleFetchTeardown(report: ClientIssueReport): boolean {
    return pageLifecycleEnding
        && report.category === 'api'
        && report.error_code === 'TypeError'
        && report.metadata?.component === 'fetch'
        && report.metadata?.status_code == null;
}

export function reportClientIssue(report: ClientIssueReport): void {
    const token = localStorage.getItem('token');
    if (
        isPageLifecycleFetchTeardown(report)
        || !token
        || routeWithoutQuery(report.route || '') === REPORT_URL
        || isDuplicateReport(report)
    ) return;
    const payload = {
        ...report,
        route: report.route ? routeWithoutQuery(report.route) : undefined,
    };
    void fetch(REPORT_URL, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(payload),
        keepalive: true,
    }).catch(() => undefined);
}

export function installClientIssueReporting(): void {
    if (reportingInstalled) return;
    reportingInstalled = true;

    // A full-page navigation tears down in-flight fetches even when the API
    // already returned 200. Browsers surface that teardown as a TypeError, and
    // keepalive would otherwise turn it into a false production incident. SPA
    // route changes do not emit pagehide, so genuine network failures remain
    // reportable. pageshow resets the flag for bfcache restores.
    window.addEventListener('pagehide', () => {
        pageLifecycleEnding = true;
    });
    window.addEventListener('pageshow', () => {
        pageLifecycleEnding = false;
    });

    window.addEventListener('error', (event) => {
        const file = event.filename ? event.filename.split('/').pop() : undefined;
        reportClientIssue({
            category: 'runtime',
            error_code: event.error?.name || 'WindowError',
            route: window.location.pathname,
            operation: 'render',
            metadata: {
                component: 'window',
                ...(file ? { file } : {}),
                line: event.lineno || 0,
                column: event.colno || 0,
            },
        });
    });
    window.addEventListener('unhandledrejection', (event) => {
        const reason = event.reason;
        reportClientIssue({
            category: 'runtime',
            error_code: reason instanceof Error ? reason.name : 'UnhandledRejection',
            route: window.location.pathname,
            operation: 'promise',
            metadata: { component: 'window' },
        });
    });
}
