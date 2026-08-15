import { useState, useEffect } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '../stores';
import {
    authApi,
    tenantApi,
    fetchJson,
    type MfaLoginChallenge,
    type MfaTokenResponse,
} from '../services/api';
import {
    createTenantSsoAuthorization,
    loadTenantSsoProviders,
} from '../services/ssoLogin';
import type { TokenResponse } from '../types';
import {
    IconAlertTriangle,
    IconArrowRight,
    IconCheck,
} from '@tabler/icons-react';
import { AtlasFrame, OriginPlate } from '../components/atlas';
import { deriveRegistrationIdentity } from '../utils/registrationIdentity';

export default function Login() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const legacyCode = searchParams.get('code') || '';
    const invitedEmail = searchParams.get('email') || '';
    // New links name the two credentials explicitly. Legacy links with an
    // email are treated as organization invitations; legacy bare codes remain
    // registration grants. Neither credential is consumed as the other kind.
    const organizationInvitationToken = searchParams.get('organization_invitation')
        || searchParams.get('join_token')
        || (invitedEmail ? legacyCode : '');
    const initialRegistrationGrant = searchParams.get('registration_grant')
        || searchParams.get('registration_code')
        || (!invitedEmail ? legacyCode : '');
    const setAuth = useAuthStore((s) => s.setAuth);
    // Default to register if there's an invitation code — will be overridden after email check
    const [isRegister, setIsRegister] = useState(!!organizationInvitationToken || !!initialRegistrationGrant);
    const [error, setError] = useState('');
    const [successMessage, setSuccessMessage] = useState('');
    const [loading, setLoading] = useState(false);
    const [tenant, setTenant] = useState<any>(null);
    const [resolving, setResolving] = useState(true);
    const [ssoProviders, setSsoProviders] = useState<any[]>([]);
    const [oauthProviders, setOauthProviders] = useState<any[]>([]);
    const [ssoLoading, setSsoLoading] = useState(false);
    const [oauthLoading, setOauthLoading] = useState(false);
    const [ssoError, setSsoError] = useState('');
    const [oauthError, setOauthError] = useState('');
    const [tenantSelection, setTenantSelection] = useState<any[] | null>(null);
    const [showVerification, setShowVerification] = useState(false);
    const [verificationEmail, setVerificationEmail] = useState('');
    const [verificationCode, setVerificationCode] = useState('');
    const [verificationEntryMode, setVerificationEntryMode] = useState<'create' | 'join' | 'home'>('home');
    const [registrationCodeRequired, setRegistrationCodeRequired] = useState(true);
    const [passwordRegistrationAvailable, setPasswordRegistrationAvailable] = useState<boolean | null>(null);
    const [registrationCode, setRegistrationCode] = useState(initialRegistrationGrant);
    const [mfaFlow, setMfaFlow] = useState<{
        stage: 'verify' | 'setup' | 'recovery';
        challengeToken: string;
        destination: string;
        secret?: string;
        provisioningUri?: string;
        recoveryCodes?: string[];
        tokenResponse?: MfaTokenResponse;
    } | null>(null);
    const [mfaCode, setMfaCode] = useState('');
    const [mfaCodesSaved, setMfaCodesSaved] = useState(false);

    const [form, setForm] = useState({
        login_identifier: invitedEmail,  // Pre-fill invited email if present
        password: '',
        tenant_id: '',
    });

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');

        authApi.registrationConfig()
            .then(config => {
                setRegistrationCodeRequired(!!config.invitation_code_required);
                setPasswordRegistrationAvailable(config.password_registration_available ?? true);
            })
            .catch(() => {
                setRegistrationCodeRequired(true);
                setPasswordRegistrationAvailable(false);
            });

        // Resolve tenant by domain (for SSO detection only, not for login form)
        const domain = window.location.host;
        if (domain.startsWith('localhost') || domain.startsWith('127.0.0.1')) {
            setResolving(false);
            return;
        }

        tenantApi.resolveByDomain(domain)
            .then(res => {
                if (res) {
                    setTenant(res);
                }
            })
            .catch(() => { })
            .finally(() => setResolving(false));
    }, []);

    useEffect(() => {
        let cancelled = false;
        if (isRegister) {
            setOauthProviders([]);
            setOauthError('');
            return;
        }

        setOauthLoading(true);
        setOauthError('');
        fetchJson<any[]>('/auth/providers')
            .then(providers => {
                if (cancelled) return;
                setOauthProviders((providers || []).filter(p => ['google', 'github'].includes(p.provider_type)));
            })
            .catch(() => {
                if (cancelled) return;
                setOauthProviders([]);
                setOauthError('Failed to load social login providers.');
            })
            .finally(() => {
                if (cancelled) return;
                setOauthLoading(false);
            });

        return () => { cancelled = true; };
    }, [isRegister]);

    useEffect(() => {
        let cancelled = false;
        if (!tenant?.sso_enabled || isRegister) {
            setSsoProviders([]);
            setSsoError('');
            return;
        }
        if (!tenant?.id) return;

        setSsoLoading(true);
        setSsoError('');

        loadTenantSsoProviders(tenant.id)
            .then(providers => {
                if (cancelled) return;
                setSsoProviders(providers || []);
            })
            .catch(() => {
                if (cancelled) return;
                setSsoError(t('auth.ssoLoadFailed', 'Failed to load SSO providers.'));
                setSsoProviders([]);
            })
            .finally(() => {
                if (cancelled) return;
                setSsoLoading(false);
            });

        return () => { cancelled = true; };
    }, [tenant?.id, tenant?.sso_enabled, isRegister, t]);

    const toggleLang = () => {
        i18n.changeLanguage(i18n.language === 'zh' ? 'en' : 'zh');
    };

    const isZh = i18n.language.startsWith('zh');

    const enterVerificationStep = (email: string, mode: 'create' | 'join' | 'home') => {
        setVerificationEmail(email);
        setVerificationCode('');
        setVerificationEntryMode(mode);
        setShowVerification(true);
        setTenantSelection(null);
    };

    const handleVerifyEmail = async (e: React.FormEvent) => {
        e.preventDefault();
        const token = verificationCode.trim();
        if (!token) return;

        setError('');
        setSuccessMessage('');
        setLoading(true);

        try {
            const res = await authApi.verifyEmail(token);
            if (res.access_token && res.user) {
                await setAuth(res.user, res.access_token);
            }

            if (res.needs_company_setup || verificationEntryMode === 'join') {
                const joinQuery = organizationInvitationToken
                    ? `?join_token=${encodeURIComponent(organizationInvitationToken)}`
                    : '';
                navigate(`/setup-company${joinQuery}`, {
                    state: {
                        fromRegister: true,
                        email: verificationEmail || res.user?.email,
                    },
                });
                return;
            }

            navigate('/');
        } catch (err: any) {
            setError(err.message || (isZh ? '验证凭证无效或已过期' : 'The verification token is invalid or expired.'));
        } finally {
            setLoading(false);
        }
    };

    const handleResendVerification = async () => {
        const email = verificationEmail || form.login_identifier;
        if (!email) return;

        setError('');
        setSuccessMessage('');
        setLoading(true);

        try {
            await authApi.resendVerification(email);
            setSuccessMessage(isZh ? `新的验证凭证已发送到 ${email}` : `A new verification token has been sent to ${email}.`);
        } catch (err: any) {
            setError(err.message || (isZh ? '发送验证凭证失败' : 'Failed to resend the verification token.'));
        } finally {
            setLoading(false);
        }
    };

    const isMfaChallenge = (value: unknown): value is MfaLoginChallenge => {
        if (!value || typeof value !== 'object') return false;
        const candidate = value as Partial<MfaLoginChallenge>;
        return typeof candidate.challenge_token === 'string'
            && (candidate.requires_mfa === true || candidate.requires_mfa_setup === true);
    };

    const finishTokenLogin = async (tokenResponse: TokenResponse, destination: string) => {
        await setAuth(tokenResponse.user, tokenResponse.access_token);
        setMfaFlow(null);
        setMfaCode('');
        setMfaCodesSaved(false);
        navigate(destination);
    };

    const beginMfaFlow = async (challenge: MfaLoginChallenge, destination: string) => {
        setTenantSelection(null);
        setMfaCode('');
        setMfaCodesSaved(false);
        if (challenge.requires_mfa_setup) {
            const setup = await authApi.startMfaBootstrap(challenge.challenge_token);
            setMfaFlow({
                stage: 'setup',
                challengeToken: setup.challenge_token,
                destination,
                secret: setup.secret,
                provisioningUri: setup.provisioning_uri,
            });
            return;
        }
        setMfaFlow({
            stage: 'verify',
            challengeToken: challenge.challenge_token,
            destination,
        });
    };

    const handleLoginResult = async (result: Awaited<ReturnType<typeof authApi.login>>, destination: string) => {
        if ('requires_tenant_selection' in result && result.requires_tenant_selection) {
            setTenantSelection(result.tenants);
            return;
        }
        if (isMfaChallenge(result)) {
            await beginMfaFlow(result, destination);
            return;
        }
        await finishTokenLogin(result as TokenResponse, destination);
    };

    const handleMfaSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!mfaFlow || mfaFlow.stage === 'recovery' || !mfaCode.trim()) return;
        setError('');
        setLoading(true);
        try {
            const tokenResponse = mfaFlow.stage === 'setup'
                ? await authApi.confirmMfaSetup(mfaFlow.challengeToken, mfaCode.trim())
                : await authApi.verifyMfaChallenge(mfaFlow.challengeToken, mfaCode.trim());
            if (tokenResponse.recovery_codes?.length) {
                setMfaFlow({
                    ...mfaFlow,
                    stage: 'recovery',
                    recoveryCodes: tokenResponse.recovery_codes,
                    tokenResponse,
                });
                setMfaCode('');
                return;
            }
            await finishTokenLogin(tokenResponse, mfaFlow.destination);
        } catch (nextError: any) {
            setError(nextError.message || (isZh ? '多因素验证失败，请重试。' : 'Multi-factor verification failed.'));
        } finally {
            setLoading(false);
        }
    };

    const copyRecoveryCodes = async () => {
        if (!mfaFlow?.recoveryCodes?.length) return;
        try {
            await navigator.clipboard.writeText(mfaFlow.recoveryCodes.join('\n'));
            setSuccessMessage(isZh ? '恢复码已复制，请保存到安全的位置。' : 'Recovery codes copied. Store them securely.');
        } catch {
            setError(isZh ? '无法自动复制，请手动保存恢复码。' : 'Copy failed. Save the recovery codes manually.');
        }
    };

    const finishRecoveryStep = async () => {
        if (!mfaFlow?.tokenResponse || !mfaCodesSaved) return;
        await finishTokenLogin(mfaFlow.tokenResponse, mfaFlow.destination);
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setSuccessMessage('');
        setLoading(true);

        try {
            if (isRegister) {
                if (passwordRegistrationAvailable === false) {
                    setError(t('auth.registrationUnavailable'));
                    setLoading(false);
                    return;
                }
                const code = registrationCode.trim();
                if (registrationCodeRequired && !code) {
                    setError(isZh ? '请输入注册码' : 'Registration code is required.');
                    setLoading(false);
                    return;
                }
                const registrationIdentity = deriveRegistrationIdentity(form.login_identifier);
                const regRes = await authApi.register({
                    username: registrationIdentity.username,
                    email: form.login_identifier,
                    password: form.password,
                    display_name: registrationIdentity.displayName,
                    ...(code ? { invitation_code: code } : {})
                });
                // Save authentication state for company selection (user not active yet)
                if (regRes.access_token && regRes.user) {
                    await setAuth(regRes.user, regRes.access_token);
                }
                if (regRes.user?.email_verified || regRes.user?.is_active) {
                    if (organizationInvitationToken) {
                        navigate(`/setup-company?join_token=${encodeURIComponent(organizationInvitationToken)}`);
                    } else if (regRes.needs_company_setup) {
                        navigate('/setup-company', {
                            state: { fromRegister: true, email: regRes.email || form.login_identifier },
                        });
                    } else {
                        navigate('/');
                    }
                    return;
                }
                enterVerificationStep(regRes.email || form.login_identifier, organizationInvitationToken ? 'join' : 'create');
                setSuccessMessage(
                    i18n.language.startsWith('zh')
                        ? `验证凭证已发送到 ${regRes.email || form.login_identifier}`
                        : `A verification token has been sent to ${regRes.email || form.login_identifier}.`
                );
                return;
            } else {
                const res = await authApi.login({
                    login_identifier: form.login_identifier,
                    password: form.password,
                    // Only pass tenant_id for dedicated SSO subdomain login (not IP-mode SSO).
                    // IP-mode SSO resolves a tenant for SSO buttons only and must NOT constrain
                    // password-based login to that tenant (it would reject users from other tenants).
                    ...(tenant?.id && tenant.sso_domain && !tenant.sso_domain.match(/^https?:\/\/\d{1,3}(\.\d{1,3}){3}(:\d+)?$/)
                        ? { tenant_id: tenant.id }
                        : {}
                    ),
                });

                // Check if multi-tenant selection is needed
                if ('requires_tenant_selection' in res && res.requires_tenant_selection) {
                    setTenantSelection(res.tenants);
                    setLoading(false);
                    return;
                }

                // Organization membership is always an explicit post-login
                // acceptance step. Login never consumes a company credential.
                const destination = organizationInvitationToken
                    ? `/setup-company?join_token=${encodeURIComponent(organizationInvitationToken)}`
                    : '/';
                await handleLoginResult(res, destination);
            }
        } catch (err: any) {
            // Handle structured verification error
            if (err.detail?.needs_verification) {
                enterVerificationStep(err.detail.email || form.login_identifier, 'home');
                setSuccessMessage(
                    i18n.language.startsWith('zh')
                        ? `请先输入发送到 ${err.detail.email || form.login_identifier} 的验证凭证。`
                        : `Enter the verification token sent to ${err.detail.email || form.login_identifier}.`
                );
                return;
            }

            const msg = err.message || '';
            if (msg && msg !== 'Failed to fetch' && !msg.includes('NetworkError') && !msg.includes('ERR_CONNECTION')) {
                if (msg.includes('company has been disabled')) {
                    setError(t('auth.companyDisabled'));
                } else if (msg.includes('Invalid credentials')) {
                    setError(t('auth.invalidCredentials'));
                } else if (msg.includes('Account is disabled')) {
                    setError(t('auth.accountDisabled'));
                } else if (msg.includes('does not belong to this organization')) {
                    setError(t('auth.notInOrganization', 'This account does not belong to this organization.'));
                } else if (msg.includes('500') || msg.includes('Internal Server Error')) {
                    setError(t('auth.serverStarting'));
                } else if (msg.includes('Email already registered') || msg.includes('该邮箱已注册')) {
                    setError(t('auth.emailAlreadyRegistered', '该邮箱已注册，请直接登录'));
                } else if (msg.includes('Password registration is temporarily unavailable')) {
                    setError(t('auth.registrationUnavailable'));
                } else {
                    setError(msg);
                }
            } else {
                setError(t('auth.serverUnreachable'));
            }
        } finally {
            setLoading(false);
        }
    };

    const handleTenantSelect = async (tenantId: string) => {
        setForm(f => ({ ...f, tenant_id: tenantId }));
        setTenantSelection(null);
        setError('');
        setLoading(true);

        try {
            const res = await authApi.login({
                login_identifier: form.login_identifier,
                password: form.password,
                tenant_id: tenantId,
            });

            // Should not get multi-tenant response when tenant_id is provided
            if ('requires_tenant_selection' in res && res.requires_tenant_selection) {
                setTenantSelection(res.tenants);
                setLoading(false);
                return;
            }

            const destination = organizationInvitationToken
                ? `/setup-company?join_token=${encodeURIComponent(organizationInvitationToken)}`
                : '/';
            await handleLoginResult(res, destination);
        } catch (err: any) {
            const msg = err.message || '';
            setError(msg || t('auth.loginFailed', 'Login failed'));
        } finally {
            setLoading(false);
        }
    };

    const ssoMeta: Record<string, { label: string; icon: string }> = {
        feishu: { label: 'Feishu', icon: '/feishu.png' },
        dingtalk: { label: 'DingTalk', icon: '/dingtalk.png' },
        wecom: { label: 'WeCom', icon: '/wecom.png' },
        google: { label: 'Google', icon: '/google.svg' },
        google_workspace: { label: 'Google', icon: '/google.svg' },
    };

    const startOAuthLogin = async (providerType: string) => {
        try {
            const redirectUri = `${window.location.origin}/oauth/callback/${providerType}`;
            const res = await fetchJson<{ authorization_url: string }>(
                `/auth/${providerType}/authorize?redirect_uri=${encodeURIComponent(redirectUri)}`
            );
            if (res?.authorization_url) {
                window.location.href = res.authorization_url;
            }
        } catch (err: any) {
            setError(err.message || 'Failed to start social login');
        }
    };

    const startSsoLogin = async (providerType: string) => {
        if (!tenant?.id) return;
        setSsoLoading(true);
        setSsoError('');
        try {
            const authorizationUrl = await createTenantSsoAuthorization(
                tenant.id,
                providerType,
            );
            window.location.href = authorizationUrl;
        } catch (err: any) {
            setSsoError(
                err.message || t('auth.ssoLoadFailed', 'Failed to start SSO login.'),
            );
        } finally {
            setSsoLoading(false);
        }
    };

    const shouldShowGlobalOAuth = !tenant?.sso_enabled && !isRegister && !showVerification && !mfaFlow;

    const mfaTitle = mfaFlow?.stage === 'setup'
        ? (isZh ? '设置多因素验证' : 'Set up multi-factor authentication')
        : mfaFlow?.stage === 'recovery'
            ? (isZh ? '保存恢复码' : 'Save recovery codes')
            : (isZh ? '完成多因素验证' : 'Complete multi-factor authentication');
    const mfaSubtitle = mfaFlow?.stage === 'setup'
        ? (isZh ? '高权限账号必须先绑定验证器，验证完成后才会签发访问令牌。' : 'Privileged accounts must bind an authenticator before an access token is issued.')
        : mfaFlow?.stage === 'recovery'
            ? (isZh ? '这些一次性恢复码只显示一次。保存后再进入产品。' : 'These one-time recovery codes are shown once. Save them before continuing.')
            : (isZh ? '输入验证器动态码，或使用一枚尚未使用的恢复码。' : 'Enter an authenticator code or one unused recovery code.');

    return (
        <AtlasFrame onToggleLang={toggleLang}>
            <div className="atlas-screen-split atlas-login-split">
                {/* ── Left: Hero with compass ── */}
                <div className="atlas-screen-plate atlas-login-hero">
                    <div className="atlas-login-compass">
                        <OriginPlate size={620} />
                    </div>
                    <div className="atlas-login-welcome">
                        <h1 className="atlas-h1">
                            {isZh
                                ? '欢迎，创始人。'
                                : `${t('login.hero.welcome')} ${t('login.hero.founder')}.`}
                        </h1>
                        <p className="atlas-body atlas-body--muted">{t('login.hero.description')}</p>
                    </div>
                </div>

                {/* ── Right: Form Panel ── */}
                <div className="atlas-screen-form atlas-login-form-pane">
                    <div className="atlas-login-form-wrapper">
                    <div className="login-form-header">
                        <h2 className="login-form-title">
                            {mfaFlow
                                ? mfaTitle
                                : showVerification
                                ? (isZh ? '验证邮箱' : 'Verify email')
                                : (isRegister ? t('auth.register') : t('auth.login'))}
                        </h2>
                        <p className="login-form-subtitle">
                            {mfaFlow
                                ? mfaSubtitle
                                : showVerification
                                ? (isZh
                                    ? `输入发送到 ${verificationEmail || form.login_identifier} 的验证凭证。`
                                    : `Enter the verification token sent to ${verificationEmail || form.login_identifier}.`)
                                : (isRegister ? t('auth.subtitleRegister') : t('auth.subtitleLogin'))}
                        </p>
                    </div>

                    {error && (
                        <div className="login-error">
                            <IconAlertTriangle size={16} stroke={1.8} /> {error}
                        </div>
                    )}

                    {successMessage && (
                        <div className="login-success" style={{
                            background: 'rgba(34, 197, 94, 0.1)',
                            color: '#16a34a',
                            padding: '12px 16px',
                            borderRadius: '8px',
                            marginBottom: '16px',
                            fontSize: '14px',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '8px',
                            border: '1px solid rgba(34, 197, 94, 0.2)',
                        }}>
                            <IconCheck size={16} stroke={1.8} /> {successMessage}
                        </div>
                    )}

                    {isRegister && passwordRegistrationAvailable === false && !showVerification && !mfaFlow && (
                        <div className="login-error">
                            <IconAlertTriangle size={16} stroke={1.8} />
                            {t('auth.registrationUnavailable')}
                        </div>
                    )}

                    {tenant && tenant.sso_enabled && !isRegister && !showVerification && !mfaFlow && (
                        <div style={{ marginBottom: '24px' }}>
                            <div style={{
                                padding: '16px', borderRadius: '12px', background: 'rgba(59,130,246,0.08)',
                                border: '1px solid rgba(59,130,246,0.15)', marginBottom: '16px',
                                textAlign: 'center'
                            }}>
                                <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--accent-primary)', marginBottom: '4px' }}>
                                    {tenant.name}
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                    {t('auth.ssoNotice', 'Enterprise SSO is enabled for this domain.')}
                                </div>
                            </div>

                            {ssoLoading && (
                                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                                    {t('auth.ssoLoading', 'Loading SSO providers...')}
                                </div>
                            )}

                            {!ssoLoading && ssoProviders.length > 0 && (
                                <>
                                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '12px' }}>
                                        {ssoProviders.map(p => {
                                            const meta = ssoMeta[p.provider_type] || { label: p.name || p.provider_type, icon: '' };
                                            return (
                                                <button
                                                    key={p.provider_type}
                                                    className="login-submit"
                                                    type="button"
                                                    disabled={ssoLoading}
                                                    style={{
                                                        background: 'var(--bg-secondary)',
                                                        color: 'var(--text-primary)',
                                                        display: 'flex',
                                                        alignItems: 'center',
                                                        justifyContent: 'center',
                                                        gap: '10px',
                                                        border: '1px solid var(--border-subtle)',
                                                    }}
                                                    onClick={() => startSsoLogin(p.provider_type)}
                                                >
                                                    {meta.icon ? (
                                                        <img src={meta.icon} alt={meta.label} width={18} height={18} />
                                                    ) : (
                                                        <span style={{ width: 18, height: 18, borderRadius: 4, background: 'var(--bg-tertiary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10 }}>
                                                            {(meta.label || '').slice(0, 1).toUpperCase()}
                                                        </span>
                                                    )}
                                                    {meta.label || p.name || p.provider_type}
                                                </button>
                                            );
                                        })}
                                    </div>
                                    <div style={{ marginTop: '10px', fontSize: '11px', lineHeight: 1.5, color: 'var(--text-tertiary)', textAlign: 'center' }}>
                                        {t('auth.tenantSsoPolicy', 'Company SSO follows company policy. JIT, when enabled, creates ordinary members only—never admins or owners.')}
                                    </div>
                                </>
                            )}

                            {!ssoLoading && ssoProviders.length === 0 && (
                                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                                    {ssoError || t('auth.ssoNoProviders', 'No SSO providers configured.')}
                                </div>
                            )}

                            <div style={{
                                display: 'flex', alignItems: 'center', gap: '12px',
                                margin: '20px 0', color: 'var(--text-tertiary)', fontSize: '11px'
                            }}>
                                <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                                {t('auth.or', 'or')}
                                <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                            </div>
                        </div>
                    )}

                    {shouldShowGlobalOAuth && (
                        <div style={{ marginBottom: '24px' }}>
                            {oauthLoading && (
                                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                                    Loading social login providers...
                                </div>
                            )}

                            {!oauthLoading && oauthProviders.length > 0 && (
                                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '12px' }}>
                                    {oauthProviders.map(p => {
                                        const meta = ssoMeta[p.provider_type] || { label: p.name || p.provider_type, icon: '' };
                                        return (
                                            <button
                                                key={p.provider_type}
                                                className="login-submit"
                                                type="button"
                                                style={{
                                                    background: 'var(--bg-secondary)',
                                                    color: 'var(--text-primary)',
                                                    display: 'flex',
                                                    alignItems: 'center',
                                                    justifyContent: 'center',
                                                    gap: '10px',
                                                    border: '1px solid var(--border-subtle)',
                                                }}
                                                onClick={() => startOAuthLogin(p.provider_type)}
                                            >
                                                {meta.icon ? (
                                                    <img
                                                        src={meta.icon}
                                                        width={18}
                                                        height={18}
                                                        alt=""
                                                        aria-hidden="true"
                                                    />
                                                ) : (
                                                    <span style={{ width: 18, height: 18, borderRadius: 4, background: 'var(--bg-tertiary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10 }}>
                                                        {(meta.label || '').slice(0, 1).toUpperCase()}
                                                    </span>
                                                )}
                                                Continue with {meta.label || p.name || p.provider_type}
                                            </button>
                                        );
                                    })}
                                </div>
                            )}

                            {!oauthLoading && oauthProviders.length === 0 && oauthError && (
                                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                                    {oauthError}
                                </div>
                            )}

                            {!oauthLoading && oauthProviders.length > 0 && (
                                <>
                                    <div style={{ marginTop: '10px', fontSize: '11px', lineHeight: 1.5, color: 'var(--text-tertiary)', textAlign: 'center' }}>
                                        {t('auth.publicOAuthSignInOnly', 'Google/GitHub only sign in an already linked account. They do not create an account or join a company. New users should register with email or use company SSO.')}
                                    </div>
                                    <div style={{
                                        display: 'flex', alignItems: 'center', gap: '12px',
                                        margin: '20px 0', color: 'var(--text-tertiary)', fontSize: '11px'
                                    }}>
                                        <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                                        {t('auth.or', 'or')}
                                        <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                                    </div>
                                </>
                            )}
                        </div>
                    )}

                    {mfaFlow ? (
                        mfaFlow.stage === 'recovery' ? (
                            <div className="login-form" role="region" aria-label={mfaTitle}>
                                <div className="login-recovery-grid">
                                    {mfaFlow.recoveryCodes?.map((code) => (
                                        <code key={code}>{code}</code>
                                    ))}
                                </div>
                                <button className="login-secondary-action" type="button" onClick={() => void copyRecoveryCodes()}>
                                    {isZh ? '复制全部恢复码' : 'Copy all recovery codes'}
                                </button>
                                <label className="login-recovery-confirmation">
                                    <input
                                        type="checkbox"
                                        checked={mfaCodesSaved}
                                        onChange={(event) => setMfaCodesSaved(event.target.checked)}
                                    />
                                    <span>{isZh ? '我已将恢复码保存到安全位置' : 'I saved the recovery codes in a secure place'}</span>
                                </label>
                                <button
                                    className="login-submit"
                                    type="button"
                                    disabled={!mfaCodesSaved || loading}
                                    onClick={() => void finishRecoveryStep()}
                                >
                                    {isZh ? '进入产品' : 'Continue'}
                                    <IconArrowRight size={17} stroke={1.9} style={{ marginLeft: '6px' }} />
                                </button>
                            </div>
                        ) : (
                            <form onSubmit={handleMfaSubmit} className="login-form">
                                {mfaFlow.stage === 'setup' && (
                                    <div className="login-mfa-setup">
                                        <p>{isZh ? '在验证器应用中扫描或手动输入下面的密钥。密钥不会写入浏览器存储。' : 'Add the key below to your authenticator app. It is never written to browser storage.'}</p>
                                        <code>{mfaFlow.secret}</code>
                                        {mfaFlow.provisioningUri && (
                                            <a href={mfaFlow.provisioningUri}>{isZh ? '在验证器应用中打开' : 'Open in authenticator app'}</a>
                                        )}
                                    </div>
                                )}
                                <div className="login-field">
                                    <label>{mfaFlow.stage === 'setup'
                                        ? (isZh ? '6 位动态码' : '6-digit authenticator code')
                                        : (isZh ? '动态码或恢复码' : 'Authenticator or recovery code')}</label>
                                    <input
                                        type="text"
                                        value={mfaCode}
                                        onChange={(event) => setMfaCode(event.target.value)}
                                        required
                                        autoFocus
                                        inputMode="text"
                                        autoComplete="one-time-code"
                                        maxLength={64}
                                        placeholder={mfaFlow.stage === 'setup' ? '123456' : (isZh ? '123456 或 XXXX-XXXX-XXXX-XXXX' : '123456 or XXXX-XXXX-XXXX-XXXX')}
                                    />
                                </div>
                                <button className="login-submit" type="submit" disabled={loading || !mfaCode.trim()}>
                                    {loading ? <span className="login-spinner" /> : (
                                        <>
                                            {isZh ? '验证并继续' : 'Verify and continue'}
                                            <IconArrowRight size={17} stroke={1.9} style={{ marginLeft: '6px' }} />
                                        </>
                                    )}
                                </button>
                                <div className="login-verification-actions">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setMfaFlow(null);
                                            setMfaCode('');
                                            setForm((current) => ({ ...current, password: '' }));
                                            setError('');
                                        }}
                                        disabled={loading}
                                    >
                                        {isZh ? '重新登录' : 'Start over'}
                                    </button>
                                </div>
                            </form>
                        )
                    ) : showVerification ? (
                        <form onSubmit={handleVerifyEmail} className="login-form">
                            <div className="login-field">
                                <label>{isZh ? '邮箱验证凭证' : 'Verification token'}</label>
                                <input
                                    type="text"
                                    value={verificationCode}
                                    onChange={(e) => setVerificationCode(e.target.value)}
                                    required
                                    autoFocus
                                    inputMode="text"
                                    autoComplete="one-time-code"
                                    maxLength={512}
                                    placeholder={isZh ? '粘贴邮件中的验证凭证' : 'Paste the token from your email'}
                                />
                            </div>

                            <button className="login-submit" type="submit" disabled={loading || !verificationCode.trim()}>
                                {loading ? (
                                    <span className="login-spinner" />
                                ) : (
                                    <>
                                        {isZh ? '验证并继续' : 'Verify and continue'}
                                        <IconArrowRight size={17} stroke={1.9} style={{ marginLeft: '6px' }} />
                                    </>
                                )}
                            </button>

                            <div className="login-verification-actions">
                                <button type="button" onClick={handleResendVerification} disabled={loading}>
                                    {isZh ? '重新发送验证凭证' : 'Resend token'}
                                </button>
                                <button
                                    type="button"
                                    onClick={() => {
                                        setShowVerification(false);
                                        setVerificationCode('');
                                        setError('');
                                        setSuccessMessage('');
                                    }}
                                    disabled={loading}
                                >
                                    {isZh ? '返回' : 'Back'}
                                </button>
                            </div>
                        </form>
                    ) : (
                        <form onSubmit={handleSubmit} className="login-form">
                            <div className="login-field">
                                <label>{t('auth.email')}</label>
                                <input
                                    type="email"
                                    value={form.login_identifier}
                                    onChange={(e) => setForm({ ...form, login_identifier: e.target.value })}
                                    required
                                    autoFocus
                                    autoComplete="email"
                                    placeholder={t('auth.emailPlaceholder')}
                                />
                            </div>

                            <div className="login-field">
                                <label>{t('auth.password')}</label>
                                <input
                                    type="password"
                                    value={form.password}
                                    onChange={(e) => setForm({ ...form, password: e.target.value })}
                                    required
                                    autoComplete={isRegister ? 'new-password' : 'current-password'}
                                    placeholder={t('auth.passwordPlaceholder')}
                                />
                            </div>

                            {isRegister && registrationCodeRequired && (
                                <div className="login-field">
                                    <label>{isZh ? '平台注册凭证' : 'Platform registration grant'}</label>
                                    <input
                                        type="text"
                                        value={registrationCode}
                                        onChange={(e) => setRegistrationCode(e.target.value)}
                                        required
                                        placeholder={isZh ? '输入平台发放的注册凭证' : 'Enter a platform-issued registration grant'}
                                        autoComplete="off"
                                    />
                                    <div style={{ marginTop: '6px', fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>
                                        {isZh ? '只用于创建全局账号，不会授予任何公司的成员或管理员权限。' : 'This creates only a global account and grants no company membership or admin role.'}
                                    </div>
                                </div>
                            )}

                            {organizationInvitationToken && (
                                <div className="login-info-banner" role="status">
                                    {isZh ? '账号创建或登录完成后，你会先查看并明确接受公司邀请；公司邀请不会替代平台注册凭证。' : 'After signup or login, you will review and explicitly accept the company invitation. It never replaces the platform registration grant.'}
                                </div>
                            )}

                            {!isRegister && (
                                <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '-4px', marginBottom: '8px' }}>
                                    <Link
                                        to="/forgot-password"
                                        style={{ fontSize: '13px', color: 'var(--accent-primary)', textDecoration: 'none' }}
                                    >
                                        {t('auth.forgotPassword', 'Forgot password?')}
                                    </Link>
                                </div>
                            )}

                            <button
                                className="login-submit"
                                type="submit"
                                disabled={loading || (isRegister && passwordRegistrationAvailable === false)}
                            >
                                {loading ? (
                                    <span className="login-spinner" />
                                ) : (
                                    <>
                                        {isRegister ? t('auth.register') : t('auth.login')}
                                        <IconArrowRight size={17} stroke={1.9} style={{ marginLeft: '6px' }} />
                                    </>
                                )}
                            </button>
                        </form>
                    )}

                    {/* Multi-tenant selection modal */}
                    {tenantSelection && (
                        <div style={{
                            position: 'fixed',
                            top: 0, left: 0, right: 0, bottom: 0,
                            background: 'rgba(17, 17, 20, 0.28)',
                            backdropFilter: 'blur(8px)',
                            WebkitBackdropFilter: 'blur(8px)',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            zIndex: 2000,
                        }}>
                            <div style={{
                                background: '#fbfbfa',
                                borderRadius: '16px',
                                padding: '32px',
                                maxWidth: '400px',
                                width: '90%',
                                maxHeight: 'min(620px, calc(100vh - 64px))',
                                border: '1px solid rgba(17, 17, 20, 0.1)',
                                boxShadow: '0 24px 80px rgba(17,17,20,0.18), 0 0 0 1px rgba(255,255,255,0.55) inset',
                                display: 'flex',
                                flexDirection: 'column',
                            }}>
                                <h3 style={{ fontSize: '18px', fontWeight: 700, marginBottom: '8px', color: '#17171a' }}>
                                    {t('auth.selectOrganization', '选择公司')}
                                </h3>
                                <p style={{ fontSize: '13px', color: '#767681', marginBottom: '20px', lineHeight: '1.5' }}>
                                    {t('auth.multiTenantPrompt', '该邮箱对应多个公司，请选择要登录的公司：')}
                                </p>
                                <div style={{
                                    display: 'flex',
                                    flexDirection: 'column',
                                    gap: '8px',
                                    maxHeight: '216px',
                                    overflowY: 'auto',
                                    paddingRight: '4px',
                                    marginRight: '-4px',
                                }}>
                                    {tenantSelection.map((tenant: any) => (
                                        <button
                                            key={tenant.tenant_id}
                                            onClick={() => handleTenantSelect(tenant.tenant_id)}
                                            style={{
                                                padding: '12px 16px',
                                                borderRadius: '10px',
                                                border: '1px solid rgba(17,17,20,0.1)',
                                                background: '#ffffff',
                                                color: '#2b2b31',
                                                fontSize: '14px',
                                                fontWeight: 500,
                                                cursor: 'pointer',
                                                textAlign: 'left',
                                                transition: 'background 0.15s, border-color 0.15s',
                                            }}
                                            onMouseEnter={e => {
                                                (e.currentTarget as HTMLButtonElement).style.background = '#f2f2f0';
                                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'rgba(17,17,20,0.2)';
                                            }}
                                            onMouseLeave={e => {
                                                (e.currentTarget as HTMLButtonElement).style.background = '#ffffff';
                                                (e.currentTarget as HTMLButtonElement).style.borderColor = 'rgba(17,17,20,0.1)';
                                            }}
                                        >
                                            {tenant.tenant_name} {tenant.tenant_slug && `(${tenant.tenant_slug})`}
                                        </button>
                                    ))}
                                </div>
                                {/* Create or Join Organization */}
                                <button
                                    onClick={async () => {
                                        // Log in with the first tenant to get a valid token, then redirect to company setup
                                        try {
                                            setLoading(true);
                                            const firstTenant = tenantSelection[0];
                                            const res = await authApi.login({
                                                login_identifier: form.login_identifier,
                                                password: form.password,
                                                tenant_id: firstTenant.tenant_id,
                                            });
                                            setTenantSelection(null);
                                            await handleLoginResult(res, '/setup-company?from=tenant-selection');
                                        } catch (err: any) {
                                            setError(err.message || 'Failed');
                                            setTenantSelection(null);
                                        } finally {
                                            setLoading(false);
                                        }
                                    }}
                                    style={{
                                        marginTop: '8px',
                                        padding: '12px 16px',
                                        borderRadius: '10px',
                                        border: '1px dashed rgba(17,17,20,0.18)',
                                        background: 'transparent',
                                        color: '#8c8c96',
                                        fontSize: '14px',
                                        cursor: 'pointer',
                                        textAlign: 'left',
                                        transition: 'border-color 0.15s, color 0.15s',
                                    }}
                                    onMouseEnter={e => {
                                        (e.currentTarget as HTMLButtonElement).style.borderColor = 'rgba(17,17,20,0.32)';
                                        (e.currentTarget as HTMLButtonElement).style.color = '#4f4f58';
                                    }}
                                    onMouseLeave={e => {
                                        (e.currentTarget as HTMLButtonElement).style.borderColor = 'rgba(17,17,20,0.18)';
                                        (e.currentTarget as HTMLButtonElement).style.color = '#8c8c96';
                                    }}
                                >
                                    {t('auth.createOrJoinOrganization', 'Create or Join Organization')}
                                </button>
                                <button
                                    onClick={() => setTenantSelection(null)}
                                    style={{
                                        marginTop: '16px',
                                        padding: '10px 16px',
                                        borderRadius: '10px',
                                        border: '1px solid rgba(17,17,20,0.1)',
                                        background: '#f3f3f1',
                                        color: '#6f6f79',
                                        fontSize: '14px',
                                        fontWeight: 500,
                                        cursor: 'pointer',
                                        width: '100%',
                                        transition: 'background 0.15s, color 0.15s',
                                    }}
                                    onMouseEnter={e => {
                                        (e.currentTarget as HTMLButtonElement).style.background = '#e9e9e6';
                                        (e.currentTarget as HTMLButtonElement).style.color = '#2b2b31';
                                    }}
                                    onMouseLeave={e => {
                                        (e.currentTarget as HTMLButtonElement).style.background = '#f3f3f1';
                                        (e.currentTarget as HTMLButtonElement).style.color = '#6f6f79';
                                    }}
                                >
                                    {t('common.cancel', 'Cancel')}
                                </button>
                            </div>
                        </div>
                    )}

                    {!showVerification && !mfaFlow && (
                    <div className="login-switch">
                        {isRegister ? t('auth.hasAccount') : t('auth.noAccount')}{' '}
                        <a href="#" onClick={(e) => { e.preventDefault(); setIsRegister(!isRegister); setError(''); }}>
                            {isRegister ? t('auth.goLogin') : t('auth.goRegister')}
                        </a>
                    </div>
                    )}
                    </div>
                </div>
            </div>
        </AtlasFrame>
    );
}
