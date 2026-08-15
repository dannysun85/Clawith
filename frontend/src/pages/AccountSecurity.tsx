import {
    IconAlertTriangle,
    IconArrowLeft,
    IconCheck,
    IconCopy,
    IconKey,
    IconRefresh,
    IconShieldLock,
} from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';

import { AstraWordmark } from '../components/atlas';
import { authApi, type MfaSetupPayload, type MfaStatus } from '../services/api';
import { useAuthStore } from '../stores';
import './productSurfaces.css';
import './accountSecurity.css';

const messageFrom = (error: unknown) => error instanceof Error ? error.message : String(error);

export default function AccountSecurity() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const { user, setAuth, logout } = useAuthStore();
    const isChinese = i18n.language.startsWith('zh');
    const [status, setStatus] = useState<MfaStatus | null>(null);
    const [setup, setSetup] = useState<MfaSetupPayload | null>(null);
    const [setupPassword, setSetupPassword] = useState('');
    const [setupCode, setSetupCode] = useState('');
    const [mutationPassword, setMutationPassword] = useState('');
    const [mutationCode, setMutationCode] = useState('');
    const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
    const [codesSaved, setCodesSaved] = useState(false);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [success, setSuccess] = useState('');
    const reason = searchParams.get('reason');

    const loadStatus = async () => {
        try {
            setStatus(await authApi.mfaStatus());
        } catch (nextError) {
            setError(messageFrom(nextError));
        }
    };

    useEffect(() => { void loadStatus(); }, []);

    const startSetup = async (event: React.FormEvent) => {
        event.preventDefault();
        setBusy('setup-start'); setError(''); setSuccess('');
        try {
            setSetup(await authApi.startMfaSetup(setupPassword));
            setSetupPassword('');
        } catch (nextError) {
            setError(messageFrom(nextError));
        } finally {
            setBusy('');
        }
    };

    const confirmSetup = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!setup || !setupCode.trim()) return;
        setBusy('setup-confirm'); setError(''); setSuccess('');
        try {
            const result = await authApi.confirmMfaSetup(setup.challenge_token, setupCode.trim());
            await setAuth(result.user, result.access_token);
            setRecoveryCodes(result.recovery_codes || []);
            setCodesSaved(false);
            setSetup(null);
            setSetupCode('');
            setStatus(await authApi.mfaStatus());
        } catch (nextError) {
            setError(messageFrom(nextError));
        } finally {
            setBusy('');
        }
    };

    const rotateCodes = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!user) return;
        setBusy('rotate'); setError(''); setSuccess('');
        try {
            const result = await authApi.rotateMfaRecoveryCodes(mutationPassword, mutationCode.trim());
            await setAuth(user, result.access_token);
            setRecoveryCodes(result.recovery_codes);
            setCodesSaved(false);
            setMutationPassword('');
            setMutationCode('');
            setStatus(await authApi.mfaStatus());
        } catch (nextError) {
            setError(messageFrom(nextError));
        } finally {
            setBusy('');
        }
    };

    const disableMfa = async () => {
        if (!user || !status || status.required) return;
        if (!window.confirm(isChinese ? '确认关闭多因素验证？现有恢复码会全部失效。' : 'Disable MFA? Existing recovery codes will be revoked.')) return;
        setBusy('disable'); setError(''); setSuccess('');
        try {
            const result = await authApi.disableMfa(mutationPassword, mutationCode.trim());
            if (!result.access_token) {
                logout();
                navigate('/login', { replace: true });
                return;
            }
            await setAuth(user, result.access_token);
            setMutationPassword('');
            setMutationCode('');
            setStatus(await authApi.mfaStatus());
            setSuccess(isChinese ? '多因素验证已关闭。' : 'Multi-factor authentication is disabled.');
        } catch (nextError) {
            setError(messageFrom(nextError));
        } finally {
            setBusy('');
        }
    };

    const copyRecoveryCodes = async () => {
        try {
            await navigator.clipboard.writeText(recoveryCodes.join('\n'));
            setSuccess(isChinese ? '恢复码已复制。' : 'Recovery codes copied.');
        } catch {
            setError(isChinese ? '无法自动复制，请手动保存。' : 'Copy failed. Save the codes manually.');
        }
    };

    const startFreshLogin = () => {
        logout();
        navigate('/login', { replace: true });
    };

    return (
        <main className="company-access account-security">
            <header className="company-access__topbar">
                <AstraWordmark height={23} variant="ui" />
                <button type="button" className="btn btn-secondary" onClick={() => navigate('/account/companies')}>
                    <IconArrowLeft size={16} /> {isChinese ? '公司与邀请' : 'Companies & invitations'}
                </button>
            </header>
            <section className="company-access__intro">
                <span className="surface-eyebrow">{isChinese ? '全局身份安全' : 'Global identity security'}</span>
                <h1>{isChinese ? '多因素验证与恢复码' : 'Multi-factor authentication & recovery'}</h1>
                <p>{isChinese ? 'MFA 绑定到自然人 Identity，而不是某一家公司的 membership。启用后，切换公司仍沿用同一套验证器与恢复码。' : 'MFA belongs to your global Identity, not one company membership. The same authenticator and recovery codes protect every company context.'}</p>
            </section>

            {error && <div className="surface-alert surface-alert--error" role="alert"><IconAlertTriangle size={19} /><span>{error}</span></div>}
            {success && <div className="surface-alert surface-alert--success" role="status"><IconCheck size={19} /><span>{success}</span></div>}
            {reason === 'mfa_challenge_required' && (
                <div className="surface-alert surface-alert--error" role="alert">
                    <IconShieldLock size={19} />
                    <span><strong>{isChinese ? '当前会话尚未完成 MFA' : 'This session has not completed MFA'}</strong>{isChinese ? '请重新登录，并在登录流程中输入动态码或恢复码。' : 'Sign in again and enter an authenticator or recovery code.'}</span>
                    <button type="button" className="btn btn-secondary" onClick={startFreshLogin}>{isChinese ? '重新登录' : 'Sign in again'}</button>
                </div>
            )}

            <div className="company-access__grid">
                <section className="surface-card account-security__status">
                    <header><span className="surface-card__icon"><IconShieldLock size={21} /></span><div><h2>{isChinese ? '当前安全状态' : 'Current security posture'}</h2><p>{status?.enabled ? (isChinese ? '已启用 TOTP 多因素验证' : 'TOTP MFA is enabled') : (isChinese ? '尚未启用多因素验证' : 'MFA is not enabled')}</p></div></header>
                    <dl>
                        <div><dt>{isChinese ? '角色策略' : 'Role policy'}</dt><dd>{status?.required ? (isChinese ? '强制启用' : 'Required') : (isChinese ? '可选启用' : 'Optional')}</dd></div>
                        <div><dt>{isChinese ? '剩余恢复码' : 'Recovery codes left'}</dt><dd>{status?.recovery_codes_remaining ?? '—'}</dd></div>
                        <div><dt>{isChinese ? '启用时间' : 'Enabled at'}</dt><dd>{status?.confirmed_at ? new Date(status.confirmed_at).toLocaleString() : '—'}</dd></div>
                    </dl>
                </section>

                {!status?.enabled && !setup && (
                    <section className="surface-card account-security__action">
                        <header><span className="surface-card__icon"><IconKey size={21} /></span><div><h2>{isChinese ? '绑定验证器' : 'Bind an authenticator'}</h2><p>{isChinese ? '先重新验证当前密码，再生成一次性的绑定密钥。' : 'Re-enter your password before a one-time setup key is generated.'}</p></div></header>
                        <form className="surface-form" onSubmit={startSetup}>
                            <label>{isChinese ? '当前密码' : 'Current password'}<input type="password" value={setupPassword} onChange={(event) => setSetupPassword(event.target.value)} autoComplete="current-password" required /></label>
                            <button type="submit" className="btn btn-primary" disabled={busy === 'setup-start'}>{isChinese ? '开始设置' : 'Start setup'}</button>
                        </form>
                    </section>
                )}

                {setup && (
                    <section className="surface-card account-security__action">
                        <header><span className="surface-card__icon"><IconKey size={21} /></span><div><h2>{isChinese ? '验证绑定' : 'Confirm setup'}</h2><p>{isChinese ? '将密钥加入验证器，再输入当前 6 位动态码。' : 'Add the key to your authenticator, then enter the current 6-digit code.'}</p></div></header>
                        <div className="account-security__secret"><code>{setup.secret}</code><a href={setup.provisioning_uri}>{isChinese ? '在验证器应用中打开' : 'Open in authenticator app'}</a></div>
                        <form className="surface-form" onSubmit={confirmSetup}>
                            <label>{isChinese ? '6 位动态码' : '6-digit code'}<input type="text" value={setupCode} onChange={(event) => setSetupCode(event.target.value)} autoComplete="one-time-code" inputMode="numeric" maxLength={6} required /></label>
                            <div className="console-row-actions"><button type="submit" className="btn btn-primary" disabled={busy === 'setup-confirm'}>{isChinese ? '确认并启用' : 'Confirm and enable'}</button><button type="button" className="btn btn-ghost" onClick={() => { setSetup(null); setSetupCode(''); }}>{isChinese ? '取消' : 'Cancel'}</button></div>
                        </form>
                    </section>
                )}

                {status?.enabled && (
                    <section className="surface-card account-security__action">
                        <header><span className="surface-card__icon"><IconRefresh size={21} /></span><div><h2>{isChinese ? '恢复与变更' : 'Recovery & changes'}</h2><p>{isChinese ? '轮换恢复码需要当前密码，以及一枚当前动态码或恢复码。旧恢复码会立即全部失效。' : 'Rotating codes requires your password plus a current authenticator or recovery code. All old codes are revoked immediately.'}</p></div></header>
                        <form className="surface-form" onSubmit={rotateCodes}>
                            <label>{isChinese ? '当前密码' : 'Current password'}<input type="password" value={mutationPassword} onChange={(event) => setMutationPassword(event.target.value)} autoComplete="current-password" required /></label>
                            <label>{isChinese ? '动态码或恢复码' : 'Authenticator or recovery code'}<input type="text" value={mutationCode} onChange={(event) => setMutationCode(event.target.value)} autoComplete="one-time-code" maxLength={64} required /></label>
                            <div className="console-row-actions"><button type="submit" className="btn btn-secondary" disabled={busy === 'rotate'}>{isChinese ? '轮换恢复码' : 'Rotate recovery codes'}</button>{!status.required && <button type="button" className="btn btn-ghost account-security__danger" disabled={busy === 'disable' || !mutationPassword || !mutationCode.trim()} onClick={() => void disableMfa()}>{isChinese ? '关闭 MFA' : 'Disable MFA'}</button>}</div>
                            {status.required && <small>{isChinese ? '你的当前角色强制要求 MFA，因此不能关闭；可随时轮换恢复码。' : 'Your current role requires MFA, so it cannot be disabled. Recovery codes can still be rotated.'}</small>}
                        </form>
                    </section>
                )}

                {recoveryCodes.length > 0 && (
                    <section className="surface-card account-security__recovery">
                        <header><span className="surface-card__icon"><IconKey size={21} /></span><div><h2>{isChinese ? '一次性恢复码' : 'One-time recovery codes'}</h2><p>{isChinese ? '每枚只能使用一次，关闭此区域后不能再次查看明文。' : 'Each code works once. Plaintext cannot be viewed again after this panel closes.'}</p></div></header>
                        <div className="account-security__codes">{recoveryCodes.map((code) => <code key={code}>{code}</code>)}</div>
                        <button type="button" className="btn btn-secondary" onClick={() => void copyRecoveryCodes()}><IconCopy size={15} /> {isChinese ? '复制全部' : 'Copy all'}</button>
                        <label className="account-security__confirmation"><input type="checkbox" checked={codesSaved} onChange={(event) => setCodesSaved(event.target.checked)} /><span>{isChinese ? '我已保存到安全位置' : 'I saved these codes securely'}</span></label>
                        <button type="button" className="btn btn-primary" disabled={!codesSaved} onClick={() => { setRecoveryCodes([]); setCodesSaved(false); setSuccess(''); }}>{isChinese ? '完成' : 'Done'}</button>
                    </section>
                )}
            </div>
        </main>
    );
}
