import { Routes, Route, Navigate, useLocation } from 'react-router';
import { useAuthStore } from './stores';
import { Suspense, lazy, useEffect, useLayoutEffect, useState, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { authApi, tenantApi } from './services/api';
import {
    consumeTenantSwitchSessionFromUrl,
    resolveBootstrapToken,
} from './utils/authTransport';
import { validateCrossOriginTenantSwitch } from './utils/tenantSwitch';
import { useQueryClient } from '@tanstack/react-query';
import { authQueryScopeKey, tenantWorkspaceRedirect } from './utils/workspaceAccess';
import {
    hasProductSurface,
    resolveProductEntry,
    type ProductSurface,
} from './utils/productAccess';
import {
    isDefinitiveAuthRejection,
    isTransientAuthBootstrapFailure,
    withAuthBootstrapTimeout,
} from './utils/authBootstrapRecovery';

// React StrictMode remounts the auth bootstrap in development.  Keep a
// consumed cross-origin candidate in memory until one non-cancelled pass has
// validated and committed it; never place an unvalidated JWT in identity
// storage merely to survive that replay.
let pendingCrossOriginSession: {
    token: string;
    targetTenantId: string | null;
} | null = null;

const Login = lazy(() => import('./pages/Login'));
const ForgotPassword = lazy(() => import('./pages/ForgotPassword'));
const ResetPassword = lazy(() => import('./pages/ResetPassword'));
const VerifyEmail = lazy(() => import('./pages/VerifyEmail'));
const CompanyAccess = lazy(() => import('./pages/CompanyAccess'));
const AccountCompanies = lazy(() => import('./pages/AccountCompanies'));
const AccountSecurity = lazy(() => import('./pages/AccountSecurity'));
const SurfaceChooser = lazy(() => import('./pages/SurfaceChooser'));
const Onboarding = lazy(() => import('./pages/Onboarding'));
const Layout = lazy(() => import('./pages/Layout'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const Employees = lazy(() => import('./pages/Employees'));
const Work = lazy(() => import('./pages/Work'));
const WorkDetail = lazy(() => import('./pages/WorkDetail'));
const Plaza = lazy(() => import('./pages/Plaza'));
const AgentDetail = lazy(() => import('./pages/AgentDetail'));
const AgentCreate = lazy(() => import('./pages/AgentCreate'));
const Messages = lazy(() => import('./pages/Messages'));
const CompanyAdmin = lazy(() => import('./pages/CompanyAdmin'));
const PlatformOperations = lazy(() => import('./pages/PlatformOperations'));
const SubscriptionDetail = lazy(() => import('./pages/SubscriptionDetail'));
const BillingSuccess = lazy(() => import('./pages/BillingSuccess'));
const OAuthCallback = lazy(() => import('./pages/OAuthCallback'));
const SSOEntry = lazy(() => import('./pages/SSOEntry'));
const OKR = lazy(() => import('./pages/OKR'));
const GroupsPage = lazy(() => import('./pages/groups/GroupsPage'));
const QualityReview = lazy(() => import('./pages/QualityReview'));

function ProtectedRoute({ children }: { children: React.ReactNode }) {
    const token = useAuthStore((s) => s.token);
    const user = useAuthStore((s) => s.user);
    if (!token) return <Navigate to="/login" replace />;
    // Force email verification if not active/verified
    if (user && !user.is_active) return <Navigate to="/verify-email" state={{ email: user.email }} replace />;
    
    return <>{children}</>;
}

function TenantWorkspaceRoute({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    const redirect = tenantWorkspaceRedirect(user);
    if (redirect) return <Navigate to={redirect} replace />;
    return <>{children}</>;
}

function AuthQueryScopeReset() {
    const user = useAuthStore((s) => s.user);
    const queryClient = useQueryClient();
    const scopeKey = authQueryScopeKey(user);
    const previousScopeRef = useRef(scopeKey);

    useLayoutEffect(() => {
        if (previousScopeRef.current === scopeKey) return;
        queryClient.clear();
        previousScopeRef.current = scopeKey;
    }, [queryClient, scopeKey]);

    return null;
}

function CompanyAdminRoute({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!hasProductSurface(user, 'company_admin')) return <Navigate to={resolveProductEntry(user)} replace />;
    return <>{children}</>;
}

function PlatformAdminRoute({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!hasProductSurface(user, 'platform_admin')) return <Navigate to={resolveProductEntry(user)} replace />;
    return <>{children}</>;
}

function ProductEntryRoute() {
    const user = useAuthStore((state) => state.user);
    const storedPreference = localStorage.getItem('preferred_product_surface');
    const preferredSurface = (
        storedPreference === 'work' || storedPreference === 'platform_admin'
            ? storedPreference
            : null
    ) as ProductSurface | null;
    return <Navigate to={resolveProductEntry(user, preferredSurface)} replace />;
}

function LegacyCompanyAdminRedirect() {
    const location = useLocation();
    const hash = location.hash.replace('#', '');
    const sectionByHash: Record<string, string> = {
        users: 'members', invites: 'members', approvals: 'approvals', audit: 'audit',
        subscription: 'market', info: 'settings', quotas: 'settings', org: 'integrations',
        tools: 'integrations', skills: 'integrations', douyin: 'integrations', okr: 'settings/okr',
    };
    return <Navigate to={`/company-admin/${sectionByHash[hash] || 'settings'}`} replace />;
}

function LegacyPlatformRedirect() {
    const location = useLocation();
    const tab = new URLSearchParams(location.search).get('tab') || '';
    const section = tab === 'accounts'
        ? 'providers'
        : tab === 'model-routes' || tab === 'media-routes'
            ? 'routes'
            : tab === 'production-issues'
                ? 'health'
                : tab === 'registration-codes'
                    ? 'registration'
                    : 'billing';
    return <Navigate to={`/admin/platform/${section}${location.search}`} replace />;
}

function AuthAccessRefresh() {
    const token = useAuthStore((state) => state.token);
    const user = useAuthStore((state) => state.user);
    const setUser = useAuthStore((state) => state.setUser);

    useEffect(() => {
        if (!token || !user) return;
        let cancelled = false;
        let refreshing = false;
        const refresh = async () => {
            if (refreshing || cancelled) return;
            refreshing = true;
            try {
                const nextUser = await authApi.me();
                if (!cancelled) setUser(nextUser);
            } catch {
                // The request layer owns 401 session cleanup. Transient network
                // failures keep the last confirmed UI until the next refresh.
            } finally {
                refreshing = false;
            }
        };
        const onFocus = () => void refresh();
        const onVisibility = () => {
            if (document.visibilityState === 'visible') void refresh();
        };
        const interval = window.setInterval(() => void refresh(), 15_000);
        window.addEventListener('focus', onFocus);
        document.addEventListener('visibilitychange', onVisibility);
        return () => {
            cancelled = true;
            window.clearInterval(interval);
            window.removeEventListener('focus', onFocus);
            document.removeEventListener('visibilitychange', onVisibility);
        };
    }, [setUser, token, user?.id]);

    return null;
}

/* ─── Notification Bar ─── */
type NotificationBarConfig = { enabled: boolean; text: string; updated_at?: string | null };
type NotificationBarUpdateEvent = CustomEvent<NotificationBarConfig>;

const notificationBarClass = 'has-notification-bar';
const notificationBarRevisionKey = (config: Pick<NotificationBarConfig, 'text' | 'updated_at'>) =>
    btoa(encodeURIComponent(`${config.text}::${config.updated_at || ''}`));
const notificationBarSessionDismissKey = (config: Pick<NotificationBarConfig, 'text' | 'updated_at'>) =>
    `notification_bar_dismissed_session_${notificationBarRevisionKey(config)}`;
const notificationBarPersistentDismissKey = (config: Pick<NotificationBarConfig, 'text' | 'updated_at'>) =>
    `notification_bar_dismissed_persistent_${notificationBarRevisionKey(config)}`;

function NotificationBar() {
    const { i18n } = useTranslation();
    const isChinese = i18n.language?.startsWith('zh');
    const [config, setConfig] = useState<NotificationBarConfig | null>(null);
    const [dismissed, setDismissed] = useState(false);
    const [showDismissMenu, setShowDismissMenu] = useState(false);
    
    const textRef = useRef<HTMLSpanElement>(null);
    const containerRef = useRef<HTMLDivElement>(null);
    const dismissMenuRef = useRef<HTMLDivElement>(null);
    const [isMarquee, setIsMarquee] = useState(false);

    useEffect(() => {
        fetch('/api/enterprise/system-settings/notification_bar/public')
            .then(r => r.ok ? r.json() : null)
            .then(d => { if (d) setConfig(d); })
            .catch(() => { });
    }, []);

    useEffect(() => {
        const handleUpdate = (event: Event) => {
            const next = (event as NotificationBarUpdateEvent).detail;
            if (!next) return;
            setConfig(next);
            setShowDismissMenu(false);
            if (next.text) {
                const persistentKey = notificationBarPersistentDismissKey(next);
                const sessionKey = notificationBarSessionDismissKey(next);
                setDismissed(!!localStorage.getItem(persistentKey) || !!sessionStorage.getItem(sessionKey));
            } else {
                setDismissed(false);
            }
            if (!next.enabled || !next.text) {
                document.body.classList.remove(notificationBarClass);
            }
        };

        window.addEventListener('notification-bar-updated', handleUpdate);
        return () => window.removeEventListener('notification-bar-updated', handleUpdate);
    }, []);

    // Check sessionStorage for dismissal (keyed by text so new messages re-show)
    useEffect(() => {
        if (config?.text) {
            const persistentKey = notificationBarPersistentDismissKey(config);
            const sessionKey = notificationBarSessionDismissKey(config);
            setDismissed(!!localStorage.getItem(persistentKey) || !!sessionStorage.getItem(sessionKey));
        }
    }, [config?.text, config?.updated_at]);

    useEffect(() => {
        if (!showDismissMenu) return;
        const handleClickOutside = (event: MouseEvent) => {
            const target = event.target as Node;
            if (dismissMenuRef.current?.contains(target)) return;
            setShowDismissMenu(false);
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [showDismissMenu]);

    // Manage body class: add when visible, remove when hidden or dismissed
    const isVisible = !!config?.enabled && !!config?.text && !dismissed;
    useLayoutEffect(() => {
        document.documentElement.style.setProperty('--notification-bar-height', isVisible ? '32px' : '0px');
        if (isVisible) {
            document.body.classList.add(notificationBarClass);
        } else {
            document.body.classList.remove(notificationBarClass);
        }
        return () => {
            document.body.classList.remove(notificationBarClass);
            document.documentElement.style.setProperty('--notification-bar-height', '0px');
        };
    }, [isVisible]);

    // Dynamic marquee if text is too wide
    useEffect(() => {
        if (!isVisible) return;
        const checkWidth = () => {
            if (textRef.current && containerRef.current) {
                // Determine if text is wider than its container
                setIsMarquee(textRef.current.scrollWidth > containerRef.current.clientWidth);
            }
        };
        // Small delay to ensure DOM is fully rendered
        const timer = setTimeout(checkWidth, 100);
        window.addEventListener('resize', checkWidth);
        return () => {
            clearTimeout(timer);
            window.removeEventListener('resize', checkWidth);
        };
    }, [isVisible, config?.text]);

    if (!isVisible) return null;

    const dismissForSession = () => {
        if (!config) return;
        const key = notificationBarSessionDismissKey(config);
        sessionStorage.setItem(key, '1');
        document.body.classList.remove(notificationBarClass);
        setDismissed(true);
        setShowDismissMenu(false);
    };

    const dismissPersistently = () => {
        if (!config) return;
        const key = notificationBarPersistentDismissKey(config);
        localStorage.setItem(key, '1');
        document.body.classList.remove(notificationBarClass);
        setDismissed(true);
        setShowDismissMenu(false);
    };

    // Calculate dynamic duration: longer text = longer animation so speed is consistent
    const duration = config ? Math.max(20, config.text.length * 0.2) + 's' : '20s';

    return (
        <div className="notification-bar">
            <div className="notification-bar-inner" ref={containerRef}>
                <span 
                    ref={textRef} 
                    className={`notification-bar-text ${isMarquee ? 'marquee' : ''}`}
                    title={config!.text}
                    style={isMarquee ? { animationDuration: duration } : {}}
                >
                    {config!.text}
                </span>
            </div>
            <div className="notification-bar-close-wrap" ref={dismissMenuRef}>
                <button
                    className="notification-bar-close"
                    onClick={() => setShowDismissMenu(v => !v)}
                    aria-label="Close"
                    aria-expanded={showDismissMenu}
                >
                    ✕
                </button>
                {showDismissMenu && (
                    <div className="notification-bar-dismiss-menu">
                        <button type="button" onClick={dismissForSession}>
                            {isChinese ? '仅本次关闭' : 'Close for now'}
                        </button>
                        <button type="button" onClick={dismissPersistently}>
                            {isChinese ? '不再显示' : 'Do not show again'}
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

export default function App() {
    const { token, setAuth } = useAuthStore();
    const [loading, setLoading] = useState(true);
    const [bootstrapUnavailable, setBootstrapUnavailable] = useState(false);
    const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
    const { i18n } = useTranslation();
    const isChinese = i18n.language.startsWith('zh');

    useEffect(() => {
        let cancelled = false;
        const initializeAuth = async () => {
            setLoading(true);
            setBootstrapUnavailable(false);
            // Initialize theme on app mount (ensures login page gets correct theme)
            const savedTheme = localStorage.getItem('theme') || 'light';
            document.documentElement.setAttribute('data-theme', savedTheme);

            // Cross-domain tenant switching transports the scoped JWT in a URL
            // fragment, which browsers do not send to the server or access logs.
            // Legacy query links are consumed once for backward compatibility.
            const pathsWithOwnToken = ['/reset-password', '/verify-email'];
            const currentUrl = new URL(window.location.href);
            const urlSession = consumeTenantSwitchSessionFromUrl(currentUrl, pathsWithOwnToken);
            if (urlSession) pendingCrossOriginSession = urlSession;
            const pendingCrossOriginToken = pendingCrossOriginSession?.token || null;
            const storedToken = localStorage.getItem('token');
            const effectiveToken = resolveBootstrapToken(
                pendingCrossOriginToken,
                storedToken,
                token,
            );

            if (urlSession) {
                // Remove the one-time transport fragment/query before auth calls.
                const cleanUrl = currentUrl.pathname + currentUrl.search + currentUrl.hash;
                window.history.replaceState({}, '', cleanUrl);
            }

            if (effectiveToken) {
                try {
                    // Candidate validation uses an explicit Authorization
                    // header.  request() recognizes it as non-current and
                    // therefore cannot clear the existing origin identity on
                    // a 401. setAuth commits only after browser-session setup.
                    const isCrossOriginCandidate = effectiveToken === pendingCrossOriginToken;
                    const authenticatedUser = isCrossOriginCandidate
                        ? await validateCrossOriginTenantSwitch({
                            tenantId: pendingCrossOriginSession?.targetTenantId,
                            accessToken: effectiveToken,
                            validateToken: (candidateToken) => withAuthBootstrapTimeout(
                                (signal) => authApi.me(candidateToken, signal),
                            ),
                            resolvedTenantId: (candidateUser) => candidateUser.tenant_id,
                            resolveCurrentOriginTenant: () => tenantApi.resolveByDomain(window.location.host),
                        })
                        : await withAuthBootstrapTimeout(
                            (signal) => authApi.me(effectiveToken, signal),
                        );
                    if (!cancelled) {
                        await setAuth(authenticatedUser, effectiveToken);
                        if (authenticatedUser.tenant_id) {
                            localStorage.setItem('current_tenant_id', authenticatedUser.tenant_id);
                        }
                        if (isCrossOriginCandidate) {
                            pendingCrossOriginSession = null;
                        }
                    }
                } catch (error) {
                    if (!cancelled) {
                        const rejectedCrossOriginCandidate =
                            effectiveToken === pendingCrossOriginToken
                            && !isTransientAuthBootstrapFailure(error);
                        if (rejectedCrossOriginCandidate) {
                            pendingCrossOriginSession = null;
                        }
                        const priorToken = storedToken || token;
                        if (
                            rejectedCrossOriginCandidate
                            && priorToken
                            && priorToken !== effectiveToken
                        ) {
                            try {
                                const priorUser = await withAuthBootstrapTimeout(
                                    (signal) => authApi.me(priorToken, signal),
                                );
                                if (!cancelled) await setAuth(priorUser, priorToken);
                            } catch (priorError) {
                                if (!cancelled) {
                                    if (isDefinitiveAuthRejection(priorError)) {
                                        useAuthStore.getState().logout();
                                    } else {
                                        setBootstrapUnavailable(true);
                                    }
                                }
                            }
                        } else if (isDefinitiveAuthRejection(error)) {
                            useAuthStore.getState().logout();
                        } else {
                            // A network outage or upstream 5xx must not erase a
                            // valid local session. Keep the token and let the
                            // user retry the same identity after service recovery.
                            setBootstrapUnavailable(true);
                        }
                    }
                }
            }
            if (!cancelled) setLoading(false);
        };

        void initializeAuth();
        return () => {
            cancelled = true;
        };
    }, [bootstrapAttempt]);


    if (loading) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', color: 'var(--text-tertiary)' }}>
                加载中...
            </div>
        );
    }

    if (bootstrapUnavailable) {
        return (
            <main
                role="alert"
                data-testid="auth-bootstrap-recovery"
                style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 24, background: 'var(--bg-primary)' }}
            >
                <section style={{ width: 'min(100%, 460px)', padding: 28, border: '1px solid var(--border-subtle)', borderRadius: 16, background: 'var(--bg-secondary)', boxShadow: 'var(--shadow-lg)' }}>
                    <h1 style={{ margin: 0, fontSize: 24 }}>
                        {isChinese ? '暂时无法连接服务' : 'Service is temporarily unavailable'}
                    </h1>
                    <p style={{ margin: '12px 0 20px', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
                        {isChinese
                            ? '登录状态和本地工作不会被清除。服务恢复后，请重新检查连接。'
                            : 'Your sign-in state and local work were preserved. Retry after the service recovers.'}
                    </p>
                    <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => setBootstrapAttempt((value) => value + 1)}
                    >
                        {isChinese ? '重新检查连接' : 'Retry connection'}
                    </button>
                </section>
            </main>
        );
    }

    return (
        <>
            <AuthQueryScopeReset />
            <AuthAccessRefresh />
            <NotificationBar />
            <Suspense fallback={<div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', color: 'var(--text-tertiary)' }}>加载中...</div>}>
            <Routes>
                <Route path="/login" element={<Login />} />
                <Route path="/forgot-password" element={<ForgotPassword />} />
                <Route path="/reset-password" element={<ResetPassword />} />
                <Route path="/verify-email" element={<VerifyEmail />} />
                <Route path="/oauth/callback/:provider" element={<OAuthCallback />} />
                <Route path="/sso/entry" element={<SSOEntry />} />
                <Route path="/" element={<ProtectedRoute><ProductEntryRoute /></ProtectedRoute>} />
                <Route path="/choose-surface" element={<ProtectedRoute><SurfaceChooser /></ProtectedRoute>} />
                <Route path="/setup-company" element={<ProtectedRoute><CompanyAccess /></ProtectedRoute>} />
                <Route path="/account/companies" element={<ProtectedRoute><AccountCompanies /></ProtectedRoute>} />
                <Route path="/account/security" element={<ProtectedRoute><AccountSecurity /></ProtectedRoute>} />
                <Route path="/company-admin/*" element={<ProtectedRoute><CompanyAdminRoute><CompanyAdmin /></CompanyAdminRoute></ProtectedRoute>} />
                <Route path="/admin/platform/*" element={<ProtectedRoute><PlatformAdminRoute><PlatformOperations /></PlatformAdminRoute></ProtectedRoute>} />
                <Route path="/enterprise" element={<ProtectedRoute><CompanyAdminRoute><LegacyCompanyAdminRedirect /></CompanyAdminRoute></ProtectedRoute>} />
                <Route path="/invitations" element={<ProtectedRoute><CompanyAdminRoute><Navigate to="/company-admin/members" replace /></CompanyAdminRoute></ProtectedRoute>} />
                <Route path="/admin/platform-settings" element={<ProtectedRoute><PlatformAdminRoute><Navigate to="/admin/platform/companies" replace /></PlatformAdminRoute></ProtectedRoute>} />
                <Route path="/admin/saas" element={<ProtectedRoute><PlatformAdminRoute><LegacyPlatformRedirect /></PlatformAdminRoute></ProtectedRoute>} />
                <Route path="/account" element={<ProtectedRoute><PlatformAdminRoute><Navigate to="/admin/platform/providers?tab=accounts" replace /></PlatformAdminRoute></ProtectedRoute>} />
                <Route path="/onboarding" element={<ProtectedRoute><TenantWorkspaceRoute><Onboarding /></TenantWorkspaceRoute></ProtectedRoute>} />
                <Route element={<ProtectedRoute><TenantWorkspaceRoute><Layout /></TenantWorkspaceRoute></ProtectedRoute>}>
                    <Route path="work" element={<TenantWorkspaceRoute><Work /></TenantWorkspaceRoute>} />
                    <Route path="work/:taskId" element={<TenantWorkspaceRoute><WorkDetail /></TenantWorkspaceRoute>} />
                    <Route path="employees" element={<TenantWorkspaceRoute><Employees /></TenantWorkspaceRoute>} />
                    <Route path="dashboard" element={<TenantWorkspaceRoute><Dashboard /></TenantWorkspaceRoute>} />
                    <Route path="plaza" element={<TenantWorkspaceRoute><Plaza /></TenantWorkspaceRoute>} />
                    <Route path="agents/new" element={<TenantWorkspaceRoute><AgentCreate /></TenantWorkspaceRoute>} />
                    <Route path="agents/:id" element={<TenantWorkspaceRoute><Navigate to="chat" replace /></TenantWorkspaceRoute>} />
                    <Route path="agents/:id/chat" element={<TenantWorkspaceRoute><AgentDetail /></TenantWorkspaceRoute>} />
                    <Route path="agents/:id/directory" element={<TenantWorkspaceRoute><AgentDetail /></TenantWorkspaceRoute>} />
                    <Route path="agents/:id/settings" element={<TenantWorkspaceRoute><AgentDetail /></TenantWorkspaceRoute>} />
                    <Route path="quality-reviews/:reviewId" element={<TenantWorkspaceRoute><QualityReview /></TenantWorkspaceRoute>} />
                    <Route path="groups" element={<TenantWorkspaceRoute><GroupsPage /></TenantWorkspaceRoute>} />
                    <Route path="groups/:groupId" element={<TenantWorkspaceRoute><GroupsPage /></TenantWorkspaceRoute>} />
                    <Route path="groups/:groupId/:sessionId" element={<TenantWorkspaceRoute><GroupsPage /></TenantWorkspaceRoute>} />
                    <Route path="messages" element={<TenantWorkspaceRoute><Messages /></TenantWorkspaceRoute>} />
                    <Route path="okr" element={<TenantWorkspaceRoute><OKR /></TenantWorkspaceRoute>} />
                    <Route path="account/subscription" element={<TenantWorkspaceRoute><SubscriptionDetail /></TenantWorkspaceRoute>} />
                    <Route path="billing/success" element={<TenantWorkspaceRoute><BillingSuccess /></TenantWorkspaceRoute>} />
                </Route>
            </Routes>
            </Suspense>
        </>
    );
}
