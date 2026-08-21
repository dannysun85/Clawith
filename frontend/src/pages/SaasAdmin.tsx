import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle, IconDatabase, IconPhotoVideo, IconReceipt, IconRoute, IconStack2, IconUsers, IconWallet } from '@tabler/icons-react';
import { fetchJson, enterpriseApi } from '../services/api';
import { MODALITIES } from '../constants/modalities';
import PlansTab from './enterprise-settings/tabs/PlansTab';
import AccountManagement from './AccountManagement';

const SAAS_TIERS = ['lite', 'pro', 'ultra'];
const LLM_ROUTE_MODALITIES = ['text', 'image', 'video'];

type SaasTab = 'plans' | 'packs' | 'rules' | 'model-routes' | 'media-routes' | 'tenants' | 'orders' | 'accounts' | 'production-issues';

type ModelRoute = {
    id: string;
    saas_tier: string;
    modality: string;
    llm_model_id: string;
    priority: number;
    fallback_route_id?: string | null;
    enabled: boolean;
};

type PlatformLlmModel = {
    id: string;
    provider: string;
    model: string;
    label: string;
    base_url?: string | null;
    max_output_tokens?: number | null;
    modality: string;
    tier: string;
    enabled: boolean;
    verification_status?: string | null;
    last_verified_at?: string | null;
    supports_tool_calling?: boolean | null;
    tool_calling_error?: string | null;
};

type LlmModelTestResult = {
    success: boolean;
    connection_success: boolean;
    reply?: string;
    connection_latency_ms: number;
    tool_calling_supported: boolean | null;
    tool_calling_latency_ms: number;
    capability_recorded: boolean;
    error?: string | null;
};

