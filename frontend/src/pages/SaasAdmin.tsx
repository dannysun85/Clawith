import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle, IconDatabase, IconKey, IconPhotoVideo, IconReceipt, IconRoute, IconStack2, IconUsers, IconWallet } from '@tabler/icons-react';
import { fetchJson, enterpriseApi } from '../services/api';
import { MODALITIES } from '../constants/modalities';
import PlansTab from './enterprise-settings/tabs/PlansTab';
import AccountManagement from './AccountManagement';
import { RegistrationCodesTab } from './AdminCompanies';

const SAAS_TIERS = ['lite', 'pro', 'ultra'];
const LLM_ROUTE_MODALITIES = ['text', 'image', 'video'];

type SaasTab = 'plans' | 'packs' | 'rules' | 'model-routes' | 'media-routes' | 'tenants' | 'registration-codes' | 'accounts' | 'production-issues';

type ModelRoute = {
    id: string;
    saas_tier: string;
    modality: string;
    llm_model_id: string;
    priority: number;
    fallback_route_id?: string | null;
    enabled: boolean;
};

type MediaRoute = {
    modality: 'image' | 'audio' | 'music' | 'video';
    tier: 'lite' | 'pro' | 'ultra';
    provider: string;
    tool_name: string;
    model: string;
    settings: Record<string, string | number | boolean>;
    valid_models: string[];
    enabled: boolean;
    tool_enabled: boolean;
    pool_available: boolean;
    available: boolean;
    source: 'default' | 'override';
    billing_mode: 'provider_dynamic';
    estimated_credits?: number | null;
    billing_unit: string;
};

type CreditPack = {
    id: string;
    code: string;
    name: string;
    credits: number;
    price_cents: number;
    currency: string;
    is_active: boolean;
    sort_order: number;
};

type BillingRule = {
    id: string;
    action: string;
    modality?: string | null;
    tier?: string | null;
    unit: string;
    credit_cost: number;
    enabled: boolean;
    priority: number;
};

type TenantSummary = {
    tenant_id: string;
    tenant_name?: string | null;
    plan_code?: string | null;
    subscription_status?: string | null;
    seats_total: number;
    seats_used: number;
    credits_balance: number;
};

type RuntimeModelCandidate = {
    id: string;
    label: string;
    provider: string;
    model: string;
};

type RuntimeModelSettings = {
    tenant_id: string;
    planning_model_id: string | null;
    compact_model_id: string | null;
    planning_source: 'database' | 'environment';
    compact_source: 'database' | 'environment';
    candidates: RuntimeModelCandidate[];
};

type Plan = {
    id: string;
    code: string;
    name: string;
};

type InitializeFreeResult = {
    total_candidates: number;
    created: number;
    skipped_existing: number;
    tenant_ids: string[];
};

type ProductionIssue = {
    id: string;
    category: string;
    severity: 'warning' | 'error' | 'critical';
    status: 'open' | 'acknowledged' | 'resolved' | 'ignored';
    source: string;
    error_code?: string | null;
    summary: string;
    route?: string | null;
    operation?: string | null;
    event_count: number;
    affected_tenant_count: number;
    first_seen_at: string;
    last_seen_at: string;
    last_trace_id?: string | null;
    release_version?: string | null;
    last_metadata?: Record<string, string | number | boolean | null> | null;
};

type ProductionIssueSummary = {
    open_total: number;
    open_warning: number;
    open_error: number;
    open_critical: number;
    events_last_24h: number;
    affected_tenants_last_24h: number;
};

const tabMeta: { key: SaasTab; label: string; icon: ReactNode }[] = [
    { key: 'plans', label: '套餐', icon: <IconStack2 size={15} stroke={1.7} /> },
    { key: 'packs', label: '额度包', icon: <IconWallet size={15} stroke={1.7} /> },
    { key: 'rules', label: '计费规则', icon: <IconReceipt size={15} stroke={1.7} /> },
    { key: 'model-routes', label: '理解模型', icon: <IconRoute size={15} stroke={1.7} /> },
    { key: 'media-routes', label: '媒体路由', icon: <IconPhotoVideo size={15} stroke={1.7} /> },
    { key: 'tenants', label: '租户订阅', icon: <IconUsers size={15} stroke={1.7} /> },
    { key: 'registration-codes', label: '注册码', icon: <IconKey size={15} stroke={1.7} /> },
    { key: 'accounts', label: '账号池', icon: <IconDatabase size={15} stroke={1.7} /> },
    { key: 'production-issues', label: '生产问题', icon: <IconAlertTriangle size={15} stroke={1.7} /> },
];

