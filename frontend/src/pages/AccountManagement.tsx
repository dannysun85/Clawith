import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { fetchJson } from '../services/api';
import { useAuthStore } from '../stores';
import { MODALITIES } from '../constants/modalities';

interface Credential {
    id: string;
    provider: string;
    label: string;
    base_url?: string | null;
    api_key_masked: string;
    capabilities?: string[] | null;
    daily_quota?: number | null;
    rpm_limit?: number | null;
    tpm_limit?: number | null;
    window_5h_limit?: number | null;
    used_today: number;
    status: string;
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
    capabilities: ['text'] as string[],
    daily_quota: '', weight: '1', priority: '0',
    rpm_limit: '', tpm_limit: '', window_5h_limit: '',
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
            capabilities: credential.capabilities ? [...credential.capabilities] : [...MODALITIES],
            daily_quota: credential.daily_quota == null ? '' : String(credential.daily_quota),
            weight: String(credential.weight || 1),
            priority: String(credential.priority || 0),
            rpm_limit: credential.rpm_limit == null ? '' : String(credential.rpm_limit),
            tpm_limit: credential.tpm_limit == null ? '' : String(credential.tpm_limit),
            window_5h_limit: credential.window_5h_limit == null ? '' : String(credential.window_5h_limit),
        });
        setShowForm(true);
    };

    const submit = async () => {
        const mutableFields: Record<string, unknown> = {
            label: form.label,
            base_url: form.base_url || null,
            capabilities: form.capabilities,
            daily_quota: form.daily_quota ? Number(form.daily_quota) : null,
            rpm_limit: form.rpm_limit ? Number(form.rpm_limit) : null,
            tpm_limit: form.tpm_limit ? Number(form.tpm_limit) : null,
            window_5h_limit: form.window_5h_limit ? Number(form.window_5h_limit) : null,
            weight: Number(form.weight) || 1,
            priority: Number(form.priority) || 0,
        };
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
            if (!editingId) {
                setPoolNotice(t('account.verifyRequired', '账号已安全保存，但在验证成功前不会参与模型路由。'));
            }
        } catch (error) {
            setFormError(error instanceof Error ? error.message : String(error));
        }
    };

    const toggleCap = (m: string) => setForm((f) => ({ ...f, capabilities: f.capabilities.includes(m) ? f.capabilities.filter((x) => x !== m) : [...f.capabilities, m] }));

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
                        <select className="form-input" value={form.provider} disabled={!!editingId} onChange={(e) => setForm({ ...form, provider: e.target.value })}>
                            {['minimax', 'openai', 'anthropic', 'deepseek', 'qwen', 'zhipu', 'gemini', 'kimi', 'custom'].map((p) => <option key={p} value={p}>{p}</option>)}
                        </select>
                    </div>
                    <div className="form-group"><label className="form-label">label</label><input className="form-input" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="MiniMax Token Plan #1" /></div>
                    {editingId ? (
                        <div className="form-group" style={{ gridColumn: 'span 2' }}>
                            <label className="form-label">API key</label>
                            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', padding: '9px 0' }}>
                                {t('account.editKeepsKey', '编辑能力和限额不会读取或覆盖现有 API key。')}
                            </div>
                        </div>
                    ) : (
                        <div className="form-group" style={{ gridColumn: 'span 2' }}>
                            <label className="form-label">API key</label>
                            <input className="form-input" type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder={editingId ? '留空保持原 Key；填写后需重新验证' : 'sk-...'} />
                        </div>
                    )}
                    <div className="form-group"><label className="form-label">base_url (可选)</label><input className="form-input" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></div>
                    <div className="form-group"><label className="form-label">每日配额 (可选)</label><input className="form-input" type="number" value={form.daily_quota} onChange={(e) => setForm({ ...form, daily_quota: e.target.value })} placeholder="留空=不限" /></div>
                    <div className="form-group"><label className="form-label" title="每分钟最大请求数">RPM 限流 (每分钟请求数)</label><input className="form-input" type="number" value={form.rpm_limit} onChange={(e) => setForm({ ...form, rpm_limit: e.target.value })} placeholder="留空=不限, e.g. 200" /></div>
                    <div className="form-group"><label className="form-label" title="每分钟最大 token 数">TPM 限流 (每分钟 tokens)</label><input className="form-input" type="number" value={form.tpm_limit} onChange={(e) => setForm({ ...form, tpm_limit: e.target.value })} placeholder="留空=不限, e.g. 10000000" /></div>
                    <div className="form-group"><label className="form-label" title="MiniMax Token Plan 5小时窗口配额 (token)">5小时窗口配额 (可选)</label><input className="form-input" type="number" value={form.window_5h_limit} onChange={(e) => setForm({ ...form, window_5h_limit: e.target.value })} placeholder="MiniMax订阅key的5h窗口" /></div>
                    <div className="form-group"><label className="form-label">weight</label><input className="form-input" type="number" value={form.weight} onChange={(e) => setForm({ ...form, weight: e.target.value })} /></div>
                    <div className="form-group"><label className="form-label">priority</label><input className="form-input" type="number" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} /></div>
                    <div className="form-group" style={{ gridColumn: 'span 2' }}>
                        <label className="form-label">{t('account.capabilities', '能力 (可调用的 modality)')}</label>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                            {MODALITIES.map((m) => (
                                <label key={m} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, padding: '2px 8px', border: '1px solid var(--border-subtle)', borderRadius: 4, cursor: 'pointer', background: form.capabilities.includes(m) ? 'var(--bg-secondary)' : 'transparent' }}>
                                    <input type="checkbox" checked={form.capabilities.includes(m)} onChange={() => toggleCap(m)} />{m}
                                </label>
                            ))}
                        </div>
                    </div>
                    {formError && <div style={{ gridColumn: 'span 2', color: 'var(--error)', fontSize: 12 }}>{formError}</div>}
                    <div style={{ gridColumn: 'span 2', display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                        <button className="btn btn-secondary" onClick={closeForm}>{t('common.cancel')}</button>
                        <button className="btn btn-primary" disabled={!form.label || (!editingId && !form.api_key) || createMut.isPending || updateMut.isPending} onClick={() => void submit()}>{t('common.save')}</button>
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
                    return (
                        <div key={c.id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <div style={{ flex: 1 }}>
                                <div style={{ fontWeight: 500 }}>
                                    {c.label} <span style={{ fontSize: 10, padding: '2px 6px', borderRadius: 4, color: '#fff', background: STATUS_COLOR[c.status] || 'var(--text-tertiary)' }}>{c.status}</span>
                                </div>
                                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                                    {c.provider} · {c.api_key_masked} · {c.capabilities?.join('/') || 'all'}
                                    {c.base_url && ` · ${c.base_url}`}
                                </div>
                                <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 2, display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                                    <span>{t('account.usedToday', '今日用量')}: {c.used_today}{c.daily_quota ? `/${c.daily_quota}` : ''}</span>
                                    <span>errors: {c.error_count}</span>
                                    {h && c.status !== 'unverified' && <span>{t('account.successRate', '成功率')}: {(h.success_rate * 100).toFixed(0)}%</span>}
                                    {c.rpm_limit && h && <span>RPM: {h.rpm_current}/{c.rpm_limit}</span>}
                                    {c.tpm_limit && h && <span>TPM: {fmtNum(h.tpm_current)}/{fmtNum(c.tpm_limit)}</span>}
                                    {c.last_used_at && <span>{t('account.lastUsed', '最后使用')}: {new Date(c.last_used_at).toLocaleString()}</span>}
                                </div>
                            </div>
                            <div style={{ display: 'flex', gap: 6 }}>
                                {c.status === 'unverified' && (
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
                                            : t('account.verify', '验证')}
                                    </button>
                                )}
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