type MediaRoute = {
    modality: 'image' | 'audio' | 'music' | 'video';
    tier: 'lite' | 'pro' | 'ultra';
    route_purpose: 'media_generation';
    provider: string;
    routing_mode: 'automatic_failover';
    route_semantics?: 'account_pool_readiness_only';
    provider_order: string[];
    available_providers: string[];
    execution_strategies?: Array<{
        strategy: 'commercial_quality' | 'creative_exploration' | 'default';
        provider_order: string[];
        available_providers: string[];
        preferred_provider: string;
        alternate_provider: string;
        preferred_ready: boolean;
        executable_without_alternate_confirmation: boolean;
        alternate_confirmation_required: boolean;
    }>;
    primary_provider: string;
    degraded_providers: string[];
    capability_status: 'available' | 'degraded' | 'unavailable';
    reason_code?: string | null;
    recommended_action: string;
    evaluation_source: 'persisted_account_and_generation_receipts';
    readiness_status: 'unconfigured' | 'account_verification_required' | 'generation_unverified' | 'generation_observed';
    quality_evidence_status: 'not_reviewed';
    provider_readiness: Array<{
        provider: string;
        configured: boolean;
        account_verified: boolean;
        generation_observed: boolean;
        plan_tiers: string[];
        account_receipt?: Record<string, unknown> | null;
        generation_receipt?: Record<string, unknown> | null;
    }>;
    fallback_provider: string;
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
    volcengine_profile?: { model: string; resolution: string } | null;
    minimax_allowance?: {
        allowance_date: string;
        timezone: string;
        quota: number;
        used: number;
        remaining: number;
        tracked_accounts: number;
        eligible_accounts: number;
        excluded_accounts: number;
        accounts: Array<{ credential_id: string; label: string; quota: number; used: number; remaining: number; eligible: boolean }>;
    } | null;
    provider_quotes?: Record<string, {
        model: string;
        resolution: string;
        duration_seconds: number;
        credits: number;
        billing_basis: string;
        pricing_version: string;
    }>;
    pricing_version?: string | null;
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
    verification_source: 'connection_probe' | 'legacy_tool_probe';
    verified_at: string;
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

type PaymentOrder = {
    id: string;
    tenant_id: string;
    type: string;
    plan_id?: string | null;
    credits?: number | null;
    amount_cents: number;
    currency: string;
    provider: string;
    status: string;
    created_at: string;
    paid_at?: string | null;
};

type ManualOrderDisposition =
    | 'keep_pending'
    | 'mark_paid'
    | 'cancel_expired'
    | 'cancel_test'
    | 'cancel_invalid'
    | 'restore_pending';

type ManualOrderDecision = {
    id: string;
    order_id: string;
    tenant_id: string;
    disposition: ManualOrderDisposition;
    evidence_ref: string;
    reason: string;
    previous_status: string;
    resulting_status: string;
    rollback_of_decision_id?: string | null;
    created_at: string;
};

type ManualOrderDecisionResult = {
    order: PaymentOrder;
    decision: ManualOrderDecision;
    replayed: boolean;
};

type ManualOrderDecisionRequest = {
    order: PaymentOrder;
    disposition: ManualOrderDisposition;
    evidenceRef: string;
    reason: string;
    rollbackOfDecisionId?: string | null;
    idempotencyKey: string;
};

type ManualOrderDecisionDraft = ManualOrderDecisionRequest & {
    confirmed: boolean;
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
    resolution_reason?: string | null;
    auto_resolved: boolean;
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
    { key: 'model-routes', label: '输入理解路由', icon: <IconRoute size={15} stroke={1.7} /> },
    { key: 'media-routes', label: '媒体生成策略', icon: <IconPhotoVideo size={15} stroke={1.7} /> },
    { key: 'tenants', label: '租户订阅', icon: <IconUsers size={15} stroke={1.7} /> },
    { key: 'orders', label: '人工订单', icon: <IconReceipt size={15} stroke={1.7} /> },
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
            {tab === 'orders' && <ManualOrdersTab />}
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
    const { data: models = [] } = useQuery<PlatformLlmModel[]>({
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
            <PlatformModelPoolCard models={models} />
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

function PlatformModelPoolCard({ models }: { models: PlatformLlmModel[] }) {
    const qc = useQueryClient();
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState({
        provider: 'custom',
        model: '',
        label: '',
        base_url: '',
        max_output_tokens: '4096',
        modality: 'text',
        tier: 'standard',
    });
    const [testResult, setTestResult] = useState<{ modelId: string; result: LlmModelTestResult } | null>(null);

    const create = useMutation({
        mutationFn: () => fetchJson<PlatformLlmModel>('/enterprise/llm-models?platform=true', {
            method: 'POST',
            body: JSON.stringify({
                provider: form.provider.trim(),
                model: form.model.trim(),
                api_key: '',
                base_url: form.base_url.trim() || null,
                label: form.label.trim(),
                enabled: true,
                supports_vision: false,
                max_output_tokens: Number(form.max_output_tokens),
                modality: form.modality,
                tier: form.tier,
            }),
        }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['llm-models-platform-routes'] });
            setShowForm(false);
            setForm({
                provider: 'custom',
                model: '',
                label: '',
                base_url: '',
                max_output_tokens: '4096',
                modality: 'text',
                tier: 'standard',
            });
        },
    });
    const test = useMutation({
        mutationFn: (model: PlatformLlmModel) => fetchJson<LlmModelTestResult>('/enterprise/llm-test', {
            method: 'POST',
            body: JSON.stringify({
                provider: model.provider,
                model: model.model,
                base_url: model.base_url || null,
                model_id: model.id,
            }),
        }),
        onSuccess: (result, model) => {
            setTestResult({ modelId: model.id, result });
            qc.invalidateQueries({ queryKey: ['llm-models-platform-routes'] });
            qc.invalidateQueries({ queryKey: ['runtime-model-settings'] });
        },
    });
    const error = create.error || test.error;

    return (
        <div className="card" style={{ marginBottom: 16, padding: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'start' }}>
                <div>
                    <div style={{ fontSize: 14, fontWeight: 650 }}>平台模型池</div>
                    <div style={{ marginTop: 6, color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                        先在“Provider 账号池”保存并验证 provider 账号，再创建模型并执行连接与原生工具调用测试；只有保留当前配置验证 evidence 的模型才能进入租户运行时候选。
                    </div>
                </div>
                <button className="btn btn-ghost" onClick={() => setShowForm((value) => !value)}>
                    {showForm ? '取消' : '新增平台模型'}
                </button>
            </div>
            {showForm && (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(180px, 1fr))', gap: 10, alignItems: 'end', marginTop: 14 }}>
                    <Field label="Provider">
                        <input className="form-input" value={form.provider} onChange={(event) => setForm({ ...form, provider: event.target.value })} placeholder="custom" />
                    </Field>
                    <Field label="Model ID">
                        <input className="form-input" value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="provider model id" />
                    </Field>
                    <Field label="显示名称">
                        <input className="form-input" value={form.label} onChange={(event) => setForm({ ...form, label: event.target.value })} placeholder="平台模型名称" />
                    </Field>
                    <Field label="Base URL（可选）">
                        <input className="form-input" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://.../v1" />
                    </Field>
                    <Field label="最大输出 tokens">
                        <input className="form-input" type="number" min="1" value={form.max_output_tokens} onChange={(event) => setForm({ ...form, max_output_tokens: event.target.value })} />
                    </Field>
                    <button
                        className="btn btn-primary"
                        disabled={create.isPending || !form.provider.trim() || !form.model.trim() || !form.label.trim() || Number(form.max_output_tokens) < 1}
                        onClick={() => create.mutate()}
                    >
                        {create.isPending ? '保存中...' : '保存模型'}
                    </button>
                </div>
            )}
            {create.isSuccess && <div style={{ marginTop: 10, color: 'var(--success)', fontSize: 12 }}>平台模型已保存；请执行验证后再绑定路由。</div>}
            {error && <div style={{ marginTop: 10, color: 'var(--error)', fontSize: 12 }}>模型操作失败：{error instanceof Error ? error.message : String(error)}</div>}
            <div style={{ marginTop: 14, display: 'grid', gap: 8 }}>
                {models.length === 0 && <div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>暂无平台模型</div>}
                {models.map((model) => {
                    const result = testResult?.modelId === model.id ? testResult.result : null;
                    return (
                        <div key={model.id} style={{ borderTop: '1px solid var(--border)', paddingTop: 10, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                            <div style={{ minWidth: 0 }}>
                                <div style={{ fontSize: 13, fontWeight: 600 }}>{model.label}</div>
                                <div style={{ marginTop: 3, color: 'var(--text-tertiary)', fontSize: 11 }}>
                                    {model.provider}/{model.model} · {model.modality} · {model.verification_status || 'unverified'} · 工具调用 {model.supports_tool_calling == null ? '未验证' : model.supports_tool_calling ? '支持' : '不支持'}
                                </div>
                                {result && (
                                    <div style={{ marginTop: 4, color: result.connection_success && result.tool_calling_supported ? 'var(--success)' : 'var(--warning)', fontSize: 11 }}>
                                        连接 {result.connection_success ? `通过 ${result.connection_latency_ms}ms` : '失败'} · 原生工具调用 {result.tool_calling_supported === true ? `通过 ${result.tool_calling_latency_ms}ms` : result.tool_calling_supported === false ? '不支持' : '未确认'}
                                        {result.error ? ` · ${result.error}` : ''}
                                    </div>
                                )}
                            </div>
                            <button className="btn btn-ghost" disabled={test.isPending} onClick={() => test.mutate(model)}>
                                {test.isPending && test.variables?.id === model.id ? '验证中...' : '连接与工具验证'}
                            </button>
                        </div>
                    );
                })}
            </div>
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
                仅选择 Groups 的规划和上下文压缩模型；候选项必须有当前连接验证 evidence。原生工具调用是否支持属于独立诊断，不是规划/压缩模型的必需条件。模型对象、API Key 与 Credits 均由平台治理，不向租户暴露模型对象或密钥。
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
                            <option key={model.id} value={model.id}>{model.label} · {model.provider}/{model.model} · 已验证 {new Date(model.verified_at).toLocaleString()}</option>
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
                            <option key={model.id} value={model.id}>{model.label} · {model.provider}/{model.model} · 已验证 {new Date(model.verified_at).toLocaleString()}</option>
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
                    暂无可用候选模型。候选模型必须已启用并保留当前配置的连接验证 evidence。
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

const MEDIA_LABELS: Record<MediaRoute['modality'], { zh: string; en: string }> = {
    image: { zh: '图片', en: 'Image' },
    audio: { zh: '语音', en: 'Speech' },
    music: { zh: '音乐', en: 'Music' },
    video: { zh: '视频', en: 'Video' },
};

function MediaRoutesTab() {
    const { i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const { data: routes = [], isLoading, error } = useQuery({
        queryKey: ['saas-media-routes'],
        queryFn: () => fetchJson<MediaRoute[]>('/saas/media-routes'),
    });

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, padding: 16 }}>
                <div style={{ fontSize: 14, fontWeight: 650, marginBottom: 6 }}>
                    {isZh ? '媒体生成路由（平台统一配置）' : 'Media generation routes (platform-managed)'}
                </div>
                <div style={{ color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                    {isZh
                        ? '“输入理解路由”和“媒体生成执行策略”是两条独立链路。视频优先消耗 MiniMax Plan 每账号每日 3 次额度，额度耗尽后自动接续火山 Agent Plan；只有供应商明确拒绝且尚未接受任务时才切换，accepted/unknown 均禁止重复提交。火山档位固定为 Lite=Seedance 2.0-mini/480P、Pro=2.0-fast/720P、Ultra=标准 2.0/720P；Ultra 只有请求明确指定 1080P 才升级。页面严格区分账号 readiness、真实生成 receipt 与人工商用品质。'
                        : 'Input-understanding routes and media-generation execution strategies are separate chains. Video first uses the MiniMax Plan allowance of three runs per account per day, then continues with Volcengine Agent Plan after exhaustion. Failover is allowed only after an explicit provider rejection before acceptance; accepted or unknown submissions must never be duplicated. Volcengine tiers are fixed at Lite=Seedance 2.0-mini/480P, Pro=2.0-fast/720P, and Ultra=standard 2.0/720P; Ultra upgrades to 1080P only when explicitly requested. Account readiness, real generation receipts, and human commercial-quality review remain distinct.'}
                </div>
            </div>
            {error && (
                <div className="card" style={{ marginBottom: 16, padding: 14, color: 'var(--error)' }}>
                    {isZh ? '媒体路由加载失败：' : 'Failed to load media routes: '}{error instanceof Error ? error.message : String(error)}
                </div>
            )}
            <DataTable
                rows={routes}
                empty={isLoading ? (isZh ? '正在读取生产媒体路由…' : 'Loading production media routes…') : (isZh ? '暂无媒体路由' : 'No media routes')}
                renderHeader={() => <><th>{isZh ? '媒体生成策略' : 'Media strategy'}</th><th>Tier</th><th>MiniMax</th><th>{isZh ? '媒体参数' : 'Media parameters'}</th><th>{isZh ? '供应商报价' : 'Provider quote'}</th><th>{isZh ? '可用性' : 'Availability'}</th><th>{isZh ? '配置来源' : 'Source'}</th><th /></>}
                renderRow={(route) => <MediaRouteRow route={route} isZh={isZh} />}
            />
        </div>
    );
}

function MediaRouteRow({ route, isZh }: { route: MediaRoute; isZh: boolean }) {
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
        onError: (err) => window.alert(`${isZh ? '保存失败：' : 'Save failed: '}${err instanceof Error ? err.message : String(err)}`),
    });

    const setSetting = (key: string, value: string | number) => {
        setSettings((current) => ({ ...current, [key]: value }));
    };
    const payload: Record<string, unknown> = { model, enabled, ...settings };
    const providerLabel = (provider: string) => provider === 'volcengine_agent_plan'
        ? '火山 Agent Plan'
        : provider === 'minimax'
            ? 'MiniMax'
            : provider;
    const statusLabel = route.capability_status === 'available'
        ? (isZh ? '账号线路可路由' : 'Account route available')
        : route.capability_status === 'degraded'
            ? (isZh ? '仅降级线路可路由' : 'Fallback route only')
            : (isZh ? '不可路由' : 'Unavailable');
    const statusColor = route.capability_status === 'available'
        ? 'var(--success)'
        : route.capability_status === 'degraded'
            ? 'var(--warning)'
            : 'var(--error)';
    const readinessLabel = route.readiness_status === 'generation_observed'
        ? (isZh ? '真实生成成功，质量未评审' : 'Real generation observed; quality not reviewed')
        : route.readiness_status === 'generation_unverified'
            ? (isZh ? '账号已验证，生成未验证' : 'Account verified; generation unverified')
            : route.readiness_status === 'account_verification_required'
                ? (isZh ? '已配置，等待账号验证' : 'Configured; account verification required')
                : (isZh ? '尚未配置账号' : 'Account not configured');
    const receiptTime = (receipt: Record<string, unknown> | null | undefined, field: string) => {
        const value = receipt?.[field];
        return typeof value === 'string' && value ? new Date(value).toLocaleString() : null;
    };
    const strategyLabel = (strategy: string) => strategy === 'commercial_quality'
        ? (isZh ? '商用品质' : 'Commercial quality')
        : strategy === 'creative_exploration'
            ? (isZh ? '创意探索' : 'Creative exploration')
            : (isZh ? '默认策略' : 'Default strategy');

    return (
        <>
            <td style={{ minWidth: 180 }}>
                <strong>{isZh ? MEDIA_LABELS[route.modality].zh : MEDIA_LABELS[route.modality].en}</strong>
                <div style={{ color: 'var(--text-secondary)', fontSize: 11, marginTop: 4 }}>
                    {isZh ? '账号池策略基线：' : 'Account-pool baseline: '}{route.provider_order.map(providerLabel).join(' → ')}
                </div>
                <div style={{ color: 'var(--text-tertiary)', fontSize: 10, marginTop: 2 }}>
                    {isZh ? '当前可用：' : 'Currently available: '}{route.available_providers.length > 0
                        ? route.available_providers.map(providerLabel).join('、')
                        : (isZh ? '无' : 'None')}
                </div>
                <div style={{ color: statusColor, fontSize: 11, fontWeight: 650, marginTop: 4 }}>
                    {statusLabel} · {isZh ? '兼容策略基线 ' : 'Compatibility baseline '}{route.primary_provider ? providerLabel(route.primary_provider) : (isZh ? '无' : 'None')}
                </div>
                <div role="status" style={{ color: 'var(--text-secondary)', fontSize: 10, lineHeight: 1.5, marginTop: 3 }}>
                    {readinessLabel}. {isZh
                        ? '此处仅表示账号 readiness，不代表任务实际执行；实际 provider/model 只以任务 receipt 为准。'
                        : 'This reports account readiness only, not actual task execution. The task receipt is authoritative for the provider and model.'}{route.recommended_action ? ` ${route.recommended_action}` : ''}
                </div>
                <div style={{ marginTop: 5, display: 'grid', gap: 3 }}>
                    {(route.execution_strategies ?? []).map((strategy) => (
                        <div key={strategy.strategy} style={{ color: 'var(--text-secondary)', fontSize: 10 }}>
                            {strategyLabel(strategy.strategy)}：{strategy.provider_order.map(providerLabel).join(' → ')}；
                            {strategy.preferred_ready
                                ? (isZh ? `首选 ${providerLabel(strategy.preferred_provider)} 可执行` : `Preferred ${providerLabel(strategy.preferred_provider)} is executable`)
                                : strategy.alternate_provider
                                    ? (isZh
                                        ? `首选不可用，改用 ${providerLabel(strategy.alternate_provider)}${strategy.alternate_confirmation_required ? '需确认' : '自动接续'}`
                                        : `Preferred route unavailable; use ${providerLabel(strategy.alternate_provider)} ${strategy.alternate_confirmation_required ? 'after confirmation' : 'automatically'}`)
                                    : (isZh ? '当前不可执行' : 'Not currently executable')}
                        </div>
                    ))}
                </div>
                {route.modality === 'video' && route.volcengine_profile && (
                    <div style={{ color: 'var(--text-secondary)', fontSize: 10, marginTop: 5 }}>
                        {isZh ? '火山接续档位：' : 'Volcengine continuation tier: '}{route.volcengine_profile.model} / {route.volcengine_profile.resolution}
                    </div>
                )}
                {route.modality === 'video' && route.minimax_allowance && (
                    <div style={{ color: route.minimax_allowance.remaining > 0 ? 'var(--success)' : 'var(--warning)', fontSize: 10, marginTop: 3 }}>
                        {isZh ? 'MiniMax 日额度：已用 ' : 'MiniMax daily allowance: used '}{route.minimax_allowance.used}/{route.minimax_allowance.quota}{isZh ? '，剩余 ' : ', remaining '}{route.minimax_allowance.remaining} ({route.minimax_allowance.allowance_date})
                        {route.minimax_allowance.excluded_accounts > 0 && (isZh
                            ? `；${route.minimax_allowance.excluded_accounts} 个账号当前不可执行`
                            : `; ${route.minimax_allowance.excluded_accounts} account(s) currently unavailable`)}
                    </div>
                )}
                <div style={{ marginTop: 5, display: 'grid', gap: 3 }}>
                    {route.provider_readiness.map((item) => {
                        const accountTime = receiptTime(item.account_receipt, 'checked_at');
                        const generationTime = receiptTime(item.generation_receipt, 'completed_at');
                        return (
                            <div key={item.provider} style={{ color: 'var(--text-tertiary)', fontSize: 10 }}>
                                {providerLabel(item.provider)}: {item.configured ? (isZh ? '已配置' : 'configured') : (isZh ? '未配置' : 'not configured')} / {item.account_verified ? (isZh ? '账号已验证' : 'account verified') : (isZh ? '账号未验证' : 'account unverified')} / {item.generation_observed ? (isZh ? '生成已观察' : 'generation observed') : (isZh ? '生成未验证' : 'generation unverified')}
                                {item.plan_tiers.length > 0 && ` / plan=${item.plan_tiers.join(',')}`}
                                {accountTime && ` / ${isZh ? '鉴权' : 'auth'} ${accountTime}`}
                                {generationTime && ` / ${isZh ? '生成' : 'generation'} ${generationTime}`}
                            </div>
                        );
                    })}
                </div>
            </td>
            <td style={{ textTransform: 'uppercase' }}>{route.tier}</td>
            <td style={{ minWidth: 190 }}>
                <select className="form-input" value={model} onChange={(e) => setModel(e.target.value)}>
                    {route.valid_models.map((name) => <option key={name} value={name}>{name}</option>)}
                </select>
            </td>
            <td style={{ minWidth: 230 }}>
                <MediaQualityFields modality={route.modality} tier={route.tier} settings={settings} onChange={setSetting} isZh={isZh} />
            </td>
            <td style={{ whiteSpace: 'nowrap' }}>
                {route.modality === 'video' && route.provider_quotes
                    ? Object.entries(route.provider_quotes).map(([provider, quote]) => (
                        <div key={provider} style={{ fontSize: 10, marginBottom: 3 }}>
                            {providerLabel(provider)}：{quote.credits} Credits/{quote.duration_seconds}s
                        </div>
                    ))
                    : route.estimated_credits == null ? (isZh ? '按量' : 'Usage-based') : `${route.estimated_credits} Credits/${route.billing_unit}`}
            </td>
            <td style={{ minWidth: 130 }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> {isZh ? '路由启用' : 'Route enabled'}
                </label>
                <div style={{ color: statusColor, fontSize: 11 }}>
                    {route.available
                        ? `${route.available_providers.length} ${isZh ? '个账号验证路径' : 'verified account path(s)'} · ${readinessLabel}`
                        : !route.pool_available
                            ? readinessLabel
                            : !route.tool_enabled
                                ? (isZh ? '工具已停用' : 'Tool disabled')
                                : (isZh ? '路由已停用' : 'Route disabled')}
                </div>
            </td>
            <td>{route.source === 'override' ? (isZh ? '后台覆盖' : 'Admin override') : (isZh ? '系统默认' : 'System default')}</td>
            <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                <button className="btn btn-primary" disabled={update.isPending} onClick={() => update.mutate(payload)}>{isZh ? '保存' : 'Save'}</button>
                <button className="btn btn-ghost" disabled={update.isPending || route.source === 'default'} onClick={() => update.mutate({ reset_to_default: true })}>{isZh ? '恢复默认' : 'Restore default'}</button>
            </td>
        </>
    );
}

function MediaQualityFields({
    modality,
    tier,
    settings,
    onChange,
    isZh,
}: {
    modality: MediaRoute['modality'];
    tier: MediaRoute['tier'];
    settings: Record<string, string | number | boolean>;
    onChange: (key: string, value: string | number) => void;
    isZh: boolean;
}) {
    if (modality === 'image') return <span style={{ color: 'var(--text-tertiary)' }}>{isZh ? '由 image-01 输出规格决定' : 'Defined by the image-01 output contract'}</span>;
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
                            {pair.duration} {isZh ? '秒' : 'seconds'} / {pair.resolution}
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

function ManualOrdersTab() {
    const qc = useQueryClient();
    const [statusFilter, setStatusFilter] = useState<'pending' | 'all'>('pending');
    const [retryRequest, setRetryRequest] = useState<ManualOrderDecisionRequest | null>(null);
    const [decisionDraft, setDecisionDraft] = useState<ManualOrderDecisionDraft | null>(null);
    const [lastDecisionResult, setLastDecisionResult] = useState<ManualOrderDecisionResult | null>(null);
    const orders = useQuery({
        queryKey: ['saas-orders', statusFilter],
        queryFn: () => fetchJson<PaymentOrder[]>(
            `/saas/orders?limit=100${statusFilter === 'pending' ? '&status=pending' : ''}`,
        ),
        refetchInterval: 30_000,
    });
    const decisions = useQuery({
        queryKey: ['saas-order-decisions'],
        queryFn: () => fetchJson<ManualOrderDecision[]>('/saas/order-decisions?limit=500'),
        refetchInterval: 30_000,
    });
    const latestDecisionByOrder = useMemo(() => {
        const latest = new Map<string, ManualOrderDecision>();
        for (const decision of decisions.data ?? []) {
            if (!latest.has(decision.order_id)) latest.set(decision.order_id, decision);
        }
        return latest;
    }, [decisions.data]);
    const decideOrder = useMutation({
        mutationFn: (request: ManualOrderDecisionRequest) => {
            const path = request.disposition === 'mark_paid'
                ? `/saas/orders/${request.order.id}/mark-paid`
                : `/saas/orders/${request.order.id}/operator-decisions`;
            return fetchJson<ManualOrderDecisionResult>(path, {
                method: 'POST',
                headers: { 'Idempotency-Key': request.idempotencyKey },
                body: JSON.stringify({
                    expected_tenant_id: request.order.tenant_id,
                    expected_status: request.disposition === 'restore_pending' ? 'canceled' : 'pending',
                    disposition: request.disposition,
                    evidence_ref: request.evidenceRef,
                    reason: request.reason,
                    rollback_of_decision_id: request.rollbackOfDecisionId ?? null,
                }),
            });
        },
        onSuccess: (result) => {
            setRetryRequest(null);
            setDecisionDraft(null);
            setLastDecisionResult(result);
            qc.invalidateQueries({ queryKey: ['saas-orders'] });
            qc.invalidateQueries({ queryKey: ['saas-order-decisions'] });
            qc.invalidateQueries({ queryKey: ['saas-tenants'] });
        },
        onError: (_error, request) => setRetryRequest(request),
    });

    const openDecisionForm = (
        order: PaymentOrder,
        disposition: ManualOrderDisposition,
        rollbackOfDecisionId?: string,
    ) => {
        setLastDecisionResult(null);
        setDecisionDraft({
            order,
            disposition,
            evidenceRef: '',
            reason: '',
            rollbackOfDecisionId,
            confirmed: false,
            idempotencyKey: `manual-order-${crypto.randomUUID()}`,
        });
    };

    const decisionRequiresConfirmation = decisionDraft?.disposition !== 'keep_pending';
    const decisionFormValid = Boolean(
        decisionDraft
        && decisionDraft.evidenceRef.trim().length >= 8
        && decisionDraft.reason.trim().length >= 8
        && (!decisionRequiresConfirmation || decisionDraft.confirmed),
    );

    const submitDecision = () => {
        if (!decisionDraft || !decisionFormValid) return;
        decideOrder.mutate({
            order: decisionDraft.order,
            disposition: decisionDraft.disposition,
            evidenceRef: decisionDraft.evidenceRef.trim(),
            reason: decisionDraft.reason.trim(),
            rollbackOfDecisionId: decisionDraft.rollbackOfDecisionId,
            idempotencyKey: decisionDraft.idempotencyKey,
        });
    };

    return (
        <div>
            <div className="card" style={{ marginBottom: 16, padding: 14, color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.7 }}>
                这里只处理 provider=manual 的人工订单。每次保留、确认收款、取消或恢复都必须填写凭证与原因，并以企业、预期状态和幂等键锁定。微信等供应商订单只能由已验证的支付回调或主动查单完成，不能在此人工置为已支付。
            </div>
            {retryRequest && (
                <div className="card" style={{ marginBottom: 12, padding: 14, color: 'var(--warning)' }}>
                    上次操作结果未知。请使用同一个幂等键重试，避免重复发放。
                    <button className="btn btn-secondary" style={{ marginLeft: 10 }} disabled={decideOrder.isPending} onClick={() => decideOrder.mutate(retryRequest)}>重试上次操作</button>
                </div>
            )}
            {lastDecisionResult && (
                <div className="card" role="status" style={{ marginBottom: 12, padding: 14, color: 'var(--success)' }}>
                    处置已写入：订单状态 {lastDecisionResult.order.status}；审计凭证 {lastDecisionResult.decision.id}
                    {lastDecisionResult.replayed ? '（幂等重放，未重复执行）' : '（首次执行）'}。
                </div>
            )}
            {decisionDraft && (
                <section className="card" aria-labelledby="manual-order-decision-title" style={{ marginBottom: 12, padding: 16 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start', gap: 12, marginBottom: 14 }}>
                        <div>
                            <h2 id="manual-order-decision-title" style={{ margin: 0, fontSize: 16 }}>复核人工订单处置</h2>
                            <div style={{ marginTop: 6, color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.6 }}>
                                订单 {decisionDraft.order.id} · 企业 {decisionDraft.order.tenant_id} · {decisionDraft.order.currency} {(decisionDraft.order.amount_cents / 100).toFixed(2)} · {decisionDraft.disposition}
                            </div>
                        </div>
                        <button className="btn btn-secondary" disabled={decideOrder.isPending} onClick={() => setDecisionDraft(null)}>取消</button>
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
                        <Field label="凭证编号或工单引用">
                            <input
                                className="form-input"
                                autoFocus
                                value={decisionDraft.evidenceRef}
                                minLength={8}
                                placeholder="至少 8 个字符；不要填写密码或密钥"
                                onChange={(event) => setDecisionDraft({ ...decisionDraft, evidenceRef: event.target.value })}
                            />
                        </Field>
                        <Field label="处置原因">
                            <textarea
                                className="form-input"
                                rows={3}
                                value={decisionDraft.reason}
                                minLength={8}
                                placeholder="至少 8 个字符，说明业务依据"
                                onChange={(event) => setDecisionDraft({ ...decisionDraft, reason: event.target.value })}
                            />
                        </Field>
                    </div>
                    {decisionRequiresConfirmation && (
                        <label style={{ display: 'flex', alignItems: 'start', gap: 8, marginTop: 12, color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.6 }}>
                            <input
                                type="checkbox"
                                checked={decisionDraft.confirmed}
                                onChange={(event) => setDecisionDraft({ ...decisionDraft, confirmed: event.target.checked })}
                            />
                            <span>我已核对企业、金额、当前状态和凭证；确认写入不可重复的审计记录并执行 {decisionDraft.disposition}。</span>
                        </label>
                    )}
                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
                        <button className="btn btn-secondary" disabled={decideOrder.isPending} onClick={() => setDecisionDraft(null)}>返回订单</button>
                        <button className="btn btn-primary" disabled={!decisionFormValid || decideOrder.isPending} onClick={submitDecision}>
                            {decideOrder.isPending ? '正在提交…' : '提交处置'}
                        </button>
                    </div>
                </section>
            )}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'end', gap: 10, marginBottom: 12 }}>
                <Field label="状态">
                    <select className="form-input" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as 'pending' | 'all')}>
                        <option value="pending">待处理</option>
                        <option value="all">全部</option>
                    </select>
                </Field>
                <button className="btn btn-secondary" disabled={orders.isFetching} onClick={() => void orders.refetch()}>立即刷新</button>
            </div>
            {(orders.error || decisions.error || decideOrder.error) && (
                <div className="card" style={{ marginBottom: 12, padding: 14, color: 'var(--error)' }}>
                    订单操作失败：{String(orders.error || decisions.error || decideOrder.error)}
                </div>
            )}
            <DataTable
                rows={orders.data ?? []}
                empty={orders.isLoading ? '正在读取订单…' : '当前没有订单'}
                renderHeader={() => <><th>订单 / 企业</th><th>内容</th><th>金额</th><th>通道</th><th>状态</th><th>创建时间</th><th /></>}
                renderRow={(order) => {
                    const latestDecision = latestDecisionByOrder.get(order.id);
                    const restorable = Boolean(order.provider === 'manual'
                        && order.status === 'canceled'
                        && latestDecision
                        && ['cancel_expired', 'cancel_test', 'cancel_invalid'].includes(latestDecision.disposition));
                    return <>
                        <td style={{ minWidth: 230 }}>
                            <strong title={order.id}>{order.id}</strong>
                            <div style={{ color: 'var(--text-tertiary)', fontSize: 10, marginTop: 4 }} title={order.tenant_id}>企业 {order.tenant_id}</div>
                        </td>
                        <td>{order.type === 'topup' ? `${(order.credits ?? 0).toLocaleString()} Credits` : order.type}</td>
                        <td>{order.currency} {(order.amount_cents / 100).toFixed(2)}</td>
                        <td>{order.provider}</td>
                        <td>
                            {order.status === 'pending' && order.provider === 'manual' ? '待人工处理' : order.status}
                            {latestDecision && <div style={{ color: 'var(--text-tertiary)', fontSize: 10, marginTop: 4 }}>最近处置：{latestDecision.disposition}</div>}
                        </td>
                        <td style={{ whiteSpace: 'nowrap' }}>{new Date(order.created_at).toLocaleString()}</td>
                        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                            {order.provider === 'manual' && order.status === 'pending' && (
                                <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 6 }}>
                                    <button className="btn btn-primary" disabled={decideOrder.isPending} onClick={() => openDecisionForm(order, 'mark_paid')}>确认人工收款</button>
                                    <button className="btn btn-secondary" disabled={decideOrder.isPending} onClick={() => openDecisionForm(order, 'keep_pending')}>保留待处理</button>
                                    <button className="btn btn-secondary" disabled={decideOrder.isPending} onClick={() => openDecisionForm(order, 'cancel_expired')}>取消过期</button>
                                    <button className="btn btn-secondary" disabled={decideOrder.isPending} onClick={() => openDecisionForm(order, 'cancel_test')}>取消测试单</button>
                                </div>
                            )}
                            {restorable && (
                                <button className="btn btn-secondary" disabled={decideOrder.isPending} onClick={() => openDecisionForm(order, 'restore_pending', latestDecision!.id)}>撤销取消</button>
                            )}
                            {order.provider !== 'manual' && order.status === 'pending' && (
                                <span style={{ color: 'var(--text-tertiary)', fontSize: 11 }}>等待供应商凭证</span>
                            )}
                        </td>
                    </>;
                }}
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
                            {issue.resolution_reason && (
                                <div style={{ color: 'var(--text-tertiary)', fontSize: 10, marginTop: 3 }}>
                                    {issue.auto_resolved ? '自动解决' : '人工解决'}：{issue.resolution_reason}
                                </div>
                            )}
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
