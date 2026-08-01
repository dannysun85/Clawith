import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { fetchJson } from '../services/api';
import { useAuthStore } from '../stores';
import { MODALITIES } from '../constants/modalities';
import { summarizeCredentialQuota } from '../utils/credentialQuotaStatus';

interface Credential {
    id: string;
    provider: string;
    label: string;
    base_url?: string | null;
    plan_tier?: string | null;
    api_key_masked: string;
    capabilities?: string[] | null;
    modality_status?: Record<string, { status: string; error_code?: string; reset_scope?: string; model?: string | null }> | null;
    daily_quota?: number | null;
    rpm_limit?: number | null;
    tpm_limit?: number | null;
    window_5h_limit?: number | null;
    used_today: number;
    status: string;
    last_verification_at?: string | null;
    verification_receipt?: {
        receipt_ref?: string;
        kind?: string;
        scope?: string;
        evidence_level?: string;
        checked_at?: string;
        ok?: boolean;
        provider_status?: number | null;
        model_count?: number | null;
    } | null;
    error_count: number;
    weight: number;
    priority: number;
    last_used_at?: string | null;
    enabled: boolean;
}

interface Health {
    id: string;
    provider: string;
    label: string;
    status: string;
    enabled: boolean;
    modality_status?: Record<string, { status: string; error_code?: string; reset_scope?: string; model?: string | null }> | null;
    used_today: number;
    daily_quota?: number | null;
    error_count: number;
    success_rate: number;
    last_used_at?: string | null;
    rpm_limit?: number | null;
    tpm_limit?: number | null;
    rpm_current: number;
    tpm_current: number;
}

interface CredentialVerification {
    ok: boolean;
    status: string;
    provider_status?: number | null;
    model_count?: number | null;
    message?: string | null;
    receipt: Record<string, unknown>;
}

const STATUS_COLOR: Record<string, string> = {
    unverified: 'var(--warning)',
    healthy: 'var(--success)',
    degraded: 'var(--warning)',
    quota_exceeded: 'var(--error)',
    disabled: 'var(--text-tertiary)',
};

const emptyForm = {
    provider: 'minimax', label: '', api_key: '', base_url: '',
    plan_tier: '',
    capabilities: [...MODALITIES] as string[],
    daily_quota: '', weight: '1', priority: '0',
    rpm_limit: '', tpm_limit: '',
};

