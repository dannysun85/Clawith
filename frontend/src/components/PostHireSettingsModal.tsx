import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconClock, IconLink, IconShieldCheck, IconX } from '@tabler/icons-react';
import { agentApi, fetchJson } from '../services/api';
import { translateTemplate } from '../i18n/templateTranslations';
import { useDialog } from './Dialog/DialogProvider';
import TierSelector, { type SaasTier } from './TierSelector';
import { canonicalizeModalities, MODALITIES } from '../constants/modalities';
import { SUBSCRIPTION_UPGRADE_PATH } from '../hooks/useAgentCreationLimit';

interface Template {
    id: string;
    name: string;
    description?: string;
    icon?: string;
    category?: string;
    role_key?: string | null;
    role_revision?: number;
    lifecycle_status?: string;
    limitations?: string[];
    deliverables?: string[];
    source_provenance?: Record<string, unknown>;
    capability_contract?: {
        contract_ready?: boolean;
        skills?: Array<{ name: string; status: string }>;
        tools?: Array<{ name: string; status: string }>;
        mcp_servers?: Array<{ server_id: string; status: string }>;
    };
}

interface Props {
    template: Template | null;
    open: boolean;
    // User cancelled the settings step — close this modal, but keep the caller
    // (e.g. the Talent Market grid) open so they can pick again.
    onClose: () => void;
    // Creation succeeded — caller should close too. Navigation is handled here.
    onDone?: () => void;
}

type Visibility = 'company' | 'only_me' | 'custom';

const DOUYIN_TEMPLATE_NAME = 'Douyin Operations Manager';

