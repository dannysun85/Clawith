import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { fetchJson } from '../utils/fetchJson';
import { MODALITIES } from '../../../constants/modalities';
import {
    buildPlanUpdatePayload,
    GENERATION_MODALITIES,
    getPlanGenerationSettings,
    type PlanEditorForm,
    planEditorFormIsDirty,
    planToEditorForm,
} from '../utils/planGenerationFeatures';

interface PlanOut {
    id: string;
    code: string;
    name: string;
    tier: number;
    period: string;
    price_cents: number;
    currency: string;
    max_agents: number;
    max_llm_calls_per_day: number;
    message_limit: number;
    message_period: string;
    max_triggers: number;
    credits_per_period: number;
    allowed_modalities: string[] | null;
    allowed_tiers: string[] | null;
    features: Record<string, unknown> | null;
    is_active: boolean;
    sort_order: number;
    updated_at: string;
}

const TIERS = ['lite', 'pro', 'ultra'];

export default function PlansTab() {
    const { t } = useTranslation();
    const qc = useQueryClient();
    const { data: plans = [] } = useQuery({
        queryKey: ['plans'],
        queryFn: () => fetchJson<PlanOut[]>('/subscription/plans'),
    });

    const updatePlan = useMutation<PlanOut, Error, { id: string; data: unknown }>({
        mutationFn: ({ id, data }: { id: string; data: unknown }) =>
            fetchJson<PlanOut>(`/subscription/plans/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
        onSuccess: (updated) => {
            qc.setQueryData<PlanOut[]>(['plans'], (current) =>
                current?.map((plan) => plan.id === updated.id ? updated : plan),
            );
            return qc.invalidateQueries({ queryKey: ['plans'] });
        },
        onError: () => qc.invalidateQueries({ queryKey: ['plans'] }),
    });
    const createPlan = useMutation({
        mutationFn: (data: unknown) =>
            fetchJson('/subscription/plans', { method: 'POST', body: JSON.stringify(data) }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['plans'] }),
    });

    const [showCreate, setShowCreate] = useState(false);
    const [newPlan, setNewPlan] = useState({ code: '', name: '' });

    return (
        <div style={{ padding: '16px 0' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <h3 style={{ margin: 0 }}>{t('enterprise.plans.title', '套餐管理')}</h3>
                <button className="btn btn-primary" onClick={() => setShowCreate((v) => !v)}>
                    + {t('enterprise.plans.add', '新建套餐')}
                </button>
            </div>
            <p style={{ color: 'var(--text-tertiary)', fontSize: 12, marginBottom: 16 }}>
                {t(
                    'enterprise.plans.desc',
                    '分别配置对话模型路由和媒体生成能力。两组权限独立生效，后端 entitlement 会在真实调用时兜底校验。'
                )}
            </p>

            {showCreate && (
                <div className="card" style={{ marginBottom: 16, display: 'flex', gap: 8, alignItems: 'end' }}>
                    <div className="form-group">
                        <label className="form-label">code</label>
                        <input className="form-input" value={newPlan.code} onChange={(e) => setNewPlan({ ...newPlan, code: e.target.value })} placeholder="pro" />
                    </div>
                    <div className="form-group">
                        <label className="form-label">name</label>
                        <input className="form-input" value={newPlan.name} onChange={(e) => setNewPlan({ ...newPlan, name: e.target.value })} placeholder="Pro" />
                    </div>
                    <button
                        className="btn btn-primary"
                        disabled={!newPlan.code || !newPlan.name}
                        onClick={() => {
                            createPlan.mutate({ code: newPlan.code, name: newPlan.name });
                            setNewPlan({ code: '', name: '' });
                            setShowCreate(false);
                        }}
                    >
                        {t('common.save')}
                    </button>
                </div>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {plans.map((p) => (
                    <PlanCard
                        key={p.id}
                        plan={p}
                        onSave={(data) => updatePlan.mutateAsync({ id: p.id, data })}
                        saving={updatePlan.isPending}
                    />
                ))}
                {plans.length === 0 && <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-tertiary)' }}>{t('common.noData')}</div>}
            </div>
        </div>
    );
}

function PlanCard({
    plan,
    onSave,
    saving,
}: {
    plan: PlanOut;
    onSave: (data: unknown) => Promise<PlanOut>;
    saving: boolean;
}) {
    const { t } = useTranslation();
    const [baselinePlan, setBaselinePlan] = useState(plan);
    const [form, setForm] = useState<PlanEditorForm>(() => planToEditorForm(plan));
    const [conflictPlan, setConflictPlan] = useState<PlanOut | null>(null);
    const [saved, setSaved] = useState(false);
    const generation = getPlanGenerationSettings(
        baselinePlan.features,
        baselinePlan.allowed_modalities,
        baselinePlan.allowed_tiers,
    );
    const featureSource = baselinePlan.features ?? {};
    const malformedGenerationModalities =
        Object.prototype.hasOwnProperty.call(featureSource, 'generation_modalities') &&
        !Array.isArray(featureSource.generation_modalities);
    const malformedGenerationTiers =
        Object.prototype.hasOwnProperty.call(featureSource, 'generation_tiers') &&
        !Array.isArray(featureSource.generation_tiers);

    const toggle = (
        key: 'allowed_modalities' | 'allowed_tiers' | 'generation_modalities' | 'generation_tiers',
        val: string,
    ) => {
        setForm((f) => {
            const arr = f[key] as string[];
            return { ...f, [key]: arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val] };
        });
    };

    const dirty = planEditorFormIsDirty(form, baselinePlan);

    useEffect(() => {
        if (plan.updated_at === baselinePlan.updated_at) return;
        const incomingTime = Date.parse(plan.updated_at);
        const baselineTime = Date.parse(baselinePlan.updated_at);
        if (Number.isFinite(incomingTime) && Number.isFinite(baselineTime) && incomingTime < baselineTime) {
            return;
        }
        if (dirty) {
            if (conflictPlan?.updated_at !== plan.updated_at) setConflictPlan(plan);
            return;
        }
        setBaselinePlan(plan);
        setForm(planToEditorForm(plan));
        setConflictPlan(null);
    }, [baselinePlan, conflictPlan?.updated_at, dirty, plan]);

    const loadLatestPlan = (latest: PlanOut) => {
        setBaselinePlan(latest);
        setForm(planToEditorForm(latest));
        setConflictPlan(null);
        setSaved(false);
    };

    return (
        <div className="card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <div>
                    <strong>{plan.name}</strong>{' '}
                    <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>
                        code={plan.code} · {plan.period} · {plan.currency} {(plan.price_cents / 100).toFixed(2)}
                    </span>
                </div>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, cursor: 'pointer' }}>
                    <input type="checkbox" checked={form.is_active} onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))} />
                    {t('enterprise.plans.active', '生效')}
                </label>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.allowedModalities', '对话模型类型')}</label>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {MODALITIES.map((m) => (
                            <label
                                key={m}
                                style={{
                                    display: 'flex', alignItems: 'center', gap: 4, fontSize: 12,
                                    padding: '2px 8px', border: '1px solid var(--border-subtle)', borderRadius: 4,
                                    cursor: 'pointer', background: form.allowed_modalities.includes(m) ? 'var(--bg-secondary)' : 'transparent',
                                }}
                            >
                                <input type="checkbox" checked={form.allowed_modalities.includes(m)} onChange={() => toggle('allowed_modalities', m)} />
                                {m}
                            </label>
                        ))}
                    </div>
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.allowedTiers', '对话模型档位')}</label>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {TIERS.map((tier) => (
                            <label
                                key={tier}
                                style={{
                                    display: 'flex', alignItems: 'center', gap: 4, fontSize: 12,
                                    padding: '2px 8px', border: '1px solid var(--border-subtle)', borderRadius: 4,
                                    cursor: 'pointer', background: form.allowed_tiers.includes(tier) ? 'var(--bg-secondary)' : 'transparent',
                                }}
                            >
                                <input type="checkbox" checked={form.allowed_tiers.includes(tier)} onChange={() => toggle('allowed_tiers', tier)} />
                                {tier}
                            </label>
                        ))}
                    </div>
                </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.generationModalities', '媒体生成能力')}</label>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {GENERATION_MODALITIES.map((modality) => (
                            <label
                                key={modality}
                                style={{
                                    display: 'flex', alignItems: 'center', gap: 4, fontSize: 12,
                                    padding: '2px 8px', border: '1px solid var(--border-subtle)', borderRadius: 4,
                                    cursor: 'pointer', background: form.generation_modalities.includes(modality) ? 'var(--bg-secondary)' : 'transparent',
                                }}
                            >
                                <input
                                    type="checkbox"
                                    checked={form.generation_modalities.includes(modality)}
                                    onChange={() => toggle('generation_modalities', modality)}
                                />
                                {modality}
                            </label>
                        ))}
                    </div>
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.generationTiers', '媒体生成档位')}</label>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {TIERS.map((tier) => (
                            <label
                                key={tier}
                                style={{
                                    display: 'flex', alignItems: 'center', gap: 4, fontSize: 12,
                                    padding: '2px 8px', border: '1px solid var(--border-subtle)', borderRadius: 4,
                                    cursor: 'pointer', background: form.generation_tiers.includes(tier) ? 'var(--bg-secondary)' : 'transparent',
                                }}
                            >
                                <input
                                    type="checkbox"
                                    checked={form.generation_tiers.includes(tier)}
                                    onChange={() => toggle('generation_tiers', tier)}
                                />
                                {tier}
                            </label>
                        ))}
                    </div>
                </div>
            </div>
            <div style={{ marginTop: 6, color: 'var(--text-tertiary)', fontSize: 11 }}>
                对话权限未勾选时沿用兼容模式（不限制）；媒体生成未勾选时表示禁用全部媒体生成。
                {(generation.preservedModalities.length + generation.preservedTiers.length) > 0 && (
                    <span style={{ marginLeft: 8, color: 'var(--warning)' }}>
                        已保留 {generation.preservedModalities.length + generation.preservedTiers.length} 个当前界面未识别的扩展值，保存不会删除。
                    </span>
                )}
                {(malformedGenerationModalities || malformedGenerationTiers) && (
                    <span style={{ marginLeft: 8, color: 'var(--error)' }}>
                        检测到旧版媒体配置格式异常；请调整对应媒体生成选项后再保存，系统不会静默覆盖原值。
                    </span>
                )}
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginTop: 12 }}>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.maxAgents', '最大 Agent')}</label>
                    <input className="form-input" type="number" value={form.max_agents} onChange={(e) => setForm((f) => ({ ...f, max_agents: Number(e.target.value) }))} />
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.maxLlmCalls', '每日 LLM')}</label>
                    <input className="form-input" type="number" value={form.max_llm_calls_per_day} onChange={(e) => setForm((f) => ({ ...f, max_llm_calls_per_day: Number(e.target.value) }))} />
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.messageLimit', '消息配额')}</label>
                    <input className="form-input" type="number" value={form.message_limit} onChange={(e) => setForm((f) => ({ ...f, message_limit: Number(e.target.value) }))} />
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.messagePeriod', '消息周期')}</label>
                    <select className="form-input" value={form.message_period} onChange={(e) => setForm((f) => ({ ...f, message_period: e.target.value }))}>
                        <option value="permanent">permanent</option>
                        <option value="daily">daily</option>
                        <option value="weekly">weekly</option>
                        <option value="monthly">monthly</option>
                    </select>
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }}>{t('enterprise.plans.maxTriggers', '触发器')}</label>
                    <input className="form-input" type="number" value={form.max_triggers} onChange={(e) => setForm((f) => ({ ...f, max_triggers: Number(e.target.value) }))} />
                </div>
                <div>
                    <label className="form-label" style={{ fontSize: 12 }} title="每周期 token 预算（单位：千 token，1 credit ≈ 1000 tokens）">
                        {t('enterprise.plans.creditsPerPeriod', 'Token 额度(credits)')}
                    </label>
                    <input className="form-input" type="number" value={form.credits_per_period} onChange={(e) => setForm((f) => ({ ...f, credits_per_period: Number(e.target.value) }))} placeholder="0=不限" />
                </div>
                <div style={{ gridColumn: 'span 2' }}>
                    <label className="form-label" style={{ fontSize: 12 }} title="套餐特性 JSON，如 {&quot;priority_support&quot;: true}">
                        {t('enterprise.plans.features', '其他特性 (JSON)')}
                    </label>
                    <input className="form-input" value={form.features} onChange={(e) => setForm((f) => ({ ...f, features: e.target.value }))} placeholder='{"priority_support": true}' />
                </div>
            </div>

            <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', alignItems: 'center', marginTop: 12 }}>
                {conflictPlan && (
                    <span style={{ marginRight: 'auto', fontSize: 12, color: 'var(--warning)' }}>
                        套餐已被其他管理员更新。
                        <button
                            className="btn btn-ghost"
                            style={{ marginLeft: 6 }}
                            onClick={() => loadLatestPlan(conflictPlan)}
                        >
                            载入最新数据
                        </button>
                    </span>
                )}
                <input className="form-input" type="number" style={{ width: 120 }} value={form.price_cents} onChange={(e) => setForm((f) => ({ ...f, price_cents: Number(e.target.value) }))} title="price_cents" />
                <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{t('enterprise.plans.cents', '分')}</span>
                {saved && <span style={{ fontSize: 12, color: 'var(--success)' }}>✓</span>}
                <button
                    className="btn btn-primary"
                    disabled={!dirty || saving || Boolean(conflictPlan)}
                    onClick={async () => {
                        let payload: Record<string, unknown>;
                        try {
                            payload = buildPlanUpdatePayload(form, baselinePlan);
                        } catch (error) {
                            alert(error instanceof Error ? error.message : '其他特性不是合法 JSON 对象');
                            return;
                        }

                        try {
                            const updated = await onSave(payload);
                            loadLatestPlan(updated);
                            setSaved(true);
                            setTimeout(() => setSaved(false), 1500);
                        } catch (error) {
                            alert(`保存失败：${error instanceof Error ? error.message : '未知错误'}`);
                        }
                    }}
                >
                    {t('common.save')}
                </button>
            </div>
        </div>
    );
}
