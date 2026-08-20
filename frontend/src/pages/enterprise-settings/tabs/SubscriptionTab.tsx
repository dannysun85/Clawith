import { useMemo, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { IconCheck, IconShoppingCart } from '@tabler/icons-react';
import { fetchJson } from '../utils/fetchJson';
import { WeChatPayModal } from '../../../components/WeChatPayModal';
import { useToast } from '../../../components/Toast/ToastProvider';
import { useBillingConfig } from '../../../hooks/useBillingConfig';
import { normalizeUnknownError } from '../../../services/apiError';
import { Entitlements } from '../../../hooks/useLlmModels';
import { DEFAULT_USD_CNY_RATE, formatMoneyCny, toCnyCents } from '../../../utils/money';
import { useAuthStore } from '../../../stores';
import { hasEffectiveCapability, productAccessSignature } from '../../../utils/productAccess';
import {
    buildPaymentDomainRedirectUrl,
    needsPaymentDomainRedirect,
} from '../../../utils/paymentCheckout';

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
    period?: string | null;
    change_kind?: string | null;
}

const STATUS_LABEL: Record<string, string> = {
    active: '生效中',
    trialing: '试用中',
    canceled: '已取消(周期末失效)',
    expired: '已过期',
    past_due: '续费失败(宽限中)',
    none: '无订阅(使用默认配额)',
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

const formatUnitPrice = (currency: string, priceCents: number, credits: number, usdCnyRate: number) => {
    if (!credits) return '';
    const unit = ((toCnyCents(currency, priceCents, usdCnyRate) / 100 / credits) * 100).toFixed(2);
    return `(¥${unit}/100)`;
};

export default function SubscriptionTab({ showMarketplace = true }: { showMarketplace?: boolean }) {
    const { t } = useTranslation();
    const qc = useQueryClient();
    const navigate = useNavigate();
    const user = useAuthStore((state) => state.user);
    const token = useAuthStore((state) => state.token);
    const [billingPeriod, setBillingPeriod] = useState<'monthly' | 'yearly'>('monthly');
    const [lastOrder, setLastOrder] = useState<PaymentOrder | null>(null);
    const [wechatPay, setWechatPay] = useState<{ orderId: string; codeUrl: string; amountCents: number; currency: string } | null>(null);
    const toast = useToast();
    const { data: billingConfig, isLoading: billingConfigLoading } = useBillingConfig();
    const cnyRate = billingConfig?.usd_cny_rate ?? DEFAULT_USD_CNY_RATE;

    /** Real-money checkout only runs on the public payment domain; carry the session across hosts. */
    const redirectToPaymentDomain = () => {
        const host = billingConfig?.payment_host;
        if (!host || !needsPaymentDomainRedirect(host, window.location.hostname)) return false;
        window.location.assign(buildPaymentDomainRedirectUrl({
            paymentHost: host,
            currentHref: window.location.href,
            sessionToken: token,
        }));
        return true;
    };
    const tenantId = user?.tenant_id || null;
    const membershipId = user?.membership_id || user?.id || null;
    const accessSignature = productAccessSignature(user);
    const canViewCompanyBilling = hasEffectiveCapability(user, 'company.billing.view');
    const canManageCompanyBilling = hasEffectiveCapability(user, 'company.billing.manage');
    const requireCheckoutReady = () => {
        if (!canManageCompanyBilling) {
            throw new Error(t('enterprise.subscription.ownerOnly', '仅公司所有者可购买'));
        }
        if (billingConfigLoading) {
            throw new Error(t('enterprise.subscription.configLoading', '支付配置加载中，请稍后再试'));
        }
        if (!billingConfig) {
            throw new Error(t('enterprise.subscription.configUnavailable', '无法确认支付配置，已阻止下单'));
        }
        if (!billingConfig.checkout_enabled) {
            throw new Error(
                billingConfig.next_action
                || t('enterprise.subscription.providerUnavailable', '支付通道尚未就绪，请联系平台管理员'),
            );
        }
        return redirectToPaymentDomain();
    };
    const reportCheckoutError = (error: unknown) => {
        toast.error(normalizeUnknownError(error).message);
    };

    const { data: ent } = useQuery({
        queryKey: ['subscription-entitlements', tenantId, membershipId, accessSignature],
        queryFn: () => fetchJson<Entitlements | null>('/subscription/my-entitlements'),
        enabled: Boolean(tenantId),
    });
    const { data: usage } = useQuery({
        queryKey: ['subscription-usage', tenantId, membershipId, accessSignature],
        queryFn: () => fetchJson<Usage | null>('/subscription/usage'),
        enabled: Boolean(tenantId && canViewCompanyBilling),
        refetchInterval: 30000,
    });
    const { data: seats } = useQuery({
        queryKey: ['subscription-seats', tenantId, membershipId, accessSignature],
        queryFn: () => fetchJson<SeatUsage>('/subscription/seats'),
        enabled: Boolean(tenantId && canViewCompanyBilling),
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
    const { data: summary } = useQuery({
        queryKey: ['subscription-summary', tenantId, membershipId, accessSignature],
        queryFn: () => fetchJson<{ period_start?: string | null; period_end?: string | null; scheduled_plan_code?: string | null } | null>('/subscription/summary'),
        enabled: Boolean(tenantId && canViewCompanyBilling),
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
        if (order.provider === 'wechat') {
            if (order.session_url) {
                setWechatPay({
                    orderId: order.id,
                    codeUrl: order.session_url,
                    amountCents: order.amount_cents,
                    currency: order.currency,
                });
            } else {
                toast.error(t(
                    'enterprise.subscription.wechatQrMissing',
                    '微信支付未返回付款二维码，请稍后重试或联系平台管理员',
                ));
            }
            return;
        }
        if (order.session_url) {
            window.location.assign(order.session_url);
            return;
        }
        if (order.provider !== 'manual') {
            navigate(`/billing/success?order_id=${order.id}`);
        }
    };

    const wechatPayModal = wechatPay ? (
        <WeChatPayModal
            orderId={wechatPay.orderId}
            codeUrl={wechatPay.codeUrl}
            amountCents={wechatPay.amountCents}
            currency={wechatPay.currency}
            onPaid={invalidateBillingQueries}
            onClose={() => setWechatPay(null)}
        />
    ) : null;

    const checkoutSubscribe = useMutation({
        mutationFn: (planId: string) => {
            if (requireCheckoutReady()) return new Promise<PaymentOrder>(() => {});
            return fetchJson<PaymentOrder>('/subscription/checkout/subscribe', {
                method: 'POST',
                body: JSON.stringify({ plan_id: planId, period: billingPeriod, seats: 1 }),
            });
        },
        onSuccess: handleCheckoutOrder,
        onError: reportCheckoutError,
    });

    const checkoutTopup = useMutation({
        mutationFn: (creditPackId: string) => {
            if (requireCheckoutReady()) return new Promise<PaymentOrder>(() => {});
            return fetchJson<PaymentOrder>('/subscription/checkout/topup', {
                method: 'POST',
                body: JSON.stringify({ credit_pack_id: creditPackId }),
            });
        },
        onSuccess: handleCheckoutOrder,
        onError: reportCheckoutError,
    });

    const status = ent?.subscription_status || 'none';
    const planCode = ent?.plan_code || 'free';
    const sortedPlans = useMemo(() => [...plans].sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0)), [plans]);

    // Plan-change semantics (server classifies the same way at checkout):
    // same plan+period → renew; same plan other period → period switch;
    // higher tier → upgrade now; lower tier → downgrade scheduled to period end.
    const currentPlan = sortedPlans.find((p) => p.code === planCode || p.id === ent?.plan_id) || null;
    const currentPeriod: 'monthly' | 'yearly' = (() => {
        if (summary?.period_start && summary?.period_end) {
            const days = (new Date(summary.period_end).getTime() - new Date(summary.period_start).getTime()) / 86400000;
            return days > 60 ? 'yearly' : 'monthly';
        }
        return 'monthly';
    })();
    const scheduledPlanCode = summary?.scheduled_plan_code || null;

    const planAction = (plan: Plan): { label: string; disabled: boolean; primary: boolean } => {
        if (!canManageCompanyBilling) {
            return { label: t('enterprise.subscription.ownerOnly', '仅公司所有者可购买'), disabled: true, primary: false };
        }
        const isCurrentPlan = plan.code === planCode || plan.id === ent?.plan_id;
        if (isCurrentPlan && plan.price_cents === 0) {
            return { label: t('enterprise.subscription.current', '当前使用的套餐'), disabled: true, primary: false };
        }
        if (billingConfigLoading || !billingConfig) {
            return { label: t('enterprise.subscription.configLoading', '支付配置检查中'), disabled: true, primary: false };
        }
        if (!billingConfig.checkout_enabled) {
            return { label: t('enterprise.subscription.providerUnavailable', '支付暂不可用'), disabled: true, primary: false };
        }
        const manualMode = billingConfig.provider === 'manual';
        if (isCurrentPlan) {
            if (billingPeriod === currentPeriod) {
                return {
                    label: manualMode
                        ? t('enterprise.subscription.manualRenew', '提交续费申请')
                        : t('enterprise.subscription.renew', '续费'),
                    disabled: false,
                    primary: false,
                };
            }
            return {
                label: manualMode
                    ? t('enterprise.subscription.manualPeriodSwitch', '提交周期变更申请')
                    : billingPeriod === 'yearly'
                        ? t('enterprise.subscription.switchYearly', '转年付')
                        : t('enterprise.subscription.switchMonthly', '转月付'),
                disabled: false,
                primary: true,
            };
        }
        if (currentPlan && plan.tier > currentPlan.tier) {
            return {
                label: manualMode
                    ? t('enterprise.subscription.manualUpgrade', '提交升级申请')
                    : t('enterprise.subscription.upgrade', '升级'),
                disabled: false,
                primary: true,
            };
        }
        if (currentPlan && plan.tier < currentPlan.tier) {
            return {
                label: manualMode
                    ? t('enterprise.subscription.manualDowngrade', '提交降级申请（下个周期）')
                    : t('enterprise.subscription.downgrade', '降级（下个周期生效）'),
                disabled: false,
                primary: false,
            };
        }
        return {
            label: manualMode
                ? t('enterprise.subscription.manualUpgrade', '提交升级申请')
                : t('enterprise.subscription.upgrade', '升级'),
            disabled: false,
            primary: true,
        };
    };

    const CHANGE_KIND_TEXT: Record<string, string> = {
        new: t('enterprise.subscription.orderNew', '订阅订单已创建'),
        renew: t('enterprise.subscription.orderRenew', '续费订单已创建'),
        period_switch: t('enterprise.subscription.orderPeriodSwitch', '周期变更订单已创建'),
        upgrade: t('enterprise.subscription.orderUpgrade', '升级订单已创建，支付成功后立即生效'),
        downgrade: t('enterprise.subscription.orderDowngrade', '降级订单已创建，支付成功后将于当前周期末生效'),
    };

    if (showMarketplace) {
        return (
            <div style={{ padding: '16px 0 30px' }}>
                {billingConfig?.native_payment_enabled && billingConfig.payment_host && needsPaymentDomainRedirect(billingConfig.payment_host, window.location.hostname) && (
                    <div className="card" style={{ marginBottom: 16, background: 'var(--bg-secondary)', fontSize: 13 }}>
                        {t(
                            'enterprise.subscription.paymentDomainHint',
                            '支付必须在 {{host}} 完成。请先跳转后再购买，当前登录会一并带过去。',
                            { host: billingConfig.payment_host },
                        )}
                        <button
                            type="button"
                            className="btn btn-primary"
                            style={{ marginLeft: 12 }}
                            onClick={() => redirectToPaymentDomain()}
                        >
                            {t('enterprise.subscription.goToPaymentDomain', '前往支付页')}
                        </button>
                    </div>
                )}
                {(billingConfig?.provider || 'manual') === 'manual' && (
                    <div className="card" style={{ marginBottom: 16, background: 'var(--bg-secondary)', fontSize: 13 }}>
                        {t(
                            'enterprise.subscription.manualProviderHint',
                            '当前是人工订单模式：提交后由平台管理员线下处理，不会生成在线付款或微信二维码。',
                        )}
                    </div>
                )}
                {billingConfig && !billingConfig.checkout_enabled && (
                    <div className="card" role="alert" style={{ marginBottom: 16, background: 'var(--bg-secondary)', fontSize: 13, color: 'var(--warning)' }}>
                        {billingConfig.next_action || t(
                            'enterprise.subscription.providerUnavailableHint',
                            '支付通道尚未就绪，系统已阻止创建订单。请联系平台管理员。',
                        )}
                    </div>
                )}
                <section style={{ border: '1px solid var(--border-subtle)', borderRadius: 8, background: 'var(--bg-primary)', padding: '32px 40px 30px' }}>
                    <div style={{ textAlign: 'center', marginBottom: 28 }}>
                        <h2 style={{ margin: '0 0 8px', fontSize: 24, fontWeight: 700 }}>{t('enterprise.subscription.marketTitle', '和你一起成长的套餐')}</h2>
                        <p style={{ margin: '0 0 22px', color: 'var(--text-tertiary)', fontSize: 13 }}>
                            {t('enterprise.subscription.marketDesc', '选择适合团队的方案，随时可切换。')}
                        </p>
                        {scheduledPlanCode && (
                            <p style={{ margin: '0 0 16px', color: 'var(--warning)', fontSize: 13 }}>
                                {t('enterprise.subscription.scheduledDowngrade', '已预约降级为 {{plan}}，将于当前周期结束后生效').replace('{{plan}}', scheduledPlanCode)}
                            </p>
                        )}
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
                            const action = planAction(plan);
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
                                            <span style={{ fontSize: 32, lineHeight: 1, fontWeight: 750 }}>{formatMoneyCny(plan.currency, price.current, cnyRate)}</span>
                                            <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>/ {billingPeriod === 'yearly' ? t('common.year', '年') : t('common.month', '月')}</span>
                                            {price.original > price.current && (
                                                <span style={{ color: 'var(--text-tertiary)', fontSize: 12, textDecoration: 'line-through' }}>{formatMoneyCny(plan.currency, price.original, cnyRate)}</span>
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
                                        className={action.primary ? 'btn btn-primary' : 'btn btn-secondary'}
                                        disabled={action.disabled || checkoutSubscribe.isPending}
                                        style={{ marginTop: 'auto', width: '100%', justifyContent: 'center' }}
                                        onClick={() => checkoutSubscribe.mutate(plan.id)}
                                    >
                                        {action.label}
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
                                    {formatMoneyCny(pack.currency, pack.price_cents, cnyRate)}
                                    <span style={{ color: 'var(--text-tertiary)', fontSize: 12, marginLeft: 5 }}>{formatUnitPrice(pack.currency, pack.price_cents, pack.credits, cnyRate)}</span>
                                </div>
                                <button className="btn btn-primary" disabled={checkoutTopup.isPending || !canManageCompanyBilling || !billingConfig?.checkout_enabled} style={{ marginTop: 8, width: '100%', justifyContent: 'center' }} onClick={() => checkoutTopup.mutate(pack.id)}>
                                    <IconShoppingCart size={15} />
                                    {canManageCompanyBilling
                                        ? billingConfig?.provider === 'manual'
                                            ? t('enterprise.subscription.manualOrderSubmit', '提交人工订单')
                                            : billingConfig?.checkout_enabled
                                                ? t('enterprise.subscription.buyNow', '立即购买')
                                                : t('enterprise.subscription.providerUnavailable', '支付暂不可用')
                                        : t('enterprise.subscription.ownerOnly', '仅公司所有者可购买')}
                                </button>
                            </div>
                        ))}
                    </div>

                    {lastOrder && (
                        <div style={{ margin: '18px auto 0', maxWidth: 920, border: '1px solid var(--border-subtle)', borderRadius: 8, padding: 12, background: 'var(--bg-secondary)', fontSize: 13 }}>
                            {(lastOrder.change_kind && CHANGE_KIND_TEXT[lastOrder.change_kind]) || t('enterprise.subscription.orderCreated', '订单已创建')}:
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
                {wechatPayModal}
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
                    {billingConfig?.native_payment_enabled && billingConfig.payment_host && needsPaymentDomainRedirect(billingConfig.payment_host, window.location.hostname) && (
                        <div className="card" style={{ marginBottom: 16, background: 'var(--bg-secondary)', fontSize: 13 }}>
                            {t(
                                'enterprise.subscription.paymentDomainHint',
                                '支付必须在 {{host}} 完成。请先跳转后再购买，当前登录会一并带过去。',
                                { host: billingConfig.payment_host },
                            )}
                            <button
                                type="button"
                                className="btn btn-primary"
                                style={{ marginLeft: 12 }}
                                onClick={() => redirectToPaymentDomain()}
                            >
                                {t('enterprise.subscription.goToPaymentDomain', '前往支付页')}
                            </button>
                        </div>
                    )}
                    {(billingConfig?.provider || 'manual') === 'manual' && (
                        <div className="card" style={{ marginBottom: 16, background: 'var(--bg-secondary)', fontSize: 13 }}>
                            {t(
                                'enterprise.subscription.manualProviderHint',
                                '当前是人工订单模式：提交后由平台管理员线下处理，不会生成在线付款或微信二维码。',
                            )}
                        </div>
                    )}
                    {billingConfig && !billingConfig.checkout_enabled && (
                        <div className="card" role="alert" style={{ marginBottom: 16, background: 'var(--bg-secondary)', fontSize: 13, color: 'var(--warning)' }}>
                            {billingConfig.next_action || t(
                                'enterprise.subscription.providerUnavailableHint',
                                '支付通道尚未就绪，系统已阻止创建订单。请联系平台管理员。',
                            )}
                        </div>
                    )}
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
                            const action = planAction(plan);
                            const displayPrice = billingPeriod === 'yearly' ? plan.price_cents * 10 : plan.price_cents;
                            return (
                                <div key={plan.id} className="card" style={{ minHeight: 250, display: 'flex', flexDirection: 'column', gap: 12, borderColor: action.disabled && !action.primary ? 'var(--text-primary)' : undefined }}>
                                    <div>
                                        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>{plan.name}</div>
                                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                                            <span style={{ fontSize: 30, fontWeight: 700 }}>{formatMoneyCny(plan.currency, displayPrice, cnyRate)}</span>
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
                                        className={action.primary ? 'btn btn-primary' : 'btn btn-secondary'}
                                        disabled={action.disabled || checkoutSubscribe.isPending}
                                        style={{ marginTop: 'auto' }}
                                        onClick={() => checkoutSubscribe.mutate(plan.id)}
                                    >
                                        {action.label}
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
                                <div style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>{formatMoneyCny(pack.currency, pack.price_cents, cnyRate)}</div>
                                <button className="btn btn-primary" disabled={checkoutTopup.isPending || !canManageCompanyBilling || !billingConfig?.checkout_enabled} onClick={() => checkoutTopup.mutate(pack.id)}>
                                    {canManageCompanyBilling
                                        ? billingConfig?.provider === 'manual'
                                            ? t('enterprise.subscription.manualOrderSubmit', '提交人工订单')
                                            : billingConfig?.checkout_enabled
                                                ? t('enterprise.subscription.buyNow', '立即购买')
                                                : t('enterprise.subscription.providerUnavailable', '支付暂不可用')
                                        : t('enterprise.subscription.ownerOnly', '仅公司所有者可购买')}
                                </button>
                            </div>
                        ))}
                    </div>

                    {lastOrder && (
                        <div className="card" style={{ marginTop: 16, background: 'var(--bg-secondary)', fontSize: 13 }}>
                            {(lastOrder.change_kind && CHANGE_KIND_TEXT[lastOrder.change_kind]) || t('enterprise.subscription.orderCreated', '订单已创建')}:
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
            {wechatPayModal}
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