export default function PostHireSettingsModal({ template, open, onClose, onDone }: Props) {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const dialog = useDialog();
    const isChinese = i18n.language.startsWith('zh');

    const [visibility, setVisibility] = useState<Visibility>('company');
    const [preferredTier, setPreferredTier] = useState<SaasTier>('pro');
    const [preferredModality, setPreferredModality] = useState('text');
    const isDouyinTemplate = template?.name === DOUYIN_TEMPLATE_NAME;

    const { data: entitlements } = useQuery({
        queryKey: ['subscription-entitlements'],
        queryFn: () => fetchJson<any | null>('/subscription/my-entitlements'),
        enabled: open,
        staleTime: 5 * 60 * 1000,
    });
    const allowedTiers = useMemo(
        () => entitlements?.allowed_tiers?.length ? entitlements.allowed_tiers : ['lite', 'pro', 'ultra'],
        [entitlements?.allowed_tiers],
    );
    const allowedModalities = useMemo(() => {
        const canonical = canonicalizeModalities(entitlements?.allowed_modalities);
        return canonical.length ? canonical : ['text'];
    }, [entitlements?.allowed_modalities]);
    const douyinPreferredTier = useMemo<SaasTier>(() => {
        const tiers = allowedTiers as SaasTier[];
        if (tiers.includes('pro')) return 'pro';
        if (tiers.includes('lite')) return 'lite';
        return tiers[0] || 'lite';
    }, [allowedTiers]);
    const douyinPreferredModality = useMemo(() => {
        if (allowedModalities.includes('text')) return 'text';
        return allowedModalities[0] || 'text';
    }, [allowedModalities]);

    useEffect(() => {
        if (!open) return;
        if (!allowedTiers.includes(preferredTier)) {
            setPreferredTier((allowedTiers[0] as SaasTier) || 'lite');
        }
        if (!allowedModalities.includes(preferredModality)) {
            setPreferredModality(allowedModalities[0] || 'text');
        }
    }, [open, allowedTiers, allowedModalities, preferredTier, preferredModality]);

    // Reset local form whenever the modal closes so the next open is clean.
    useEffect(() => {
        if (!open) {
            setVisibility('company');
            setPreferredTier('pro');
            setPreferredModality('text');
        }
    }, [open]);

    useEffect(() => {
        if (!open) return;
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [open, onClose]);

    const hire = useMutation({
        mutationFn: (navigateAfter: boolean) => {
            if (!template) return Promise.reject(new Error('No template'));
            const effectivePreferredTier = isDouyinTemplate ? douyinPreferredTier : preferredTier;
            const effectivePreferredModality = isDouyinTemplate
                ? douyinPreferredModality
                : (preferredModality || 'text');
            if (!effectivePreferredTier) {
                return Promise.reject(new Error(t('postHire.modelRequired', '请选择模型档位')));
            }
            // Localize name + role_description when the UI is in Chinese so
            // the agent persists with the same labels the user saw on the
            // Talent Market card. Without this, the DB stores the English
            // template name and the agent shows "Rapid Prototyper" forever
            // even though the card said "快速原型工程师".
            const localized = translateTemplate(
                { name: template.name, description: template.description || '', capability_bullets: [] },
                isChinese,
            );
            const payload: any = {
                name: localized.name,
                role_description: localized.description,
                template_id: template.id,
                preferred_tier: effectivePreferredTier,
                preferred_modality: effectivePreferredModality,
                permission_access_level: 'manage',
            };
            payload.permission_scope_type = visibility === 'company'
                ? 'company'
                : visibility === 'custom'
                    ? 'custom'
                    : 'user';
            payload.permission_scope_ids = [];
            return agentApi.create(payload).then((agent: any) => ({ agent, navigateAfter }));
        },
        onSuccess: ({ agent, navigateAfter }) => {
            queryClient.invalidateQueries({ queryKey: ['agents'] });
            queryClient.invalidateQueries({ queryKey: ['subscription-seats'] });
            (onDone || onClose)();
            // "立即对话" → open directly on the chat tab (not the default status
            // tab). AgentDetail picks up the hash on mount.
            if (navigateAfter) navigate(`/agents/${agent.id}#chat`);
        },
        onError: async (err: any) => {
            const upgradeUrl = err?.detail?.details?.upgrade_url || err?.detail?.upgrade_url || (err?.status === 402 ? SUBSCRIPTION_UPGRADE_PATH : '');
            if (upgradeUrl) {
                queryClient.invalidateQueries({ queryKey: ['subscription-seats'] });
                const goToSubscription = await dialog.confirm(
                    err?.message || t('agent.limit.title', 'Agent limit reached'),
                    {
                        title: t('agent.limit.title', 'Agent limit reached'),
                        confirmLabel: t('subscription.goToDetail', 'Go to subscription'),
                        cancelLabel: t('common.cancel', 'Cancel'),
                    },
                );
                if (goToSubscription) {
                    onClose();
                    navigate(upgradeUrl);
                }
                return;
            }
            await dialog.alert(t('postHire.createFailed'), {
                type: 'error',
                details: String(err?.message || err),
            });
        },
    });

    if (!open || !template) return null;

    const busy = hire.isPending;

    return (
        <div
            style={{
                position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
                background: 'rgba(0,0,0,0.55)', display: 'flex', alignItems: 'center', justifyContent: 'center',
                zIndex: 10001,
            }}
            onClick={e => { if (e.target === e.currentTarget && !busy) onClose(); }}
        >
            <div style={{
                background: 'var(--bg-primary)', borderRadius: '12px',
                width: '480px', maxWidth: '92vw',
                border: '1px solid var(--border-subtle)',
                boxShadow: '0 20px 60px rgba(0,0,0,0.4)',
                display: 'flex', flexDirection: 'column', overflow: 'hidden',
            }}>
                <div style={{ padding: '22px 26px 8px', display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
                    <div>
                        <h3 style={{ margin: 0, fontSize: '17px', fontWeight: 600 }}>
                            {t('postHire.title')}
                        </h3>
                        <p style={{ margin: '4px 0 0', fontSize: '12.5px', color: 'var(--text-secondary)' }}>
                            {template.name}
                        </p>
                    </div>
                    <button onClick={onClose} className="btn btn-ghost" disabled={busy} style={{ padding: '4px' }}>
                        <IconX size={16} stroke={1.5} />
                    </button>
                </div>

                <div style={{ padding: '8px 26px 8px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
                    <section style={{
                        border: '1px solid var(--border-subtle)', borderRadius: '8px',
                        padding: '11px 12px', background: 'var(--bg-secondary)',
                    }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: '8px', alignItems: 'center' }}>
                            <div style={{ fontSize: '13px', fontWeight: 600 }}>
                                {isChinese ? '岗位能力合同' : 'Role capability contract'}
                            </div>
                            <span style={{
                                fontSize: '10.5px', padding: '3px 7px', borderRadius: '999px',
                                color: template.capability_contract?.contract_ready ? '#15803d' : '#a16207',
                                background: template.capability_contract?.contract_ready
                                    ? 'rgba(34,197,94,0.1)'
                                    : 'rgba(245,158,11,0.12)',
                            }}>
                                {template.capability_contract?.contract_ready
                                    ? (isChinese ? '已注册' : 'Registered')
                                    : (isChinese ? '待补齐' : 'Pending')}
                                {template.role_revision ? ` · v${template.role_revision}` : ''}
                            </span>
                        </div>
                        {!!template.deliverables?.length && (
                            <div style={{ marginTop: '8px', fontSize: '11.5px', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
                                <strong>{isChinese ? '交付物：' : 'Deliverables: '}</strong>
                                {template.deliverables.slice(0, 3).join(' · ')}
                            </div>
                        )}
                        {!!template.limitations?.length && (
                            <div style={{ marginTop: '5px', fontSize: '11.5px', color: 'var(--text-tertiary)', lineHeight: 1.55 }}>
                                <strong>{isChinese ? '边界：' : 'Limits: '}</strong>
                                {template.limitations.slice(0, 2).join(' · ')}
                            </div>
                        )}
                        {!!template.source_provenance?.repository && (
                            <div style={{ marginTop: '5px', fontSize: '10.5px', color: 'var(--text-tertiary)', lineHeight: 1.45 }}>
                                {isChinese ? '来源：' : 'Source: '}
                                {String(template.source_provenance.repository)}
                                {template.source_provenance.commit
                                    ? ` @ ${String(template.source_provenance.commit).slice(0, 8)}`
                                    : ''}
                            </div>
                        )}
                    </section>
                    {/* Visibility */}
                    <section>
                        <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>
                            {t('postHire.visibility')}
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                            <RadioRow
                                selected={visibility === 'company'}
                                onClick={() => !busy && setVisibility('company')}
                                title={t('postHire.visibilityCompanyTitle')}
                                hint={t('postHire.visibilityCompanyHint')}
                            />
                            <RadioRow
                                selected={visibility === 'only_me'}
                                onClick={() => !busy && setVisibility('only_me')}
                                title={t('postHire.visibilityOnlyMeTitle')}
                                hint={t('postHire.visibilityOnlyMeHint')}
                            />
                            <RadioRow
                                selected={visibility === 'custom'}
                                onClick={() => !busy && setVisibility('custom')}
                                title={t('postHire.visibilityCustomTitle')}
                                hint={t('postHire.visibilityCustomHint')}
                            />
                        </div>
                    </section>

                    {!isDouyinTemplate && (
                        <section>
                            <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>
                                {t('postHire.model', '模型档位')}
                            </div>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                <TierSelector
                                    value={preferredTier}
                                    onChange={setPreferredTier}
                                    allowedTiers={allowedTiers}
                                    disabled={busy}
                                />
                                <select
                                    className="form-input"
                                    value={preferredModality}
                                    onChange={e => setPreferredModality(e.target.value)}
                                    disabled={busy}
                                    style={{ width: '100%' }}
                                >
                                    {MODALITIES.filter((m) => allowedModalities.includes(m)).map((m) => (
                                        <option key={m} value={m}>{m}</option>
                                    ))}
                                </select>
                            </div>
                        </section>
                    )}

                    {isDouyinTemplate && (
                        <DouyinSetupPanel disabled={busy} />
                    )}
                </div>

                <div style={{ padding: '16px 26px 20px', display: 'flex', justifyContent: 'flex-end', gap: '8px', borderTop: '1px solid var(--border-subtle)', marginTop: '12px' }}>
                    <button
                        className="btn btn-secondary"
                        disabled={busy}
                        onClick={() => hire.mutate(false)}
                    >
                        {busy && !hire.variables
                            ? '...'
                            : isDouyinTemplate
                                ? t('postHire.douyin.createLater', isChinese ? '稍后连接，先创建' : 'Create, connect later')
                                : t('postHire.createOnly')}
                    </button>
                    <button
                        className="btn btn-primary"
                        disabled={busy}
                        onClick={() => hire.mutate(true)}
                    >
                        {busy
                            ? t('postHire.creating')
                            : isDouyinTemplate
                                ? t('postHire.douyin.chatNow', isChinese ? '创建并开始对话' : 'Create and chat')
                                : t('postHire.chatNow')}
                    </button>
                </div>
            </div>
        </div>
    );
}

function DouyinSetupPanel({ disabled }: { disabled: boolean }) {
    const { t, i18n } = useTranslation();
    const isChinese = i18n.language.startsWith('zh');

    return (
        <section
            style={{
                border: '1px solid var(--border-subtle)',
                borderRadius: '8px',
                padding: '12px',
                background: 'var(--bg-secondary)',
            }}
        >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '12px' }}>
                <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '13px', fontWeight: 600 }}>
                        {t('postHire.douyin.title', isChinese ? '抖音账号设置' : 'Douyin account setup')}
                    </div>
                    <div style={{ marginTop: '4px', fontSize: '11.5px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>
                        {t(
                            'postHire.douyin.hint',
                            isChinese
                                ? '连接账号后，这个 Agent 才能读取数据、创建发布包和评论回复审批任务。'
                                : 'After account connection, this agent can read metrics and create publish packages plus reply approval tasks.',
                        )}
                    </div>
                </div>
                <span
                    style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: '4px',
                        padding: '4px 7px',
                        borderRadius: '999px',
                        background: 'var(--bg-primary)',
                        color: 'var(--text-secondary)',
                        fontSize: '11px',
                        whiteSpace: 'nowrap',
                    }}
                >
                    <IconClock size={12} stroke={1.6} />
                    {t('postHire.douyin.pending', isChinese ? '待连接' : 'Not connected')}
                </span>
            </div>

            <div style={{ marginTop: '10px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <div style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                    {t(
                        'postHire.douyin.modelAuto',
                        isChinese
                            ? '模型配置由系统自动选择适合运营对话的默认配置，不需要在创建时设置。'
                            : 'Model routing is selected automatically for operations conversations; no setup is needed here.',
                    )}
                </div>
                <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                    <IconShieldCheck size={15} stroke={1.6} style={{ marginTop: '1px', flexShrink: 0, color: 'var(--text-tertiary)' }} />
                    <span>
                        {t(
                            'postHire.douyin.mode',
                            isChinese
                                ? '默认接管方式：审批后执行。发布作品会先生成发布包，由用户在抖音端确认。'
                                : 'Default operating mode: approval before execution. Publishing becomes a package that the user confirms in Douyin.',
                        )}
                    </span>
                </div>
                <button
                    type="button"
                    className="btn btn-secondary"
                    disabled
                    title={t(
                        'postHire.douyin.connectDisabledTitle',
                        isChinese ? '官方 OAuth 接入完成后启用' : 'Enabled after official OAuth integration is available',
                    )}
                    style={{
                        width: '100%',
                        justifyContent: 'center',
                        opacity: disabled ? 0.5 : 0.62,
                        cursor: 'not-allowed',
                    }}
                >
                    <IconLink size={14} stroke={1.6} />
                    {t('postHire.douyin.connect', isChinese ? '连接抖音账号' : 'Connect Douyin account')}
                </button>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.45 }}>
                    {t(
                        'postHire.douyin.officialOnly',
                        isChinese
                            ? '账号连接将走抖音官方 OAuth；未连接前不会发布、回复或显示已授权状态。'
                            : 'Account connection will use official Douyin OAuth; before that, nothing is published, replied to, or marked authorized.',
                    )}
                </div>
            </div>
        </section>
    );
}

function RadioRow({ selected, onClick, title, hint }: { selected: boolean; onClick: () => void; title: string; hint: string }) {
    return (
        <button
            type="button"
            onClick={onClick}
            style={{
                display: 'flex', alignItems: 'flex-start', gap: '10px',
                padding: '10px 12px', textAlign: 'left',
                border: `1px solid ${selected ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                borderRadius: '8px', background: selected ? 'var(--accent-subtle, rgba(99,102,241,0.08))' : 'transparent',
                cursor: 'pointer', width: '100%',
            }}
        >
            <span style={{
                marginTop: '2px', width: '14px', height: '14px', borderRadius: '50%',
                border: `2px solid ${selected ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0,
            }}>
                {selected && <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent-primary)' }} />}
            </span>
            <span style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                <span style={{ fontSize: '13px', color: 'var(--text-primary)' }}>{title}</span>
                <span style={{ fontSize: '11.5px', color: 'var(--text-tertiary)' }}>{hint}</span>
            </span>
        </button>
    );
}
