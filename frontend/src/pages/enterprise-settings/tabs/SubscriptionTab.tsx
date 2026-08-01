import { useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { IconCheck, IconShoppingCart } from '@tabler/icons-react';
import { fetchJson } from '../utils/fetchJson';
import { Entitlements } from '../../../hooks/useLlmModels';

interface Usage {
    period_date: string;
    llm_calls_used: number;
    llm_calls_limit: number;
    messages_used: number;
    messages_limit: number;
    tokens_used: number;
    credits_balance: number;
}

interface SeatUsage {
    seats_total: number;
    seats_used: number;
    pending_invites: number;
}

interface Plan {
    id: string;
    code: string;
    name: string;
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
    sort_order: number;
}

interface CreditPack {
    id: string;
    code: string;
    name: string;
    credits: number;
    price_cents: number;
    currency: string;
}

interface PaymentOrder {
    id: string;
    provider: string;
    amount_cents: number;
    currency: string;
    status: string;
    session_url?: string | null;
}

const STATUS_LABEL: Record<string, string> = {
    active: '生效中',
    trialing: '试用中',
    canceled: '已取消(周期末失效)',
    expired: '已过期',
    past_due: '续费失败(宽限中)',
    none: '无订阅(使用默认配额)',
};

const formatMoney = (currency: string, cents: number) => {
    const prefix = currency === 'CNY' ? '¥' : '$';
    return `${prefix}${(cents / 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
};

const planDisplayName = (plan: Plan) => {
    const mapped: Record<string, string> = {
        free: '免费版',
        starter: '入门版',
        pro: '专业版',
        scale: '规模版',
        enterprise: '规模版',
    };
    return String(plan.features?.display_name || mapped[plan.code] || plan.name);
};

const featureNumber = (features: Record<string, unknown> | null, key: string, fallback: number) => {
    const value = features?.[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
};

const yearlyDiscountPercent = (plan: Plan) => featureNumber(plan.features, 'yearly_discount_percent', plan.price_cents > 0 ? 20 : 0);

const planPrice = (plan: Plan, period: 'monthly' | 'yearly') => {
    if (period === 'monthly') {
        return {
            current: featureNumber(plan.features, 'monthly_price_cents', plan.price_cents),
            original: plan.price_cents > 0 ? featureNumber(plan.features, 'monthly_original_price_cents', Math.round(plan.price_cents / 0.8)) : 0,
        };
    }
    const original = featureNumber(plan.features, 'yearly_original_price_cents', plan.price_cents * 12);
    const discount = yearlyDiscountPercent(plan);
    return {
        current: featureNumber(plan.features, 'yearly_price_cents', Math.round(original * (1 - discount / 100))),
        original,
    };
};

const boostDiscountLine = (plan: Plan) => {
    const explicit = featureNumber(plan.features, 'boost_discount_percent', 0);
    const byCode: Record<string, number> = { pro: 10, scale: 20, enterprise: 20 };
    const discount = explicit || byCode[plan.code] || 0;
    return discount > 0 ? `加餐包 ${discount}% off` : null;
};

const formatUnitPrice = (currency: string, priceCents: number, credits: number) => {
    if (!credits) return '';
    const prefix = currency === 'CNY' ? '¥' : '$';
    const unit = (priceCents / 100 / credits * 100).toFixed(2);
    return `(${prefix}${unit}/100)`;
};

export default function SubscriptionTab({ showMarketplace = true }: { showMarketplace?: boolean }) {
    const { t } = useTranslation();
    const qc = useQueryClient();
    const navigate = useNavigate();
    const [billingPeriod, setBillingPeriod] = useState<'monthly' | 'yearly'>('monthly');
    const [lastOrder, setLastOrder] = useState<PaymentOrder | null>(null);

    const { data: ent } = useQuery({
        queryKey: ['subscription-entitlements'],
        queryFn: () => fetchJson<Entitlements | null>('/subscription/my-entitlements'),
    });
    const { data: usage } = useQuery({
        queryKey: ['subscription-usage'],
        queryFn: () => fetchJson<Usage | null>('/subscription/usage'),
        refetchInterval: 30000,
    });
    const { data: seats } = useQuery({
        queryKey: ['subscription-seats'],
        queryFn: () => fetchJson<SeatUsage>('/subscription/seats'),
    });
    const { data: plans = [] } = useQuery({
        queryKey: ['plans'],
        queryFn: () => fetchJson<Plan[]>('/subscription/plans'),
        enabled: showMarketplace,
    });
    const { data: creditPacks = [] } = useQuery({
        queryKey: ['subscription-credit-packs'],
        queryFn: () => fetchJson<CreditPack[]>('/subscription/credit-packs'),
        enabled: showMarketplace,
    });

    const invalidateBillingQueries = () => {
        qc.invalidateQueries({ queryKey: ['subscription-entitlements'] });
        qc.invalidateQueries({ queryKey: ['subscription-usage'] });
        qc.invalidateQueries({ queryKey: ['subscription-seats'] });
        qc.invalidateQueries({ queryKey: ['subscription-summary'] });
        qc.invalidateQueries({ queryKey: ['subscription-orders'] });
        qc.invalidateQueries({ queryKey: ['subscription-credit-transactions'] });
    };

    const handleCheckoutOrder = (order: PaymentOrder) => {
        setLastOrder(order);
        invalidateBillingQueries();
        if (order.session_url) {
            window.location.assign(order.session_url);
            return;
        }
        if (order.provider !== 'manual') {
            navigate(`/billing/success?order_id=${order.id}`);
        }
    };

    const checkoutSubscribe = useMutation({
        mutationFn: (planId: string) => fetchJson<PaymentOrder>('/subscription/checkout/subscribe', {
            method: 'POST',
            body: JSON.stringify({ plan_id: planId, period: billingPeriod, seats: 1 }),
        }),
        onSuccess: handleCheckoutOrder,
    });

    const checkoutTopup = useMutation({
        mutationFn: (creditPackId: string) => fetchJson<PaymentOrder>('/subscription/checkout/topup', {
            method: 'POST',
            body: JSON.stringify({ credit_pack_id: creditPackId }),
        }),
        onSuccess: handleCheckoutOrder,
    });

    const status = ent?.subscription_status || 'none';
    const planCode = ent?.plan_code || 'free';
    const sortedPlans = useMemo(() => [...plans].sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0)), [plans]);

    if (showMarketplace) {
        return (
            <div style={{ padding: '16px 0 30px' }}>
                <section style={{ border: '1px solid var(--border-subtle)', borderRadius: 8, background: 'var(--bg-primary)', padding: '32px 40px 30px' }}>
                    <div style={{ textAlign: 'center', marginBottom: 28 }}>
                        <h2 style={{ margin: '0 0 8px', fontSize: 24, fontWeight: 700 }}>{t('enterprise.subscription.marketTitle', '和你一起成长的套餐')}</h2>
                        <p style={{ margin: '0 0 22px', color: 'var(--text-tertiary)', fontSize: 13 }}>
                            {t('enterprise.subscription.marketDesc', '选择适合团队的方案，随时可切换。')}
                        </p>
                        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 12, borderBottom: '1px solid var(--border-subtle)' }}>
                            {(['monthly', 'yearly'] as const).map((period) => (
                                <button
                                    key={period}
                                    type="button"
                                    onClick={() => setBillingPeriod(period)}
                                    style={{
                                        border: 'none',
                                        background: 'transparent',
                                        padding: '8px 4px',
                                        cursor: 'pointer',
                                        color: billingPeriod === period ? 'var(--text-primary)' : 'var(--text-tertiary)',
                                        borderBottom: billingPeriod === period ? '2px solid var(--text-primary)' : '2px solid transparent',
                                        fontWeight: billingPeriod === period ? 650 : 500,
                                        minWidth: 46,
                                    }}
                                >
                                    {period === 'monthly' ? t('enterprise.subscription.monthly', '月付') : t('enterprise.subscription.yearly', '年付')}
                                    {period === 'yearly' && <span style={{ marginLeft: 6, color: 'var(--success)', fontSize: 11, background: 'var(--success-subtle)', padding: '2px 6px', borderRadius: 999 }}>省 {yearlyDiscountPercent(sortedPlans.find((p) => p.price_cents > 0) || sortedPlans[0] || ({ price_cents: 0, features: null } as Plan))}%</span>}
                                </button>
                            ))}
                        </div>
                    </div>

                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(190px, 1fr))', gap: 16, maxWidth: 1240, margin: '0 auto 36px' }}>
                        {sortedPlans.map((plan) => {
                            const isCurrent = plan.code === planCode || plan.id === ent?.plan_id;
                            const recommended = plan.features?.recommended === true || plan.code === 'pro';
                            const price = planPrice(plan, billingPeriod);
                            const boostLine = boostDiscountLine(plan);
                            return (
                                <div
                                    key={plan.id}
                                    style={{
                                        minHeight: 318,
                                        display: 'flex',
                                        flexDirection: 'column',
                                        gap: 14,
                                        border: `1px solid ${recommended ? 'var(--text-primary)' : 'var(--border-subtle)'}`,
                                        borderRadius: 8,
                                        padding: '24px 24px 22px',
                                        position: 'relative',
                                        background: 'var(--bg-primary)',
                                    }}
                                >
                                    {recommended && (
                                        <span style={{ position: 'absolute', right: 18, top: -10, background: 'var(--success)', color: '#fff', borderRadius: 4, padding: '3px 8px', fontSize: 11, fontWeight: 650 }}>
                                            推荐
                                        </span>
                                    )}
                                    <div>
                                        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 10 }}>{planDisplayName(plan)}</div>
                                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, minHeight: 42 }}>
                                            <span style={{ fontSize: 32, lineHeight: 1, fontWeight: 750 }}>{formatMoney(plan.currency, price.current)}</span>
                                            <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>/ {billingPeriod === 'yearly' ? t('common.year', '年') : t('common.month', '月')}</span>
                                            {price.original > price.current && (
                                                <span style={{ color: 'var(--text-tertiary)', fontSize: 12, textDecoration: 'line-through' }}>{formatMoney(plan.currency, price.original)}</span>
                                            )}
                                        </div>
                                        <div style={{ color: plan.price_cents > 0 ? 'var(--success)' : 'var(--text-tertiary)', fontSize: 12, marginTop: 8 }}>
                                            {plan.price_cents > 0
                                                ? billingPeriod === 'yearly'
                                                    ? '首年 · 按年计费'
                                                    : '首月 · 按月计费'
                                                : billingPeriod === 'yearly'
                                                    ? '按年计费'
                                                    : '按月计费'}
                                        </div>
                                    </div>

                                    <div style={{ height: 1, background: 'var(--border-subtle)', margin: '8px 0 2px' }} />
                                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 4, flexWrap: 'nowrap' }}>
                                        <span style={{ fontSize: plan.credits_per_period >= 100_000 ? 26 : 28, fontWeight: 750 }}>{plan.credits_per_period.toLocaleString()}</span>
                                        <span style={{ color: 'var(--text-tertiary)', fontSize: 11, whiteSpace: 'nowrap' }}>Credits / 月</span>
                                    </div>
                                    <div style={{ display: 'grid', gap: 9, color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.4 }}>
                                        <PlanBullet>{plan.max_agents} 个公开 Agent 坐席</PlanBullet>
                                        {boostLine && <PlanBullet>{boostLine}</PlanBullet>}
                                        <PlanBullet>{(plan.allowed_tiers?.length || 0) >= 3 || !plan.allowed_tiers?.length ? '解锁全部模型档位' : `可用 ${plan.allowed_tiers.join(', ')}`}</PlanBullet>
                                    </div>

                                    <button
                                        className={isCurrent ? 'btn btn-secondary' : 'btn btn-primary'}
                                        disabled={isCurrent || checkoutSubscribe.isPending}
                                        style={{ marginTop: 'auto', width: '100%', justifyContent: 'center' }}
                                        onClick={() => checkoutSubscribe.mutate(plan.id)}
                                    >
                                        {isCurrent ? t('enterprise.subscription.current', '当前使用的套餐') : t('enterprise.subscription.upgrade', '升级')}
                                    </button>
                                </div>
                            );
                        })}
                    </div>

                    <div style={{ height: 1, background: 'var(--border-subtle)', maxWidth: 1280, margin: '0 auto 34px' }} />

                    <section style={{ textAlign: 'center', marginBottom: 24 }}>
                        <h2 style={{ margin: '0 0 8px', fontSize: 22, fontWeight: 700 }}>{t('enterprise.subscription.boostTitle', '需要更多额度？')}</h2>
                        <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: 13 }}>
                            {t('enterprise.subscription.boostDesc', '为智能体补充额度，让它们持续运转。')}
                        </p>
                    </section>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(220px, 1fr))', gap: 16, maxWidth: 920, margin: '0 auto' }}>
                        {creditPacks.map((pack) => (
                            <div key={pack.id} style={{ border: '1px solid var(--border-subtle)', borderRadius: 8, padding: 20, display: 'flex', flexDirection: 'column', gap: 10, background: 'var(--bg-primary)' }}>
                                <strong style={{ fontSize: 14 }}>Boost</strong>
                                <div style={{ display: 'flex', alignItems: 'baseline', gap: 7 }}>
                                    <span style={{ color: 'var(--text-tertiary)' }}>⚡</span>
                                    <span style={{ fontSize: 24, fontWeight: 750 }}>{pack.credits.toLocaleString()}</span>
                                    <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>额度</span>
                                </div>
                                <div style={{ color: 'var(--text-secondary)', fontWeight: 700 }}>
                                    {formatMoney(pack.currency, pack.price_cents)}
                                    <span style={{ color: 'var(--text-tertiary)', fontSize: 12, marginLeft: 5 }}>{formatUnitPrice(pack.currency, pack.price_cents, pack.credits)}</span>
                                </div>
                                <button className="btn btn-primary" disabled={checkoutTopup.isPending} style={{ marginTop: 8, width: '100%', justifyContent: 'center' }} onClick={() => checkoutTopup.mutate(pack.id)}>
                                    <IconShoppingCart size={15} />
                                    {t('enterprise.subscription.buyNow', '立即购买')}
                                </button>
                            </div>
                        ))}
                    </div>

                    {lastOrder && (
                        <div style={{ margin: '18px auto 0', maxWidth: 920, border: '1px solid var(--border-subtle)', borderRadius: 8, padding: 12, background: 'var(--bg-secondary)', fontSize: 13 }}>
                            {t('enterprise.subscription.orderCreated', '订单已创建')}:
                            <span style={{ marginLeft: 8, fontFamily: 'var(--font-mono)' }}>{lastOrder.id}</span>
                            <span style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>{lastOrder.status}</span>
                            {lastOrder.provider === 'manual' && (
                                <span style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>
                                    {t('enterprise.subscription.manualOrder', '由平台管理员处理')}
                                </span>
                            )}
                        </div>
                    )}
                </section>
            </div>
        );
    }

    return (
        <div className="subscription-tab" style={{ padding: showMarketplace ? '8px 0 24px' : 0 }}>
            <section className="card" style={{ marginBottom: showMarketplace ? 24 : 16 }}>
                <div style={{ display: 'grid', gridTemplateColumns: 'minmax(220px, 1fr) minmax(320px, 1.4fr)', gap: 16 }}>
                    <div>
                        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>
                            {t('enterprise.subscription.currentPlan', '当前套餐')}
                        </div>
                        <h3 style={{ margin: '0 0 6px', textTransform: 'capitalize' }}>{planCode}</h3>
                        <p style={{ color: 'var(--text-secondary)', margin: 0, fontSize: 13 }}>
                            {STATUS_LABEL[status] || status}
                            {ent?.period_end &&
                                ` · ${t('enterprise.subscription.expires', '到期')}: ${new Date(ent.period_end).toLocaleDateString()}`}
                        </p>
                        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-tertiary)' }}>
                            Seats: {seats ? `${seats.seats_used}/${seats.seats_total}` : '--'} · Credits: {usage?.credits_balance ?? 0}
                        </div>
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 }}>
                        <Metric label={t('enterprise.subscription.maxAgents', '最大 Agent 数')} value={ent?.max_agents ?? '-'} />
                        <Metric label={t('enterprise.subscription.maxLlmCalls', '每日 LLM 调用')} value={ent?.max_llm_calls_per_day ?? '-'} />
                        <Metric label={t('enterprise.subscription.messageLimit', '消息配额')} value={ent?.message_limit ?? '-'} />
                        <Metric label={t('enterprise.subscription.tokens', 'Token')} value={usage?.tokens_used ?? 0} />
                    </div>
                </div>
            </section>

            {!showMarketplace && (
                <section className="card" style={{ marginBottom: 16 }}>
                    <h4 style={{ margin: '0 0 10px' }}>{t('enterprise.subscription.entitlements', '权益')}</h4>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 8, fontSize: 13 }}>
                        <div>{t('enterprise.subscription.maxTriggers', '最大触发器')}: {ent?.max_triggers ?? '-'}</div>
                        <div>{t('enterprise.subscription.modalities', '可用模型类型')}: {ent?.allowed_modalities?.join(', ') || '-'}</div>
                        <div>{t('enterprise.subscription.tiers', '可用模型等级')}: {ent?.allowed_tiers?.join(', ') || '-'}</div>
                        <div>{t('enterprise.subscription.credits', '周期积分')}: {ent?.credits_per_period ?? 0}</div>
                    </div>
                </section>
            )}

            <section className="card" style={{ marginBottom: showMarketplace ? 28 : 16 }}>
                <h4 style={{ margin: '0 0 10px' }}>
                    {t('enterprise.subscription.usageToday', '今日用量')}
                    {usage?.period_date && ` (${usage.period_date})`}
                </h4>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 8, fontSize: 13 }}>
                    <div>LLM: {usage?.llm_calls_used ?? 0} / {usage?.llm_calls_limit ?? '-'}</div>
                    <div>{t('enterprise.subscription.messages', '消息')}: {usage?.messages_used ?? 0} / {usage?.messages_limit ?? '-'}</div>
                    <div>{t('enterprise.subscription.creditsBalance', '积分余额')}: {usage?.credits_balance ?? 0}</div>
                    <div>Seats: {seats ? `${seats.seats_used}/${seats.seats_total}` : '--'}</div>
                </div>
            </section>

            {showMarketplace && (
                <>
                    <section style={{ textAlign: 'center', marginBottom: 24 }}>
                        <h2 style={{ margin: '0 0 6px', fontSize: 22 }}>{t('enterprise.subscription.marketTitle', '和你一起成长的套餐')}</h2>
                        <p style={{ margin: '0 0 18px', color: 'var(--text-tertiary)', fontSize: 13 }}>
                            {t('enterprise.subscription.marketDesc', '选择适合团队的方案，随时可切换。')}
                        </p>
                        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, borderBottom: '1px solid var(--border-subtle)' }}>
                            {(['monthly', 'yearly'] as const).map((period) => (
                                <button
                                    key={period}
                                    type="button"
                                    onClick={() => setBillingPeriod(period)}
                                    style={{
                                        border: 'none',
                                        background: 'transparent',
                                        padding: '8px 10px',
                                        cursor: 'pointer',
                                        color: billingPeriod === period ? 'var(--text-primary)' : 'var(--text-tertiary)',
                                        borderBottom: billingPeriod === period ? '2px solid var(--text-primary)' : '2px solid transparent',
                                        fontWeight: billingPeriod === period ? 600 : 400,
                                    }}
                                >
                                    {period === 'monthly' ? t('enterprise.subscription.monthly', '月付') : t('enterprise.subscription.yearly', '年付')}
                                    {period === 'yearly' && <span style={{ marginLeft: 6, color: 'var(--success)', fontSize: 11 }}>省 20%</span>}
                                </button>
                            ))}
                        </div>
                    </section>

                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 16, marginBottom: 32 }}>
                        {sortedPlans.map((plan) => {
                            const isCurrent = plan.code === planCode || plan.id === ent?.plan_id;
                            const displayPrice = billingPeriod === 'yearly' ? plan.price_cents * 10 : plan.price_cents;
                            return (
                                <div key={plan.id} className="card" style={{ minHeight: 250, display: 'flex', flexDirection: 'column', gap: 12, borderColor: isCurrent ? 'var(--text-primary)' : undefined }}>
                                    <div>
                                        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>{plan.name}</div>
                                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                                            <span style={{ fontSize: 30, fontWeight: 700 }}>{formatMoney(plan.currency, displayPrice)}</span>
                                            <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>/ {billingPeriod === 'yearly' ? t('common.year', '年') : t('common.month', '月')}</span>
                                        </div>
                                        <div style={{ color: 'var(--success)', fontSize: 12 }}>{plan.period || 'monthly'} · {t('enterprise.subscription.billed', '按时计费')}</div>
                                    </div>
                                    <div style={{ height: 1, background: 'var(--border-subtle)' }} />
                                    <div style={{ fontSize: 13, lineHeight: 1.8, color: 'var(--text-secondary)' }}>
                                        <div><Check /> {plan.credits_per_period.toLocaleString()} Credits</div>
                                        <div><Check /> {plan.max_agents} {t('enterprise.subscription.publicAgents', '个公开 Agent 坐席')}</div>
                                        <div><Check /> {plan.allowed_tiers?.join(', ') || 'all'} Tier</div>
                                        <div><Check /> {plan.allowed_modalities?.join(', ') || 'all'} Modality</div>
                                    </div>
                                    <button
                                        className={isCurrent ? 'btn btn-secondary' : 'btn btn-primary'}
                                        disabled={isCurrent || checkoutSubscribe.isPending}
                                        style={{ marginTop: 'auto' }}
                                        onClick={() => checkoutSubscribe.mutate(plan.id)}
                                    >
                                        {isCurrent ? t('enterprise.subscription.current', '当前使用的套餐') : t('enterprise.subscription.upgrade', '升级')}
                                    </button>
                                </div>
                            );
                        })}
                    </div>

                    <section style={{ textAlign: 'center', marginBottom: 18 }}>
                        <h2 style={{ margin: '0 0 6px', fontSize: 20 }}>{t('enterprise.subscription.boostTitle', '需要更多额度？')}</h2>
                        <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: 13 }}>
                            {t('enterprise.subscription.boostDesc', '为智能体补充额度，让它们持续运转。')}
                        </p>
                    </section>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 16 }}>
                        {creditPacks.map((pack) => (
                            <div key={pack.id} className="card" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                                <strong>{pack.name}</strong>
                                <div style={{ fontSize: 22, fontWeight: 700 }}>{pack.credits.toLocaleString()} <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>额度</span></div>
                                <div style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>{formatMoney(pack.currency, pack.price_cents)}</div>
                                <button className="btn btn-primary" disabled={checkoutTopup.isPending} onClick={() => checkoutTopup.mutate(pack.id)}>
                                    {t('enterprise.subscription.buyNow', '立即购买')}
                                </button>
                            </div>
                        ))}
                    </div>

                    {lastOrder && (
                        <div className="card" style={{ marginTop: 16, background: 'var(--bg-secondary)', fontSize: 13 }}>
                            {t('enterprise.subscription.orderCreated', '订单已创建')}:
                            <span style={{ marginLeft: 8, fontFamily: 'var(--font-mono)' }}>{lastOrder.id}</span>
                            <span style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>{lastOrder.status}</span>
                            {lastOrder.provider === 'manual' && (
                                <span style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>
                                    {t('enterprise.subscription.manualOrder', '由平台管理员处理')}
                                </span>
                            )}
                        </div>
                    )}
                </>
            )}
        </div>
    );
}

function PlanBullet({ children }: { children: ReactNode }) {
    return (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <IconCheck size={14} color="var(--success)" />
            <span>{children}</span>
        </div>
    );
}

function Metric({ label, value }: { label: string; value: string | number }) {
    return (
        <div style={{ border: '1px solid var(--border-subtle)', borderRadius: 8, padding: 12, background: 'var(--bg-secondary)' }}>
            <div style={{ color: 'var(--text-tertiary)', fontSize: 11, marginBottom: 6 }}>{label}</div>
            <div style={{ fontSize: 18, fontWeight: 650 }}>{value}</div>
        </div>
    );
}

function Check() {
    return <span style={{ color: 'var(--success)', marginRight: 6 }}>✓</span>;
}
