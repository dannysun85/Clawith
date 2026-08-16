import { IconInfoCircle, IconLock, IconMail, IconShieldCheck } from '@tabler/icons-react';
import { useEffect, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { fetchJson } from '../services/api';

type SystemEmailConfig = {
    SYSTEM_EMAIL_ENABLED: boolean;
    SYSTEM_EMAIL_FROM_ADDRESS: string;
    SYSTEM_EMAIL_FROM_NAME: string;
    SYSTEM_SMTP_HOST: string;
    SYSTEM_SMTP_PORT: number;
    SYSTEM_SMTP_USERNAME: string;
    SYSTEM_SMTP_PASSWORD: string;
    SYSTEM_SMTP_SSL: boolean;
    SYSTEM_SMTP_TIMEOUT_SECONDS: number;
};

type SystemEmailSettingResponse = {
    key: string;
    value?: Partial<SystemEmailConfig> & { _configured_secret_fields?: string[] };
    updated_at?: string | null;
};

const CONFIGURED_SECRET_PLACEHOLDER = '••••••••';

const DEFAULT_CONFIG: SystemEmailConfig = {
    SYSTEM_EMAIL_ENABLED: false,
    SYSTEM_EMAIL_FROM_ADDRESS: '',
    SYSTEM_EMAIL_FROM_NAME: 'Astra',
    SYSTEM_SMTP_HOST: '',
    SYSTEM_SMTP_PORT: 465,
    SYSTEM_SMTP_USERNAME: '',
    SYSTEM_SMTP_PASSWORD: '',
    SYSTEM_SMTP_SSL: true,
    SYSTEM_SMTP_TIMEOUT_SECONDS: 15,
};

const messageFrom = (error: unknown) => error instanceof Error ? error.message : String(error);

const configurationReady = (config: SystemEmailConfig) => Boolean(
    config.SYSTEM_EMAIL_FROM_ADDRESS
    && config.SYSTEM_SMTP_HOST
    && config.SYSTEM_SMTP_USERNAME
    && config.SYSTEM_SMTP_PASSWORD,
);

function hydrateConfig(value: SystemEmailSettingResponse['value']): SystemEmailConfig {
    return {
        SYSTEM_EMAIL_ENABLED: value?.SYSTEM_EMAIL_ENABLED === true,
        SYSTEM_EMAIL_FROM_ADDRESS: String(value?.SYSTEM_EMAIL_FROM_ADDRESS || ''),
        SYSTEM_EMAIL_FROM_NAME: String(value?.SYSTEM_EMAIL_FROM_NAME || 'Astra'),
        SYSTEM_SMTP_HOST: String(value?.SYSTEM_SMTP_HOST || ''),
        SYSTEM_SMTP_PORT: Number(value?.SYSTEM_SMTP_PORT || 465),
        SYSTEM_SMTP_USERNAME: String(value?.SYSTEM_SMTP_USERNAME || ''),
        SYSTEM_SMTP_PASSWORD: String(value?.SYSTEM_SMTP_PASSWORD || ''),
        SYSTEM_SMTP_SSL: value?.SYSTEM_SMTP_SSL !== false,
        SYSTEM_SMTP_TIMEOUT_SECONDS: Number(value?.SYSTEM_SMTP_TIMEOUT_SECONDS || 15),
    };
}

export default function PlatformSystemEmail() {
    const { i18n } = useTranslation();
    const isChinese = i18n.language.startsWith('zh');
    const [config, setConfig] = useState<SystemEmailConfig>(DEFAULT_CONFIG);
    const [updatedAt, setUpdatedAt] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [loadError, setLoadError] = useState('');
    const [saveResult, setSaveResult] = useState<{ ok: boolean; message: string } | null>(null);
    const [savedConfigurationReady, setSavedConfigurationReady] = useState(false);
    const [testRecipient, setTestRecipient] = useState('');
    const [sendingTest, setSendingTest] = useState(false);
    const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);

    useEffect(() => {
        let active = true;
        setLoading(true);
        setLoadError('');
        fetchJson<SystemEmailSettingResponse>('/enterprise/system-settings/system_email_platform')
            .then((response) => {
                if (!active) return;
                const hydrated = hydrateConfig(response.value);
                setConfig(hydrated);
                setSavedConfigurationReady(configurationReady(hydrated));
                setUpdatedAt(response.updated_at || null);
            })
            .catch((error) => {
                if (active) setLoadError(messageFrom(error));
            })
            .finally(() => {
                if (active) setLoading(false);
            });
        return () => { active = false; };
    }, []);

    const updateConfig = <Key extends keyof SystemEmailConfig>(key: Key, value: SystemEmailConfig[Key]) => {
        setConfig((current) => ({ ...current, [key]: value }));
        setSaveResult(null);
        setTestResult(null);
    };

    const saveConfig = async (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        setSaving(true);
        setSaveResult(null);
        setTestResult(null);
        try {
            const response = await fetchJson<SystemEmailSettingResponse>(
                '/enterprise/system-settings/system_email_platform',
                {
                    method: 'PUT',
                    body: JSON.stringify({ value: config }),
                },
            );
            const hydrated = hydrateConfig(response.value);
            setConfig(hydrated);
            setSavedConfigurationReady(configurationReady(hydrated));
            setUpdatedAt(response.updated_at || null);
            setSaveResult({
                ok: true,
                message: isChinese ? '配置已安全保存。' : 'Configuration saved securely.',
            });
        } catch (error) {
            setSaveResult({
                ok: false,
                message: isChinese ? `保存失败：${messageFrom(error)}` : `Save failed: ${messageFrom(error)}`,
            });
        } finally {
            setSaving(false);
        }
    };

    const sendTestEmail = async (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        const email = testRecipient.trim();
        if (!email || !savedConfigurationReady) return;
        setSendingTest(true);
        setTestResult(null);
        try {
            const response = await fetchJson<{
                success: boolean;
                evidence_level: 'smtp_accepted';
                recipient: string;
                message: string;
            }>('/enterprise/system-email/test', {
                method: 'POST',
                body: JSON.stringify({ email }),
            });
            if (!response.success || response.evidence_level !== 'smtp_accepted') {
                throw new Error(isChinese ? 'SMTP 测试没有返回可验证的接受回执。' : 'The SMTP test returned no verifiable acceptance receipt.');
            }
            setTestResult({
                ok: true,
                message: isChinese
                    ? `SMTP 服务器已接受发往 ${email} 的测试邮件（不代表对方已收件或已读）。`
                    : `The SMTP server accepted the test message for ${email}; inbox delivery is not yet proven.`,
            });
        } catch (error) {
            setTestResult({
                ok: false,
                message: isChinese ? `发送失败：${messageFrom(error)}` : `Send failed: ${messageFrom(error)}`,
            });
        } finally {
            setSendingTest(false);
        }
    };

    const secretConfigured = config.SYSTEM_SMTP_PASSWORD === CONFIGURED_SECRET_PLACEHOLDER;
    const isConfigured = configurationReady(config);

    return (
        <>
            <header className="console-page-header">
                <div>
                    <h1>{isChinese ? '系统邮件' : 'System email'}</h1>
                    <p>
                        {isChinese
                            ? '配置平台统一发件身份，用于注册验证、密码重置、公司邀请和系统通知。'
                            : 'Configure the platform sender used for registration verification, password resets, company invitations, and system notifications.'}
                    </p>
                </div>
                <span className={`console-status ${config.SYSTEM_EMAIL_ENABLED ? 'console-status--active' : ''}`}>
                    {loading
                        ? (isChinese ? '读取中' : 'Loading')
                        : config.SYSTEM_EMAIL_ENABLED
                            ? (isChinese ? '已启用' : 'Enabled')
                            : (isChinese ? '未启用' : 'Disabled')}
                </span>
            </header>

            <div className="console-inline-notice">
                <IconShieldCheck size={19} />
                <span>
                    {isChinese
                        ? '这是平台级凭据，不属于任何一家公司。SMTP 密码由后端加密保存，页面重新读取时只显示占位符。'
                        : 'These platform credentials do not belong to any company. The SMTP password is encrypted by the backend and is returned to this page only as a placeholder.'}
                </span>
            </div>

            {loadError && <div className="console-inline-notice console-inline-notice--error" role="alert" style={{ marginTop: 16 }}>{loadError}</div>}

            <section className="console-card console-card--wide" data-testid="system-email-configuration" style={{ marginTop: 16 }}>
                <div className="console-page-header" style={{ marginBottom: 16 }}>
                    <div>
                        <h2>{isChinese ? 'SMTP 发件配置' : 'SMTP sender configuration'}</h2>
                        <p>
                            {isChinese
                                ? '建议先关闭系统邮件并保存，成功发送且确认收件后再启用，避免注册和邀请流程被错误配置阻断。'
                                : 'Save and test with system email disabled first, then enable it only after inbox delivery is confirmed.'}
                        </p>
                    </div>
                    {updatedAt && <small>{isChinese ? '上次保存' : 'Last saved'} {new Date(updatedAt).toLocaleString()}</small>}
                </div>

                <form className="console-form" onSubmit={saveConfig}>
                    <label className="console-check">
                        <input
                            type="checkbox"
                            checked={config.SYSTEM_EMAIL_ENABLED}
                            onChange={(event) => updateConfig('SYSTEM_EMAIL_ENABLED', event.target.checked)}
                            disabled={loading || saving}
                        />
                        <span>
                            <strong>{isChinese ? '启用系统邮件' : 'Enable system email'}</strong>
                            <small>
                                {isChinese
                                    ? '启用后，新邮箱/密码注册需要邮件验证，公司邮箱邀请和密码重置也依赖该通道。'
                                    : 'When enabled, email/password registration requires verification, and invitations and password resets depend on this channel.'}
                            </small>
                        </span>
                    </label>

                    <div className="console-form-row">
                        <label>
                            {isChinese ? '发件邮箱' : 'From email address'}
                            <input
                                type="email"
                                required
                                autoComplete="email"
                                value={config.SYSTEM_EMAIL_FROM_ADDRESS}
                                onChange={(event) => updateConfig('SYSTEM_EMAIL_FROM_ADDRESS', event.target.value)}
                                placeholder="noreply@example.com"
                                disabled={loading || saving}
                            />
                        </label>
                        <label>
                            {isChinese ? '发件人名称' : 'From name'}
                            <input
                                required
                                value={config.SYSTEM_EMAIL_FROM_NAME}
                                onChange={(event) => updateConfig('SYSTEM_EMAIL_FROM_NAME', event.target.value)}
                                placeholder="Astra"
                                disabled={loading || saving}
                            />
                        </label>
                    </div>

                    <div className="console-form-row">
                        <label>
                            SMTP Host
                            <input
                                required
                                value={config.SYSTEM_SMTP_HOST}
                                onChange={(event) => updateConfig('SYSTEM_SMTP_HOST', event.target.value)}
                                placeholder="smtp.example.com"
                                spellCheck={false}
                                disabled={loading || saving}
                            />
                        </label>
                        <label>
                            SMTP Port
                            <input
                                type="number"
                                required
                                min={1}
                                max={65535}
                                value={config.SYSTEM_SMTP_PORT}
                                onChange={(event) => updateConfig('SYSTEM_SMTP_PORT', Number(event.target.value))}
                                disabled={loading || saving}
                            />
                        </label>
                    </div>

                    <div className="console-form-row">
                        <label>
                            {isChinese ? 'SMTP 用户名' : 'SMTP username'}
                            <input
                                required
                                autoComplete="username"
                                value={config.SYSTEM_SMTP_USERNAME}
                                onChange={(event) => updateConfig('SYSTEM_SMTP_USERNAME', event.target.value)}
                                placeholder="noreply@example.com"
                                spellCheck={false}
                                disabled={loading || saving}
                            />
                        </label>
                        <label>
                            {isChinese ? 'SMTP 密码' : 'SMTP password'}
                            <input
                                type="password"
                                required
                                autoComplete="new-password"
                                value={config.SYSTEM_SMTP_PASSWORD}
                                onChange={(event) => updateConfig('SYSTEM_SMTP_PASSWORD', event.target.value)}
                                placeholder={isChinese ? '输入邮箱密码或 App Password' : 'Enter mailbox password or app password'}
                                disabled={loading || saving}
                            />
                            <small>
                                <IconLock size={13} style={{ verticalAlign: -2, marginRight: 4 }} />
                                {secretConfigured
                                    ? (isChinese ? '已配置；保留占位符即不会更改密码。' : 'Configured; leave the placeholder unchanged to keep the current password.')
                                    : (isChinese ? '保存后页面不会再显示明文。' : 'The plaintext is never returned after saving.')}
                            </small>
                        </label>
                    </div>

                    <div className="console-form-row">
                        <label>
                            {isChinese ? '超时（秒）' : 'Timeout (seconds)'}
                            <input
                                type="number"
                                required
                                min={1}
                                max={120}
                                value={config.SYSTEM_SMTP_TIMEOUT_SECONDS}
                                onChange={(event) => updateConfig('SYSTEM_SMTP_TIMEOUT_SECONDS', Number(event.target.value))}
                                disabled={loading || saving}
                            />
                        </label>
                        <label className="console-check" style={{ alignSelf: 'end' }}>
                            <input
                                type="checkbox"
                                checked={config.SYSTEM_SMTP_SSL}
                                onChange={(event) => updateConfig('SYSTEM_SMTP_SSL', event.target.checked)}
                                disabled={loading || saving}
                            />
                            <span>
                                <strong>SSL/TLS</strong>
                                <small>{isChinese ? '直连 TLS 通常使用 465 端口。' : 'Implicit TLS commonly uses port 465.'}</small>
                            </span>
                        </label>
                    </div>

                    <div className="console-row-actions">
                        <button className="btn btn-primary" type="submit" disabled={loading || saving}>
                            {saving ? (isChinese ? '保存中…' : 'Saving…') : (isChinese ? '保存配置' : 'Save configuration')}
                        </button>
                        <span className="console-status">
                            {isConfigured ? (isChinese ? 'SMTP 字段已齐全' : 'SMTP fields complete') : (isChinese ? '待配置' : 'Configuration required')}
                        </span>
                    </div>
                    {saveResult && (
                        <div className={`console-inline-notice ${saveResult.ok ? 'console-inline-notice--success' : 'console-inline-notice--error'}`} role={saveResult.ok ? 'status' : 'alert'}>
                            {saveResult.ok ? <IconShieldCheck size={18} /> : <IconInfoCircle size={18} />}
                            <span>{saveResult.message}</span>
                        </div>
                    )}
                </form>
            </section>

            <section className="console-card console-card--wide" data-testid="system-email-test" style={{ marginTop: 16 }}>
                <h2><IconMail size={18} style={{ verticalAlign: -4, marginRight: 7 }} />{isChinese ? '发送测试邮件' : 'Send a test email'}</h2>
                <p>
                    {isChinese
                        ? '测试使用上一次已保存的服务器配置；未保存的表单修改不会参与发送。SMTP 接受成功与收件箱实际到达会分开报告。'
                        : 'The test uses the last saved server configuration. Unsaved edits are not used, and SMTP acceptance is reported separately from inbox delivery.'}
                </p>
                <form className="console-form" onSubmit={sendTestEmail} style={{ marginTop: 14 }}>
                    <div className="console-form-row">
                        <label>
                            {isChinese ? '测试收件邮箱' : 'Test recipient'}
                            <input
                                type="email"
                                required
                                autoComplete="off"
                                value={testRecipient}
                                onChange={(event) => { setTestRecipient(event.target.value); setTestResult(null); }}
                                placeholder="name@example.com"
                                disabled={sendingTest}
                            />
                        </label>
                    </div>
                    <div className="console-row-actions">
                        <button className="btn btn-secondary" type="submit" disabled={loading || sendingTest || !testRecipient.trim() || !savedConfigurationReady}>
                            {sendingTest ? (isChinese ? '发送中…' : 'Sending…') : (isChinese ? '发送测试邮件' : 'Send test email')}
                        </button>
                        {!loading && !savedConfigurationReady && (
                            <span className="console-status">
                                {isChinese ? '请先保存完整 SMTP 配置' : 'Save a complete SMTP configuration first'}
                            </span>
                        )}
                    </div>
                    {testResult && (
                        <div className={`console-inline-notice ${testResult.ok ? 'console-inline-notice--success' : 'console-inline-notice--error'}`} role={testResult.ok ? 'status' : 'alert'}>
                            {testResult.ok ? <IconMail size={18} /> : <IconInfoCircle size={18} />}
                            <span>{testResult.message}</span>
                        </div>
                    )}
                </form>
            </section>
        </>
    );
}
