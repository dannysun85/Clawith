type ClientIssueCategory = 'api' | 'runtime' | 'websocket';
type ClientIssueSignalKind = 'fetch_rejected' | 'http_response' | 'runtime_exception' | 'websocket_close';

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
const SAFE_ORIGIN_HOST = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
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
    // 1005/1006 are browser-local "no close frame" signals commonly emitted
    // during sleep, network switching, navigation, or tab teardown. The
    // socket's onerror path still reports genuine connection failures.
    return !intentional && ![1000, 1001, 1005, 1006, 4002, 4003].includes(code);
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

function signalKind(report: ClientIssueReport): ClientIssueSignalKind {
    if (report.category === 'runtime') return 'runtime_exception';
    if (report.category === 'websocket') return 'websocket_close';
    return report.metadata?.status_code == null ? 'fetch_rejected' : 'http_response';
}

function browserDiagnosticContext(report: ClientIssueReport): Record<string, string | boolean> {
    const rawHost = typeof window !== 'undefined' ? window.location?.host : undefined;
    const originHost = rawHost
        && rawHost.length <= 120
        && SAFE_ORIGIN_HOST.test(rawHost)
        ? rawHost
        : undefined;
    const rawVisibility = typeof document !== 'undefined' ? document.visibilityState : undefined;
    const visibilityState = rawVisibility === 'visible'
        || rawVisibility === 'hidden'
        || rawVisibility === 'prerender'
        ? rawVisibility
        : 'unknown';
    const releaseVersion = typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : 'unknown';

    return {
        ...(originHost ? { origin_host: originHost } : {}),
        visibility_state: visibilityState,
        lifecycle_state: pageLifecycleEnding ? 'ending' : 'active',
        online: typeof navigator !== 'undefined' ? navigator.onLine : true,
        signal_kind: signalKind(report),
        release_version: releaseVersion,
    };
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
        metadata: {
            ...(report.metadata || {}),
            ...browserDiagnosticContext(report),
        },
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
