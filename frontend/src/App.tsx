import { Routes, Route, Navigate } from 'react-router';
import { useAuthStore } from './stores';
import { Suspense, lazy, useEffect, useLayoutEffect, useState, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { authApi } from './services/api';
import { canAccessSaasAdmin } from './utils/saasAdmin';
import {
    consumeSessionTokenFromUrl,
    establishBrowserSession,
    resolveBootstrapToken,
} from './utils/authTransport';
import { useQueryClient } from '@tanstack/react-query';
import { authQueryScopeKey, tenantWorkspaceRedirect } from './utils/workspaceAccess';

const Login = lazy(() => import('./pages/Login'));
const ForgotPassword = lazy(() => import('./pages/ForgotPassword'));
const ResetPassword = lazy(() => import('./pages/ResetPassword'));
const VerifyEmail = lazy(() => import('./pages/VerifyEmail'));
const CompanySetup = lazy(() => import('./pages/CompanySetup'));
const Onboarding = lazy(() => import('./pages/Onboarding'));
const Layout = lazy(() => import('./pages/Layout'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const Work = lazy(() => import('./pages/Work'));
const Plaza = lazy(() => import('./pages/Plaza'));
const AgentDetail = lazy(() => import('./pages/AgentDetail'));
const AgentCreate = lazy(() => import('./pages/AgentCreate'));
const Messages = lazy(() => import('./pages/Messages'));
const EnterpriseSettings = lazy(() => import('./pages/EnterpriseSettings'));
const InvitationCodes = lazy(() => import('./pages/InvitationCodes'));
const AdminCompanies = lazy(() => import('./pages/AdminCompanies'));
const AccountManagement = lazy(() => import('./pages/AccountManagement'));
const SubscriptionDetail = lazy(() => import('./pages/SubscriptionDetail'));
const BillingSuccess = lazy(() => import('./pages/BillingSuccess'));
const SaasAdmin = lazy(() => import('./pages/SaasAdmin'));
const OAuthCallback = lazy(() => import('./pages/OAuthCallback'));
const SSOEntry = lazy(() => import('./pages/SSOEntry'));
const OKR = lazy(() => import('./pages/OKR'));
const GroupsPage = lazy(() => import('./pages/groups/GroupsPage'));
const QualityReview = lazy(() => import('./pages/QualityReview'));

function ProtectedRoute({ children }: { children: React.ReactNode }) {
    const token = useAuthStore((s) => s.token);
    const user = useAuthStore((s) => s.user);
    if (!token) return <Navigate to="/login" replace />;
    // Global platform administrators intentionally have no tenant. Do not trap
    // them in tenant onboarding; their landing surface is the platform console.
    if (user && !user.tenant_id && user.role !== 'platform_admin' && !(user as any).is_platform_admin) {
        return <Navigate to="/setup-company" replace />;
    }
    
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
    const canAccessCompanySettings = user?.role === 'platform_admin' || user?.role === 'org_admin' || !!(user as any)?.is_platform_admin;
    if (!canAccessCompanySettings) return <Navigate to="/" replace />;
    return <>{children}</>;
}

function SaasAdminRoute({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!canAccessSaasAdmin(user)) return <Navigate to="/" replace />;
    return <>{children}</>;
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

    useEffect(() => {
        let cancelled = false;
        const initializeAuth = async () => {
            // Initialize theme on app mount (ensures login page gets correct theme)
            const savedTheme = localStorage.getItem('theme') || 'light';
            document.documentElement.setAttribute('data-theme', savedTheme);

            // Cross-domain tenant switching transports the scoped JWT in a URL
            // fragment, which browsers do not send to the server or access logs.
            // Legacy query links are consumed once for backward compatibility.
            const pathsWithOwnToken = ['/reset-password', '/verify-email'];
            const currentUrl = new URL(window.location.href);
            const urlToken = consumeSessionTokenFromUrl(currentUrl, pathsWithOwnToken);
            const effectiveToken = resolveBootstrapToken(
                urlToken,
                localStorage.getItem('token'),
                token,
            );

            if (urlToken) {
                // /auth/me reads its bearer credential from localStorage. The
                // authenticated tree remains behind loading until setAuth has
                // also confirmed the HttpOnly browser session.
                localStorage.setItem('token', urlToken);
                useAuthStore.setState({ token: urlToken, user: null });

                // Remove the one-time transport fragment/query before auth calls.
                const cleanUrl = currentUrl.pathname + currentUrl.search + currentUrl.hash;
                window.history.replaceState({}, '', cleanUrl);
            }

            if (effectiveToken) {
                try {
                    const existingUser = useAuthStore.getState().user;
                    if (existingUser) {
                        // A user can survive a tenant/origin switch in the SPA
                        // while the host-only HttpOnly cookie does not. Refresh
                        // it even when the in-memory user is already populated;
                        // otherwise native media URLs can receive a 401 while
                        // fetch-based API calls still succeed with the bearer
                        // token from localStorage.
                        await establishBrowserSession(effectiveToken);
                    } else {
                        const authenticatedUser = await authApi.me();
                        if (!cancelled) {
                            await setAuth(authenticatedUser, effectiveToken);
                        }
                    }
                } catch {
                    if (!cancelled) {
                        useAuthStore.getState().logout();
                    }
                }
            }
            if (!cancelled) setLoading(false);
        };

        void initializeAuth();
        return () => {
            cancelled = true;
        };
    }, []);


    if (loading) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', color: 'var(--text-tertiary)' }}>
                加载中...
            </div>
        );
    }

    return (
        <>
            <AuthQueryScopeReset />
            <NotificationBar />
            <Suspense fallback={<div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', color: 'var(--text-tertiary)' }}>加载中...</div>}>
            <Routes>
                <Route path="/login" element={<Login />} />
                <Route path="/forgot-password" element={<ForgotPassword />} />
                <Route path="/reset-password" element={<ResetPassword />} />
                <Route path="/verify-email" element={<VerifyEmail />} />
                <Route path="/oauth/callback/:provider" element={<OAuthCallback />} />
                <Route path="/sso/entry" element={<SSOEntry />} />
                <Route path="/setup-company" element={<CompanySetup />} />
                <Route path="/admin/saas" element={<SaasAdminRoute><SaasAdmin /></SaasAdminRoute>} />
                <Route path="/onboarding" element={<ProtectedRoute><TenantWorkspaceRoute><Onboarding /></TenantWorkspaceRoute></ProtectedRoute>} />
                <Route path="/" element={<ProtectedRoute><Layout /></ProtectedRoute>}>
                    <Route index element={<Navigate to="/work" replace />} />
                    <Route path="work" element={<TenantWorkspaceRoute><Work /></TenantWorkspaceRoute>} />
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
                    <Route path="enterprise" element={<TenantWorkspaceRoute><CompanyAdminRoute><EnterpriseSettings /></CompanyAdminRoute></TenantWorkspaceRoute>} />
                    <Route path="okr" element={<TenantWorkspaceRoute><OKR /></TenantWorkspaceRoute>} />
                    <Route path="invitations" element={<TenantWorkspaceRoute><CompanyAdminRoute><InvitationCodes /></CompanyAdminRoute></TenantWorkspaceRoute>} />
                    <Route path="admin/platform-settings" element={<AdminCompanies />} />
                    <Route path="account" element={<SaasAdminRoute><AccountManagement /></SaasAdminRoute>} />
                    <Route path="account/subscription" element={<TenantWorkspaceRoute><SubscriptionDetail /></TenantWorkspaceRoute>} />
                    <Route path="billing/success" element={<TenantWorkspaceRoute><BillingSuccess /></TenantWorkspaceRoute>} />
                </Route>
            </Routes>
            </Suspense>
        </>
    );
}