export default function AccountManagement() {
    const { t } = useTranslation();
    const qc = useQueryClient();
    const user = useAuthStore((s) => s.user);
    const isPlatformAdmin = user?.role === 'platform_admin' || !!(user as any)?.is_platform_admin;

    const { data: creds = [] } = useQuery({ queryKey: ['credentials'], queryFn: () => fetchJson<Credential[]>('/credentials') });
    const { data: health = [] } = useQuery({ queryKey: ['credentials-health'], queryFn: () => fetchJson<Health[]>('/credentials/health'), refetchInterval: 30000 });

    const invalidatePool = () => {
        qc.invalidateQueries({ queryKey: ['credentials'] });
        qc.invalidateQueries({ queryKey: ['credentials-health'] });
    };
    const createMut = useMutation({ mutationFn: (d: unknown) => fetchJson('/credentials', { method: 'POST', body: JSON.stringify(d) }), onSuccess: invalidatePool });
    const updateMut = useMutation({ mutationFn: ({ id, d }: { id: string; d: unknown }) => fetchJson(`/credentials/${id}`, { method: 'PATCH', body: JSON.stringify(d) }), onSuccess: invalidatePool });
    const deleteMut = useMutation({ mutationFn: (id: string) => fetchJson(`/credentials/${id}`, { method: 'DELETE' }), onSuccess: invalidatePool });

    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState(emptyForm);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [formError, setFormError] = useState('');
    const [poolNotice, setPoolNotice] = useState('');
    const verifyMut = useMutation({
        mutationFn: (id: string) => fetchJson<CredentialVerification>(`/credentials/${id}/verify`, { method: 'POST' }),
        onSuccess: (result) => {
            invalidatePool();
            setPoolNotice(result.message || (result.ok ? t('account.verifySuccess', '验证成功') : t('account.verifyFailed', '验证失败')));
        },
        onError: (error) => setPoolNotice(error instanceof Error ? error.message : String(error)),
    });

    if (!isPlatformAdmin) {
        return <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>{t('account.noAccess', '仅平台管理员可访问账号池管理')}</div>;
    }

    const healthMap = new Map(health.map((h) => [h.id, h]));

    const closeForm = () => {
        setShowForm(false);
        setEditingId(null);
        setFormError('');
        setForm({ ...emptyForm, capabilities: [...emptyForm.capabilities] });
    };

    const startCreate = () => {
        setEditingId(null);
        setFormError('');
        setPoolNotice('');
        setForm({ ...emptyForm, capabilities: [...emptyForm.capabilities] });
        setShowForm(true);
    };

    const startEdit = (credential: Credential) => {
        setEditingId(credential.id);
        setFormError('');
        setPoolNotice('');
        setForm({
            provider: credential.provider,
            label: credential.label,
            api_key: '',
            base_url: credential.base_url || '',
            plan_tier: credential.plan_tier || '',
            capabilities: credential.capabilities ? [...credential.capabilities] : [...MODALITIES],
            daily_quota: credential.daily_quota == null ? '' : String(credential.daily_quota),
            weight: String(credential.weight || 1),
            priority: String(credential.priority || 0),
            rpm_limit: credential.rpm_limit == null ? '' : String(credential.rpm_limit),
            tpm_limit: credential.tpm_limit == null ? '' : String(credential.tpm_limit),
        });
        setShowForm(true);
    };

    const submit = async () => {
        if (form.capabilities.length === 0) {
            setFormError(t('account.capabilitiesRequired', '至少选择一种能力；空能力不会参与任何模型调用。'));
            return;
        }
        const replacingApiKey = !!editingId && form.api_key.trim().length > 0;
        const mutableFields: Record<string, unknown> = {
            label: form.label,
            base_url: form.base_url || null,
            capabilities: form.capabilities,
            daily_quota: form.daily_quota ? Number(form.daily_quota) : null,
            rpm_limit: form.rpm_limit ? Number(form.rpm_limit) : null,
            tpm_limit: form.tpm_limit ? Number(form.tpm_limit) : null,
            weight: Number(form.weight) || 1,
            priority: Number(form.priority) || 0,
        };
        if (form.provider === 'volcengine_agent_plan') {
            mutableFields.plan_tier = form.plan_tier;
        }
        if (editingId && form.api_key.trim()) mutableFields.api_key = form.api_key.trim();
        setFormError('');
        try {
            if (editingId) {
                await updateMut.mutateAsync({ id: editingId, d: mutableFields });
            } else {
                await createMut.mutateAsync({
                    provider: form.provider,
                    api_key: form.api_key,
                    ...mutableFields,
                });
            }
            closeForm();
            if (replacingApiKey) {
                setPoolNotice(t(
                    'account.reverifyAfterKeyReplacement',
                    'API key 已安全替换，原模型熔断已清除；请先点击“验证”，验证成功后才会重新参与路由。',
                ));
            } else if (!editingId) {
                setPoolNotice(t('account.verifyRequired', '账号已安全保存，但在验证成功前不会参与模型路由。'));
            }
        } catch (error) {
            setFormError(error instanceof Error ? error.message : String(error));
        }
    };

    const toggleCap = (m: string) => setForm((f) => {
        if (f.capabilities.includes(m) && f.capabilities.length === 1) {
            setFormError(t('account.capabilitiesRequired', '至少选择一种能力；空能力不会参与任何模型调用。'));
            return f;
        }
        setFormError('');
        return {
            ...f,
            capabilities: f.capabilities.includes(m)
                ? f.capabilities.filter((x) => x !== m)
                : [...f.capabilities, m],
        };
    });

    const fmtNum = (n: number) => n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(0)}K` : `${n}`;

    return (
        <div style={{ maxWidth: 1100, margin: '0 auto', padding: '24px 16px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
                <div>
                    <h2 style={{ margin: 0 }}>{t('account.title', '账号池管理')}</h2>
                    <p style={{ color: 'var(--text-tertiary)', fontSize: 12, margin: '4px 0 0' }}>
                        {t('account.desc', '平台统一管理 API key 账号池，所有租户共用；按 provider+modality 负载均衡调用，实时监控用量/健康。')}
                    </p>
                </div>
                <button className="btn btn-primary" onClick={showForm ? closeForm : startCreate}>
                    {showForm ? t('common.cancel') : `+ ${t('account.add', '新增账号')}`}
                </button>
            </div>

            {showForm && (
                <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                    <div className="form-group"><label className="form-label">provider</label>
                        <select
                            className="form-input"
                            value={form.provider}
                            disabled={!!editingId}
                            onChange={(e) => {
                                const provider = e.target.value;
                                setForm({
                                    ...form,
                                    provider,
                                    base_url: provider === 'volcengine_agent_plan'
                                        ? 'https://ark.cn-beijing.volces.com/api/plan/v3'
                                        : '',
                                    // Agent Plan does not expose a cheap read-only
                                    // endpoint that proves the purchased tier. Never
                                    // predeclare Large or grant video on behalf of the
                                    // operator; the selected value must match the
                                    // current Volcano Engine console.
                                    plan_tier: '',
                                    capabilities: provider === 'volcengine_agent_plan'
                                        ? ['text', 'image', 'audio']
                                        : [...MODALITIES],
                                });
                            }}
                        >
                            {['volcengine_agent_plan', 'minimax', 'openai', 'anthropic', 'deepseek', 'qwen', 'zhipu', 'gemini', 'kimi', 'custom'].map((p) => <option key={p} value={p}>{p}</option>)}
                        </select>
                    </div>
                    <div className="form-group"><label className="form-label">label</label><input className="form-input" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="MiniMax PAYG Production A" /></div>
                    <div className="form-group" style={{ gridColumn: 'span 2' }}>
                        <label className="form-label">API key</label>
                        <input
                            className="form-input"
                            type="password"
                            autoComplete="new-password"
                            value={form.api_key}
                            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                            placeholder={editingId ? '留空保持原 Key；填写后需重新验证' : 'sk-...'}
                        />
                        {editingId && (
                            <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-tertiary)' }}>
                                {t(
                                    'account.editKeepsKey',
                                    '系统不会读取或回显现有 API key。留空仅修改配置；填写新值会替换密钥、清除旧账号的模型熔断，并暂停路由直到重新验证成功。',
                                )}
                            </div>
                        )}
                    </div>
                    <div className="form-group"><label className="form-label">base_url (可选)</label><input className="form-input" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></div>
                    {form.provider === 'volcengine_agent_plan' && (
                        <div className="form-group">
                            <label className="form-label">Agent Plan 套餐</label>
                            <select
                                className="form-input"
                                value={form.plan_tier}
                                onChange={(e) => {
                                    const planTier = e.target.value;
                                    setForm({
                                        ...form,
                                        plan_tier: planTier,
                                        capabilities: planTier === 'small'
                                            ? form.capabilities.filter((item) => item !== 'video')
                                            : form.capabilities,
                                    });
                                }}
                            >
                                <option value="" disabled>请选择控制台显示的实际套餐</option>
                                {['small', 'medium', 'large', 'max'].map((tier) => (
                                    <option key={tier} value={tier}>{tier}</option>
                                ))}
                            </select>
                            <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-tertiary)' }}>
                                必须与火山控制台当前生效套餐一致；系统不会猜测套餐。文字、Seedream 图片和 Seed TTS 语音支持全部套餐；视频模型由平台依据火山当前公告统一治理，已下线模型不会进入新任务路由。
                            </div>
                        </div>
                    )}
                    <div className="form-group"><label className="form-label">每日配额 (可选)</label><input className="form-input" type="number" value={form.daily_quota} onChange={(e) => setForm({ ...form, daily_quota: e.target.value })} placeholder="留空=不限" /></div>
                    <div className="form-group"><label className="form-label" title="每分钟最大请求数">RPM 限流 (每分钟请求数)</label><input className="form-input" type="number" value={form.rpm_limit} onChange={(e) => setForm({ ...form, rpm_limit: e.target.value })} placeholder="留空=不限, e.g. 200" /></div>
                    <div className="form-group"><label className="form-label" title="每分钟最大 token 数">TPM 限流 (每分钟 tokens)</label><input className="form-input" type="number" value={form.tpm_limit} onChange={(e) => setForm({ ...form, tpm_limit: e.target.value })} placeholder="留空=不限, e.g. 10000000" /></div>
                    {form.provider === 'minimax' && (
                        <div className="form-group" style={{ gridColumn: 'span 2', fontSize: 12, color: 'var(--warning)' }}>
                            {t('account.providerCapacityNotice')}
                            <br />
                            <span style={{ color: 'var(--text-tertiary)' }}>
                                {t('account.providerQuotaNotice')}
                            </span>
                        </div>
                    )}
                    <div className="form-group"><label className="form-label">weight</label><input className="form-input" type="number" value={form.weight} onChange={(e) => setForm({ ...form, weight: e.target.value })} /></div>
                    <div className="form-group"><label className="form-label">priority</label><input className="form-input" type="number" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} /></div>
                    <div className="form-group" style={{ gridColumn: 'span 2' }}>
                        <label className="form-label">{t('account.capabilities', '能力 (可调用的 modality)')}</label>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                            {(form.provider === 'volcengine_agent_plan' ? ['text', 'image', 'audio', 'video'] : MODALITIES).map((m) => (
                                <label key={m} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, padding: '2px 8px', border: '1px solid var(--border-subtle)', borderRadius: 4, cursor: 'pointer', background: form.capabilities.includes(m) ? 'var(--bg-secondary)' : 'transparent' }}>
                                    <input
                                        type="checkbox"
                                        checked={form.capabilities.includes(m)}
                                        disabled={form.provider === 'volcengine_agent_plan' && m === 'video' && form.plan_tier === 'small'}
                                        onChange={() => toggleCap(m)}
                                    />{m}
                                </label>
                            ))}
                        </div>
                    </div>
                    {formError && <div style={{ gridColumn: 'span 2', color: 'var(--error)', fontSize: 12 }}>{formError}</div>}
                    <div style={{ gridColumn: 'span 2', display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                        <button className="btn btn-secondary" onClick={closeForm}>{t('common.cancel')}</button>
                        <button className="btn btn-primary" disabled={!form.label || (!editingId && !form.api_key) || (form.provider === 'volcengine_agent_plan' && !form.plan_tier) || createMut.isPending || updateMut.isPending} onClick={() => void submit()}>{t('common.save')}</button>
                    </div>
                </div>
            )}

            {poolNotice && (
                <div className="card" role="status" style={{ marginBottom: 12, padding: '10px 12px', fontSize: 12, color: 'var(--text-secondary)' }}>
                    {poolNotice}
                </div>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {creds.map((c) => {
                    const h = healthMap.get(c.id);
                    const {
                        blockedLabels: blockedModalities,
                        sharedPlanBlocked,
                        unsupportedModelLabels,
                    } = summarizeCredentialQuota(c.modality_status);
                    return (
                        <div key={c.id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <div style={{ flex: 1 }}>
                                <div style={{ fontWeight: 500 }}>
                                    {c.label} <span style={{ fontSize: 10, padding: '2px 6px', borderRadius: 4, color: '#fff', background: STATUS_COLOR[c.status] || 'var(--text-tertiary)' }}>{c.status}</span>
                                </div>
                                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                                    {c.provider} · {c.api_key_masked} · {
                                        c.capabilities === null || c.capabilities === undefined
                                            ? 'all'
                                            : c.capabilities.length > 0
                                                ? c.capabilities.join('/')
                                                : t('account.noCapabilities', '未配置能力')
                                    }
                                    {c.base_url && ` · ${c.base_url}`}
                                    {c.plan_tier && ` · plan=${c.plan_tier}`}
                                </div>
                                <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 2, display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                                    <span>{t('account.usedToday', '今日用量')}: {c.used_today}{c.daily_quota ? `/${c.daily_quota}` : ''}</span>
                                    <span>errors: {c.error_count}</span>
                                    {h && c.status !== 'unverified' && <span>{t('account.successRate', '成功率')}: {(h.success_rate * 100).toFixed(0)}%</span>}
                                    {c.rpm_limit && h && <span>RPM: {h.rpm_current}/{c.rpm_limit}</span>}
                                    {c.tpm_limit && h && <span>TPM: {fmtNum(h.tpm_current)}/{fmtNum(c.tpm_limit)}</span>}
                                    {c.last_used_at && <span>{t('account.lastUsed', '最后使用')}: {new Date(c.last_used_at).toLocaleString()}</span>}
                                </div>
                                <div style={{ fontSize: 11, color: c.verification_receipt?.ok ? 'var(--success)' : 'var(--warning)', marginTop: 3 }}>
                                    {c.verification_receipt && c.last_verification_at
                                        ? <>
                                            账号鉴权：{c.verification_receipt.ok ? '已通过' : '失败'} · {new Date(c.last_verification_at).toLocaleString()}
                                            {c.verification_receipt.provider_status != null && ` · HTTP ${c.verification_receipt.provider_status}`}
                                            {' · 该 receipt 不证明媒体生成权限或商用质量'}
                                        </>
                                        : '账号鉴权：尚无当前配置的验证 receipt，不参与媒体就绪判断'}
                                </div>
                                {blockedModalities.length > 0 && (
                                    <div role="status" style={{ fontSize: 11, color: 'var(--warning)', marginTop: 4 }}>
                                        {sharedPlanBlocked
                                            ? t('account.sharedPlanQuotaLimited', 'MiniMax Token Plan 共享额度已达上限，该账号的所有调用能力均暂停')
                                            : unsupportedModelLabels.length > 0
                                                ? <>{t('account.providerModelUnavailable', '供应商当前套餐未授权模型')}: {unsupportedModelLabels.join(' / ')} · {t('account.otherModalitiesAvailable', '其他能力仍可正常调用')}</>
                                            : <>{t('account.modalityQuotaLimited', '供应商独立配额已达上限')}: {blockedModalities.join(' / ')} · {t('account.otherModalitiesAvailable', '其他能力仍可正常调用')}</>}
                                    </div>
                                )}
                            </div>
                            <div style={{ display: 'flex', gap: 6 }}>
                                <button
                                    className="btn btn-secondary"
                                    style={{ fontSize: 12 }}
                                    disabled={verifyMut.isPending && verifyMut.variables === c.id}
                                    onClick={() => {
                                        setPoolNotice('');
                                        verifyMut.mutate(c.id);
                                    }}
                                >
                                    {verifyMut.isPending && verifyMut.variables === c.id
                                        ? t('account.verifying', '验证中…')
                                        : t('account.verify', '只读验证')}
                                </button>
                                <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => startEdit(c)}>
                                    {t('common.edit', '编辑')}
                                </button>
                                <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => updateMut.mutate({ id: c.id, d: { enabled: !c.enabled } })}>
                                    {c.enabled ? t('account.disable', '停用') : t('account.enable', '启用')}
                                </button>
                                <button className="btn btn-ghost" style={{ fontSize: 12, color: 'var(--error)' }} onClick={() => deleteMut.mutate(c.id)}>{t('common.delete')}</button>
                            </div>
                        </div>
                    );
                })}
                {creds.length === 0 && <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-tertiary)' }}>{t('common.noData')}</div>}
            </div>
        </div>
    );
}
