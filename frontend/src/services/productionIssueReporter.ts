type ClientIssueCategory = 'api' | 'runtime' | 'websocket';

type ClientIssueReport = {
    category: ClientIssueCategory;
    severity?: 'warning' | 'error';
    error_code: string;
    route?: string;
    operation?: string;
    metadata?: Record<string, string | number | boolean | null>;
};

const REPORT_URL = '/api/production-issues/client-report';
const REPORT_DEDUPE_WINDOW_MS = 30_000;
const MAX_RECENT_FINGERPRINTS = 200;
const recentReports = new Map<string, number>();

const routeWithoutQuery = (value: string) => value.split('?', 1)[0].slice(0, 500);

const reportFingerprint = (report: ClientIssueReport) => [
    report.category,
    report.error_code,
    routeWithoutQuery(report.route || ''),
    report.operation || '',
].join('|');

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

export function reportClientIssue(report: ClientIssueReport): void {
    const token = localStorage.getItem('token');
    if (
        !token
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