export default function SaasAdmin() {
    const { t } = useTranslation();
    const [params, setParams] = useSearchParams();
    const activeTab = (params.get('tab') || 'plans') as SaasTab;
    const tab = tabMeta.some((item) => item.key === activeTab) ? activeTab : 'plans';

    return (
        <div style={{ maxWidth: 1180, margin: '0 auto', padding: '24px 16px' }}>
            <div className="page-header" style={{ marginBottom: 16 }}>
                <div>
                    <h1 className="page-title">{t('saas.title', 'SaaS 后台')}</h1>
                    <p style={{ margin: '6px 0 0', color: 'var(--text-tertiary)', fontSize: 13 }}>
                        {t('saas.desc', '统一配置套餐、额度、理解模型、媒体生成路由、账号池和租户订阅。')}
                    </p>
                </div>
            </div>

            <div className="tabs" style={{ marginBottom: 16 }}>
                {tabMeta.map((item) => (
                    <div
                        key={item.key}
                        className={`tab ${tab === item.key ? 'active' : ''}`}
                        onClick={() => setParams({ tab: item.key })}
                        style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
                    >
                        {item.icon}
                        {item.label}
                    </div>
                ))}
            </div>

            {tab === 'plans' && <PlansTab />}
            {tab === 'packs' && <CreditPacksTab />}
            {tab === 'rules' && <BillingRulesTab />}
            {tab === 'model-routes' && <ModelRoutesTab />}
            {tab === 'media-routes' && <MediaRoutesTab />}
            {tab === 'tenants' && <TenantsTab />}
            {tab === 'registration-codes' && <RegistrationCodesTab />}
            {tab === 'accounts' && <AccountManagement />}
            {tab === 'production-issues' && <ProductionIssuesTab />}
        </div>
    );
}

function ModelRoutesTab() {
    const qc = useQueryClient();
    const { data: routes = [] } = useQuery({
        queryKey: ['saas-model-routes'],
        queryFn: () => fetchJson<ModelRoute[]>('/saas/model-routes'),
    });
    const { data: models = [] } = useQuery({
        queryKey: ['llm-models-platform-routes'],
        queryFn: () => enterpriseApi.platformLlmModels(),
    });
    const [form, setForm] = useState({ saas_tier: 'pro', modality: 'text', llm_model_id: '', priority: '0' });
    const modelMap = useMemo(() => new Map(models.map((m: any) => [m.id, m])), [models]);

    const create = useMutation({
        mutationFn: (data: unknown) => fetchJson('/saas/model-routes', { method: 'POST', body: JSON.stringify(data) }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-model-routes'] }),
    });
    const update = useMutation({
        mutationFn: ({ id, data }: { id: string; data: unknown }) => fetchJson(`/saas/model-routes/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-model-routes'] }),
    });
    const remove = useMutation({
        mutationFn: (id: string) => fetchJson(`/saas/model-routes/${id}`, { method: 'DELETE' }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-model-routes'] }),
    });

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, padding: 14, color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                这里配置对话输入理解路由：text、image、video。语音、音乐以及图片/视频“生成模型”请到“媒体路由”配置；系统会拒绝把不支持该输入类型的模型绑定到对应路由。
            </div>
            <RuntimeModelSettingsCard />
            <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10, alignItems: 'end' }}>
                <Field label="Tier">
                    <select className="form-input" value={form.saas_tier} onChange={(e) => setForm({ ...form, saas_tier: e.target.value })}>
                        {SAAS_TIERS.map((tier) => <option key={tier} value={tier}>{tier}</option>)}
                    </select>
                </Field>
                <Field label="Modality">
                    <select className="form-input" value={form.modality} onChange={(e) => setForm({ ...form, modality: e.target.value })}>
                        {LLM_ROUTE_MODALITIES.map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                </Field>
                <Field label="Model">
                    <select className="form-input" value={form.llm_model_id} onChange={(e) => setForm({ ...form, llm_model_id: e.target.value })}>
                        <option value="">Select model</option>
                        {models.map((m: any) => <option key={m.id} value={m.id}>{m.label || m.model} · {m.provider}</option>)}
                    </select>
                </Field>
                <Field label="Priority">
                    <input className="form-input" type="number" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} />
                </Field>
                <button
                    className="btn btn-primary"
                    disabled={!form.llm_model_id || create.isPending}
                    onClick={() => create.mutate({ ...form, priority: Number(form.priority), enabled: true })}
                >
                    新增路由
                </button>
            </div>
            <DataTable
                rows={routes}
                empty="暂无模型路由"
                renderHeader={() => <><th>Tier</th><th>Modality</th><th>Model</th><th>Priority</th><th>Status</th><th /></>}
                renderRow={(route) => {
                    const model = modelMap.get(route.llm_model_id) as any;
                    return (
                        <>
                            <td>{route.saas_tier}</td>
                            <td>{route.modality}</td>
                            <td>{model ? `${model.label || model.model} · ${model.provider}` : route.llm_model_id}</td>
                            <td>{route.priority}</td>
                            <td>{route.enabled ? 'enabled' : 'disabled'}</td>
                            <td style={{ textAlign: 'right' }}>
                                <button className="btn btn-ghost" onClick={() => update.mutate({ id: route.id, data: { enabled: !route.enabled } })}>{route.enabled ? '停用' : '启用'}</button>
                                <button className="btn btn-ghost" style={{ color: 'var(--error)' }} onClick={() => remove.mutate(route.id)}>删除</button>
                            </td>
                        </>
                    );
                }}
            />
        </div>
    );
}

function RuntimeModelSettingsCard() {
    const qc = useQueryClient();
    const [selectedTenantId, setSelectedTenantId] = useState(
        () => localStorage.getItem('current_tenant_id') || '',
    );
    const [form, setForm] = useState({ planning_model_id: '', compact_model_id: '' });

    const { data: tenants = [], isLoading: tenantsLoading } = useQuery({
        queryKey: ['saas-tenants-runtime-models'],
        queryFn: () => fetchJson<TenantSummary[]>('/saas/tenants'),
    });

    useEffect(() => {
        if (tenants.length === 0) return;
        if (!tenants.some((tenant) => tenant.tenant_id === selectedTenantId)) {
            setSelectedTenantId(tenants[0].tenant_id);
        }
    }, [selectedTenantId, tenants]);

    const settingsUrl = selectedTenantId
        ? `/enterprise/runtime-model-settings?tenant_id=${encodeURIComponent(selectedTenantId)}`
        : '';
    const settingsQuery = useQuery({
        queryKey: ['runtime-model-settings', selectedTenantId],
        queryFn: () => fetchJson<RuntimeModelSettings>(settingsUrl),
        enabled: Boolean(selectedTenantId),
    });

    useEffect(() => {
        if (!settingsQuery.data) return;
        setForm({
            planning_model_id: settingsQuery.data.planning_model_id || '',
            compact_model_id: settingsQuery.data.compact_model_id || '',
        });
    }, [settingsQuery.data]);

    const save = useMutation({
        mutationFn: () => fetchJson<RuntimeModelSettings>(settingsUrl, {
            method: 'PUT',
            body: JSON.stringify(form),
        }),
        onSuccess: (data) => {
            qc.setQueryData(['runtime-model-settings', selectedTenantId], data);
        },
    });

    const changeTenant = (tenantId: string) => {
        save.reset();
        setSelectedTenantId(tenantId);
        setForm({ planning_model_id: '', compact_model_id: '' });
    };
    const candidates = settingsQuery.data?.candidates || [];
    const error = settingsQuery.error || save.error;

    return (
        <div className="card" style={{ marginBottom: 16, padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 650 }}>多智能体运行时模型（按租户）</div>
            <div style={{ marginTop: 6, color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                仅选择 Groups 的规划和上下文压缩模型；模型、API Key 与 Credits 仍由平台理解路由和账号池统一管理，不向租户下放模型对象或密钥。
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(180px, 0.8fr) repeat(2, minmax(240px, 1fr)) auto', gap: 10, alignItems: 'end', marginTop: 14 }}>
                <Field label="租户">
                    <select
                        className="form-input"
                        value={selectedTenantId}
                        disabled={tenantsLoading || tenants.length === 0}
                        onChange={(event) => changeTenant(event.target.value)}
                    >
                        {tenants.length === 0 && <option value="">暂无租户</option>}
                        {tenants.map((tenant) => (
                            <option key={tenant.tenant_id} value={tenant.tenant_id}>
                                {tenant.tenant_name || tenant.tenant_id}
                            </option>
                        ))}
                    </select>
                </Field>
                <Field label="群聊规划模型">
                    <select
                        className="form-input"
                        value={form.planning_model_id}
                        disabled={settingsQuery.isLoading || candidates.length === 0}
                        onChange={(event) => setForm((current) => ({ ...current, planning_model_id: event.target.value }))}
                    >
                        <option value="" disabled>请选择模型</option>
                        {candidates.map((model) => (
                            <option key={model.id} value={model.id}>{model.label} · {model.provider}/{model.model}</option>
                        ))}
                    </select>
                </Field>
                <Field label="群聊上下文模型">
                    <select
                        className="form-input"
                        value={form.compact_model_id}
                        disabled={settingsQuery.isLoading || candidates.length === 0}
                        onChange={(event) => setForm((current) => ({ ...current, compact_model_id: event.target.value }))}
                    >
                        <option value="" disabled>请选择模型</option>
                        {candidates.map((model) => (
                            <option key={model.id} value={model.id}>{model.label} · {model.provider}/{model.model}</option>
                        ))}
                    </select>
                </Field>
                <button
                    className="btn btn-primary"
                    disabled={
                        save.isPending
                        || !form.planning_model_id
                        || !form.compact_model_id
                    }
                    onClick={() => save.mutate()}
                >
                    {save.isPending ? '保存中...' : '保存'}
                </button>
            </div>
            {settingsQuery.isLoading && (
                <div style={{ marginTop: 10, color: 'var(--text-tertiary)', fontSize: 12 }}>正在加载运行时模型...</div>
            )}
            {!settingsQuery.isLoading && selectedTenantId && candidates.length === 0 && !error && (
                <div style={{ marginTop: 10, color: 'var(--warning)', fontSize: 12 }}>
                    暂无可用候选模型。候选模型必须已启用并通过原生工具调用测试。
                </div>
            )}
            {settingsQuery.data && (
                <div style={{ marginTop: 10, color: 'var(--text-tertiary)', fontSize: 11 }}>
                    当前来源：规划 {settingsQuery.data.planning_source} · 上下文 {settingsQuery.data.compact_source}
                </div>
            )}
            {save.isSuccess && (
                <div style={{ marginTop: 10, color: 'var(--success)', fontSize: 12 }}>运行时模型配置已更新并立即生效。</div>
            )}
            {error && (
                <div style={{ marginTop: 10, color: 'var(--error)', fontSize: 12 }}>
                    运行时模型配置失败：{error instanceof Error ? error.message : String(error)}
                </div>
            )}
        </div>
    );
}

const MEDIA_LABELS: Record<MediaRoute['modality'], string> = {
    image: '图片',
    audio: '语音',
    music: '音乐',
    video: '视频',
};

function MediaRoutesTab() {
    const { data: routes = [], isLoading, error } = useQuery({
        queryKey: ['saas-media-routes'],
        queryFn: () => fetchJson<MediaRoute[]>('/saas/media-routes'),
    });

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, padding: 16 }}>
                <div style={{ fontSize: 14, fontWeight: 650, marginBottom: 6 }}>媒体生成路由（平台统一配置）</div>
                <div style={{ color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                    文本模型和媒体生成模型是两条独立链路。这里管理 MiniMax 图片、语音、音乐、视频在 Lite / Pro / Ultra 下的真实模型与质量参数；API Key 仍由“账号池”统一提供，不绑定到模型对象。
                    预计 Credits 按供应商实际模型和参数动态计算，计费规则页中的固定规则不覆盖 MiniMax 媒体调用。
                </div>
            </div>
            {error && (
                <div className="card" style={{ marginBottom: 16, padding: 14, color: 'var(--error)' }}>
                    媒体路由加载失败：{error instanceof Error ? error.message : String(error)}
                </div>
            )}
            <DataTable
                rows={routes}
                empty={isLoading ? '正在读取生产媒体路由…' : '暂无媒体路由'}
                renderHeader={() => <><th>能力</th><th>Tier</th><th>模型</th><th>质量参数</th><th>预计费用</th><th>可用性</th><th>来源</th><th /></>}
                renderRow={(route) => <MediaRouteRow route={route} />}
            />
        </div>
    );
}

function MediaRouteRow({ route }: { route: MediaRoute }) {
    const qc = useQueryClient();
    const [model, setModel] = useState(route.model);
    const [enabled, setEnabled] = useState(route.enabled);
    const [settings, setSettings] = useState(route.settings);

    useEffect(() => {
        setModel(route.model);
        setEnabled(route.enabled);
        setSettings(route.settings);
    }, [route]);

    const update = useMutation({
        mutationFn: (data: Record<string, unknown>) => fetchJson<MediaRoute>(
            `/saas/media-routes/${route.modality}/${route.tier}`,
            { method: 'PATCH', body: JSON.stringify(data) },
        ),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-media-routes'] }),
        onError: (err) => window.alert(`保存失败：${err instanceof Error ? err.message : String(err)}`),
    });

    const setSetting = (key: string, value: string | number) => {
        setSettings((current) => ({ ...current, [key]: value }));
    };
    const payload: Record<string, unknown> = { model, enabled, ...settings };

    return (
        <>
            <td><strong>{MEDIA_LABELS[route.modality]}</strong><div style={{ color: 'var(--text-tertiary)', fontSize: 10 }}>{route.provider}</div></td>
            <td style={{ textTransform: 'uppercase' }}>{route.tier}</td>
            <td style={{ minWidth: 190 }}>
                <select className="form-input" value={model} onChange={(e) => setModel(e.target.value)}>
                    {route.valid_models.map((name) => <option key={name} value={name}>{name}</option>)}
                </select>
            </td>
            <td style={{ minWidth: 230 }}>
                <MediaQualityFields modality={route.modality} tier={route.tier} settings={settings} onChange={setSetting} />
            </td>
            <td style={{ whiteSpace: 'nowrap' }}>
                {route.estimated_credits == null ? '按量' : `${route.estimated_credits} Credits/${route.billing_unit}`}
            </td>
            <td style={{ minWidth: 130 }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> 路由启用
                </label>
                <div style={{ color: route.available ? 'var(--success)' : 'var(--error)', fontSize: 11 }}>
                    {route.available ? '账号池与工具就绪' : !route.pool_available ? '账号池不可用' : !route.tool_enabled ? '工具已停用' : '路由已停用'}
                </div>
            </td>
            <td>{route.source === 'override' ? '后台覆盖' : '系统默认'}</td>
            <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                <button className="btn btn-primary" disabled={update.isPending} onClick={() => update.mutate(payload)}>保存</button>
                <button className="btn btn-ghost" disabled={update.isPending || route.source === 'default'} onClick={() => update.mutate({ reset_to_default: true })}>恢复默认</button>
            </td>
        </>
    );
}

function MediaQualityFields({
    modality,
    tier,
    settings,
    onChange,
}: {
    modality: MediaRoute['modality'];
    tier: MediaRoute['tier'];
    settings: Record<string, string | number | boolean>;
    onChange: (key: string, value: string | number) => void;
}) {
    if (modality === 'image') return <span style={{ color: 'var(--text-tertiary)' }}>由 image-01 输出规格决定</span>;
    if (modality === 'video') {
        const allowed = tier === 'lite'
            ? [{ duration: 6, resolution: '768P' }]
            : tier === 'pro'
                ? [{ duration: 6, resolution: '768P' }, { duration: 10, resolution: '768P' }]
                : [{ duration: 6, resolution: '768P' }, { duration: 10, resolution: '768P' }, { duration: 6, resolution: '1080P' }];
        const currentDuration = Number(settings.duration ?? 6);
        const currentResolution = String(settings.resolution ?? '768P');
        const changePair = (duration: number, resolution: string) => {
            onChange('duration', duration);
            onChange('resolution', resolution);
        };
        return (
            <div>
                <select
                    className="form-input"
                    value={`${currentDuration}:${currentResolution}`}
                    onChange={(e) => {
                        const selected = allowed.find((pair) => `${pair.duration}:${pair.resolution}` === e.target.value) ?? allowed[0];
                        changePair(selected.duration, selected.resolution);
                    }}
                >
                    {allowed.map((pair) => (
                        <option key={`${pair.duration}:${pair.resolution}`} value={`${pair.duration}:${pair.resolution}`}>
                            {pair.duration} 秒 / {pair.resolution}
                        </option>
                    ))}
                </select>
            </div>
        );
    }
    const sampleRates = modality === 'music' ? [32000, 44100] : [16000, 24000, 32000, 44100];
    const bitrates = modality === 'music' ? [128000, 256000] : [32000, 64000, 128000, 256000];
    return (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
            <select className="form-input" value={String(settings.sample_rate ?? 32000)} onChange={(e) => onChange('sample_rate', Number(e.target.value))}>
                {sampleRates.map((value) => <option key={value} value={value}>{value} Hz</option>)}
            </select>
            <select className="form-input" value={String(settings.bitrate ?? 128000)} onChange={(e) => onChange('bitrate', Number(e.target.value))}>
                {bitrates.map((value) => <option key={value} value={value}>{value / 1000} kbps</option>)}
            </select>
        </div>
    );
}

function CreditPacksTab() {
    const qc = useQueryClient();
    const { data: packs = [] } = useQuery({ queryKey: ['saas-credit-packs'], queryFn: () => fetchJson<CreditPack[]>('/saas/credit-packs') });
    const [form, setForm] = useState({ code: '', name: '', credits: '10000', price_cents: '1500', currency: 'USD' });
    const create = useMutation({ mutationFn: (data: unknown) => fetchJson('/saas/credit-packs', { method: 'POST', body: JSON.stringify(data) }), onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-credit-packs'] }) });
    const update = useMutation({ mutationFn: ({ id, data }: { id: string; data: unknown }) => fetchJson(`/saas/credit-packs/${id}`, { method: 'PATCH', body: JSON.stringify(data) }), onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-credit-packs'] }) });
    const remove = useMutation({ mutationFn: (id: string) => fetchJson(`/saas/credit-packs/${id}`, { method: 'DELETE' }), onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-credit-packs'] }) });

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 10, alignItems: 'end' }}>
                <Field label="Code"><input className="form-input" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} placeholder="boost_10k" /></Field>
                <Field label="Name"><input className="form-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Boost 10,000" /></Field>
                <Field label="Credits"><input className="form-input" type="number" value={form.credits} onChange={(e) => setForm({ ...form, credits: e.target.value })} /></Field>
                <Field label="Price cents"><input className="form-input" type="number" value={form.price_cents} onChange={(e) => setForm({ ...form, price_cents: e.target.value })} /></Field>
                <Field label="Currency"><input className="form-input" value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value })} /></Field>
                <button className="btn btn-primary" disabled={!form.code || !form.name} onClick={() => create.mutate({ ...form, credits: Number(form.credits), price_cents: Number(form.price_cents), is_active: true })}>新增额度包</button>
            </div>
            <DataTable
                rows={packs}
                empty="暂无额度包"
                renderHeader={() => <><th>Name</th><th>Credits</th><th>Price</th><th>Status</th><th /></>}
                renderRow={(pack) => (
                    <>
                        <td>{pack.name}<div style={{ color: 'var(--text-tertiary)', fontSize: 11 }}>{pack.code}</div></td>
                        <td>{pack.credits.toLocaleString()}</td>
                        <td>{pack.currency} {(pack.price_cents / 100).toFixed(2)}</td>
                        <td>{pack.is_active ? 'published' : 'hidden'}</td>
                        <td style={{ textAlign: 'right' }}>
                            <button className="btn btn-ghost" onClick={() => update.mutate({ id: pack.id, data: { is_active: !pack.is_active } })}>{pack.is_active ? '下架' : '发布'}</button>
                            <button className="btn btn-ghost" style={{ color: 'var(--error)' }} onClick={() => remove.mutate(pack.id)}>删除</button>
                        </td>
                    </>
                )}
            />
        </div>
    );
}

function BillingRulesTab() {
    const qc = useQueryClient();
    const { data: rules = [] } = useQuery({ queryKey: ['saas-billing-rules'], queryFn: () => fetchJson<BillingRule[]>('/saas/billing-rules') });
    const [form, setForm] = useState({ action: 'chat', modality: 'text', tier: 'pro', unit: 'call', credit_cost: '1', priority: '0' });
    const create = useMutation({ mutationFn: (data: unknown) => fetchJson('/saas/billing-rules', { method: 'POST', body: JSON.stringify(data) }), onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-billing-rules'] }) });
    const update = useMutation({ mutationFn: ({ id, data }: { id: string; data: unknown }) => fetchJson(`/saas/billing-rules/${id}`, { method: 'PATCH', body: JSON.stringify(data) }), onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-billing-rules'] }) });
    const remove = useMutation({ mutationFn: (id: string) => fetchJson(`/saas/billing-rules/${id}`, { method: 'DELETE' }), onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-billing-rules'] }) });

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, padding: 14, color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                固定规则用于 chat、heartbeat 等通用动作兜底。MiniMax 媒体调用按供应商模型、字符数、时长和分辨率动态计费，请在“媒体路由”查看当前预计费用。
            </div>
            <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 10, alignItems: 'end' }}>
                <Field label="Action"><input className="form-input" value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })} /></Field>
                <Field label="Modality"><select className="form-input" value={form.modality} onChange={(e) => setForm({ ...form, modality: e.target.value })}>{MODALITIES.map((m) => <option key={m} value={m}>{m}</option>)}</select></Field>
                <Field label="Tier"><select className="form-input" value={form.tier} onChange={(e) => setForm({ ...form, tier: e.target.value })}>{SAAS_TIERS.map((tier) => <option key={tier} value={tier}>{tier}</option>)}</select></Field>
                <Field label="Unit"><input className="form-input" value={form.unit} onChange={(e) => setForm({ ...form, unit: e.target.value })} /></Field>
                <Field label="Credits"><input className="form-input" type="number" value={form.credit_cost} onChange={(e) => setForm({ ...form, credit_cost: e.target.value })} /></Field>
                <Field label="Priority"><input className="form-input" type="number" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} /></Field>
                <button className="btn btn-primary" disabled={!form.action} onClick={() => create.mutate({ ...form, credit_cost: Number(form.credit_cost), priority: Number(form.priority), enabled: true })}>新增规则</button>
            </div>
            <DataTable
                rows={rules}
                empty="暂无计费规则"
                renderHeader={() => <><th>Action</th><th>Modality</th><th>Tier</th><th>Cost</th><th>Status</th><th /></>}
                renderRow={(rule) => (
                    <>
                        <td>{rule.action}</td>
                        <td>{rule.modality || '*'}</td>
                        <td>{rule.tier || '*'}</td>
                        <td>{rule.credit_cost} / {rule.unit}</td>
                        <td>{rule.enabled ? 'enabled' : 'disabled'}</td>
                        <td style={{ textAlign: 'right' }}>
                            <button className="btn btn-ghost" onClick={() => update.mutate({ id: rule.id, data: { enabled: !rule.enabled } })}>{rule.enabled ? '停用' : '启用'}</button>
                            <button className="btn btn-ghost" style={{ color: 'var(--error)' }} onClick={() => remove.mutate(rule.id)}>删除</button>
                        </td>
                    </>
                )}
            />
        </div>
    );
}

function TenantsTab() {
    const qc = useQueryClient();
    const { data: tenants = [] } = useQuery({ queryKey: ['saas-tenants'], queryFn: () => fetchJson<TenantSummary[]>('/saas/tenants') });
    const { data: plans = [] } = useQuery({ queryKey: ['plans'], queryFn: () => fetchJson<Plan[]>('/subscription/plans') });
    const [selectedTenantId, setSelectedTenantId] = useState('');
    const [planId, setPlanId] = useState('');
    const [credits, setCredits] = useState('1000');

    const assign = useMutation({
        mutationFn: () => fetchJson('/saas/subscriptions/assign', {
            method: 'POST',
            body: JSON.stringify({
                tenant_ids: [selectedTenantId],
                plan_id: planId,
                confirm: true,
                audit_reason: 'saas_console_plan_assignment',
            }),
        }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-tenants'] }),
    });
    const grant = useMutation({
        mutationFn: () => fetchJson('/saas/credits/grant', {
            method: 'POST',
            body: JSON.stringify({
                tenant_ids: [selectedTenantId],
                amount: Number(credits),
                reason: 'manual_adjustment',
                confirm: true,
                audit_reason: 'saas_console_credit_grant',
            }),
        }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['saas-tenants'] }),
    });
    const initializeFree = useMutation({
        mutationFn: () => fetchJson<InitializeFreeResult>('/saas/subscriptions/initialize-free', {
            method: 'POST',
            body: JSON.stringify({
                confirm: true,
                audit_reason: 'saas_console_initialize_existing_tenants_free',
            }),
        }),
        onSuccess: (result) => {
            qc.invalidateQueries({ queryKey: ['saas-tenants'] });
            window.alert(`Free 初始化完成：新增 ${result.created} 个，跳过已有订阅 ${result.skipped_existing} 个。`);
        },
    });

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '2fr 1.5fr 1fr auto auto', gap: 10, alignItems: 'end' }}>
                <Field label="Tenant">
                    <select className="form-input" value={selectedTenantId} onChange={(e) => setSelectedTenantId(e.target.value)}>
                        <option value="">Select tenant</option>
                        {tenants.map((tenant) => <option key={tenant.tenant_id} value={tenant.tenant_id}>{tenant.tenant_name || tenant.tenant_id}</option>)}
                    </select>
                </Field>
                <Field label="Plan">
                    <select className="form-input" value={planId} onChange={(e) => setPlanId(e.target.value)}>
                        <option value="">Select plan</option>
                        {plans.map((plan) => <option key={plan.id} value={plan.id}>{plan.name} ({plan.code})</option>)}
                    </select>
                </Field>
                <Field label="Credits">
                    <input className="form-input" type="number" value={credits} onChange={(e) => setCredits(e.target.value)} />
                </Field>
                <button
                    className="btn btn-primary"
                    disabled={!selectedTenantId || !planId || assign.isPending}
                    onClick={() => {
                        if (window.confirm('确认要为该租户分配套餐吗？此操作会影响可用额度和席位。')) assign.mutate();
                    }}
                >
                    分配套餐
                </button>
                <button
                    className="btn btn-secondary"
                    disabled={!selectedTenantId || !Number(credits) || grant.isPending}
                    onClick={() => {
                        if (window.confirm(`确认要为该租户补充 ${Number(credits).toLocaleString()} Credits 吗？`)) grant.mutate();
                    }}
                >
                    补额度
                </button>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginBottom: 12 }}>
                <button
                    className="btn btn-secondary"
                    disabled={initializeFree.isPending}
                    onClick={() => {
                        if (window.confirm('确认为所有未订阅企业初始化 Free 套餐吗？已有订阅不会被降级。')) {
                            initializeFree.mutate();
                        }
                    }}
                >
                    初始化缺失 Free
                </button>
                <button className="btn btn-secondary" onClick={() => void downloadCsv('/saas/orders/export.csv', 'payment_orders.csv')}>导出订单</button>
                <button className="btn btn-secondary" onClick={() => void downloadCsv('/saas/credit-transactions/export.csv', 'credit_transactions.csv')}>导出流水</button>
            </div>
            <DataTable
                rows={tenants}
                empty="暂无租户"
                renderHeader={() => <><th>Tenant</th><th>Plan</th><th>Status</th><th>Seats</th><th>Credits</th></>}
                renderRow={(tenant) => (
                    <>
                        <td>{tenant.tenant_name || tenant.tenant_id}</td>
                        <td>{tenant.plan_code || '-'}</td>
                        <td>{tenant.subscription_status || '-'}</td>
                        <td>{tenant.seats_used}/{tenant.seats_total}</td>
                        <td>{tenant.credits_balance.toLocaleString()}</td>
                    </>
                )}
            />
        </div>
    );
}

const ISSUE_STATUS_LABELS: Record<ProductionIssue['status'], string> = {
    open: '待处理',
    acknowledged: '已确认',
    resolved: '已解决',
    ignored: '已忽略',
};

function ProductionIssuesTab() {
    const qc = useQueryClient();
    const [statusFilter, setStatusFilter] = useState<ProductionIssue['status']>('open');
    const summary = useQuery({
        queryKey: ['saas-production-issue-summary'],
        queryFn: () => fetchJson<ProductionIssueSummary>('/saas/production-issues/summary'),
        refetchInterval: 30_000,
    });
    const issues = useQuery({
        queryKey: ['saas-production-issues', statusFilter],
        queryFn: () => fetchJson<ProductionIssue[]>(`/saas/production-issues?status=${statusFilter}`),
        refetchInterval: 30_000,
    });
    const updateStatus = useMutation({
        mutationFn: ({ id, status }: { id: string; status: ProductionIssue['status'] }) => fetchJson<ProductionIssue>(
            `/saas/production-issues/${id}`,
            { method: 'PATCH', body: JSON.stringify({ status }) },
        ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['saas-production-issues'] });
            qc.invalidateQueries({ queryKey: ['saas-production-issue-summary'] });
        },
        onError: (error) => window.alert(`更新失败：${error instanceof Error ? error.message : String(error)}`),
    });
    const metrics = summary.data;

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, padding: 16 }}>
                <div style={{ fontSize: 14, fontWeight: 650, marginBottom: 6 }}>生产问题监控</div>
                <div style={{ color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                    每 30 秒聚合浏览器、API、WebSocket、模型调用和媒体任务异常。仅保存路由、错误类型、Trace ID 与运行元数据，不采集对话、提示词、请求正文或 API Key。
                </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(110px, 1fr))', gap: 10, marginBottom: 16 }}>
                {[
                    ['待处理', metrics?.open_total ?? 0],
                    ['严重', metrics?.open_critical ?? 0],
                    ['错误', metrics?.open_error ?? 0],
                    ['警告', metrics?.open_warning ?? 0],
                    ['24h 事件', metrics?.events_last_24h ?? 0],
                    ['24h 受影响企业', metrics?.affected_tenants_last_24h ?? 0],
                ].map(([label, value]) => (
                    <div key={String(label)} className="card" style={{ padding: 14 }}>
                        <div style={{ color: 'var(--text-tertiary)', fontSize: 11 }}>{label}</div>
                        <div style={{ fontSize: 22, fontWeight: 700, marginTop: 5 }}>{value}</div>
                    </div>
                ))}
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'end', gap: 10, marginBottom: 12 }}>
                <Field label="状态">
                    <select
                        className="form-input"
                        value={statusFilter}
                        onChange={(event) => setStatusFilter(event.target.value as ProductionIssue['status'])}
                    >
                        {Object.entries(ISSUE_STATUS_LABELS).map(([value, label]) => (
                            <option key={value} value={value}>{label}</option>
                        ))}
                    </select>
                </Field>
                <button
                    className="btn btn-secondary"
                    disabled={issues.isFetching || summary.isFetching}
                    onClick={() => {
                        void issues.refetch();
                        void summary.refetch();
                    }}
                >
                    立即刷新
                </button>
            </div>
            {(issues.error || summary.error) && (
                <div className="card" style={{ marginBottom: 12, padding: 14, color: 'var(--error)' }}>
                    监控数据加载失败：{String(issues.error || summary.error)}
                </div>
            )}
            <DataTable
                rows={issues.data ?? []}
                empty={issues.isLoading ? '正在读取生产问题…' : '当前状态没有问题'}
                renderHeader={() => <><th>问题</th><th>级别</th><th>次数 / 企业</th><th>版本 / Trace</th><th>最近发生</th><th /></>}
                renderRow={(issue) => (
                    <>
                        <td style={{ minWidth: 300, padding: '12px 8px' }}>
                            <strong>{issue.summary}</strong>
                            <div style={{ color: 'var(--text-tertiary)', fontSize: 11, marginTop: 4 }}>
                                {issue.source} · {issue.category} · {issue.error_code || 'unknown'}
                            </div>
                            <div style={{ color: 'var(--text-secondary)', fontSize: 11, marginTop: 3 }}>
                                {issue.operation || '-'} {issue.route || ''}
                            </div>
                        </td>
                        <td style={{ color: issue.severity === 'critical' ? 'var(--error)' : 'inherit' }}>{issue.severity}</td>
                        <td>{issue.event_count} 次 / {issue.affected_tenant_count} 家</td>
                        <td>
                            <div>{issue.release_version || '-'}</div>
                            <div style={{ color: 'var(--text-tertiary)', fontSize: 10, maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis' }} title={issue.last_trace_id || ''}>
                                {issue.last_trace_id || '无 Trace'}
                            </div>
                        </td>
                        <td style={{ whiteSpace: 'nowrap' }}>{new Date(issue.last_seen_at).toLocaleString()}</td>
                        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                            {issue.status === 'open' && (
                                <button className="btn btn-ghost" disabled={updateStatus.isPending} onClick={() => updateStatus.mutate({ id: issue.id, status: 'acknowledged' })}>确认</button>
                            )}
                            {!['resolved', 'ignored'].includes(issue.status) && (
                                <button className="btn btn-primary" disabled={updateStatus.isPending} onClick={() => updateStatus.mutate({ id: issue.id, status: 'resolved' })}>解决</button>
                            )}
                            {['resolved', 'ignored'].includes(issue.status) && (
                                <button className="btn btn-secondary" disabled={updateStatus.isPending} onClick={() => updateStatus.mutate({ id: issue.id, status: 'open' })}>重新打开</button>
                            )}
                        </td>
                    </>
                )}
            />
        </div>
    );
}

async function downloadCsv(path: string, filename: string) {
    const token = localStorage.getItem('token');
    const response = await fetch(`/api${path}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
        const text = await response.text().catch(() => '');
        window.alert(text || `导出失败：HTTP ${response.status}`);
        return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

function Field({ label, children }: { label: string; children: ReactNode }) {
    return (
        <label style={{ display: 'flex', flexDirection: 'column', gap: 5, fontSize: 12, color: 'var(--text-secondary)' }}>
            {label}
            {children}
        </label>
    );
}

function DataTable<T>({
    rows,
    empty,
    renderHeader,
    renderRow,
}: {
    rows: T[];
    empty: string;
    renderHeader: () => ReactNode;
    renderRow: (row: T) => ReactNode;
}) {
    return (
        <div className="card" style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                    <tr style={{ color: 'var(--text-tertiary)', textAlign: 'left' }}>{renderHeader()}</tr>
                </thead>
                <tbody>
                    {rows.map((row, index) => (
                        <tr key={index} style={{ borderTop: '1px solid var(--border-subtle)' }}>
                            {renderRow(row)}
                        </tr>
                    ))}
                    {rows.length === 0 && (
                        <tr>
                            <td colSpan={8} style={{ padding: 28, textAlign: 'center', color: 'var(--text-tertiary)' }}>{empty}</td>
                        </tr>
                    )}
                </tbody>
            </table>
        </div>
    );
}
