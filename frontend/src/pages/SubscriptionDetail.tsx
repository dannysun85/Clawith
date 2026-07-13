import { useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import {
    IconBolt,
    IconCalendar,
    IconChartBar,
    IconChevronLeft,
    IconChevronRight,
    IconCrown,
    IconReceipt,
    IconReceipt2,
    IconSettings,
    IconUsers,
} from '@tabler/icons-react';
import { fetchJson } from '../services/api';

const PAGE_SIZE = 20;

type CreditTransaction = {
    id: string;
    delta: number;
    balance_after: number;
    reason: string;
    ref_type?: string | null;
    ref_id?: string | null;
    user_id?: string | null;
    agent_id?: string | null;
    action?: string | null;
    modality?: string | null;
    tier?: string | null;
    provider?: string | null;
    model?: string | null;
    consumer_label?: string | null;
    actor_label?: string | null;
    created_at: string;
};

type SeatUsage = {
    seats_total: number;
    seats_used: number;
    pending_invites: number;
};

type SubscriptionSummary = {
    plan_id?: string | null;
    plan_code?: string | null;
    subscription_status?: string | null;
    period_start?: string | null;
    period_end?: string | null;
    period_grant: number;
    topup_grants: number;
    consumed_credits: number;
    refunded_credits: number;
    total_granted: number;
    balance: number;
    reserved: number;
    available_balance: number;
    seats_used: number;
    seats_total: number;
    llm_calls_limit: number;
    message_limit: number;
    max_triggers: number;
};

type PaymentOrder = {
    id: string;
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

const formatMoney = (currency: string, cents: number) => {
    const prefix = currency === 'CNY' ? '¥' : '$';
    return `${prefix}${(cents / 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
};

const titleCasePlan = (code?: string | null) => {
    if (!code) return 'Free';
    const normalized = code.toLowerCase();
    const labels: Record<string, string> = {
        free: 'Free',
        starter: 'Starter',
        pro: 'Pro',
        scale: 'Scale',
        enterprise: 'Scale',
    };
    return labels[normalized] || code.charAt(0).toUpperCase() + code.slice(1);
};

const compactId = (id?: string | null) => {
    if (!id) return '-';
    return id.length > 12 ? `${id.slice(0, 8)}...` : id;
};

const formatDateParts = (value?: string | null) => {
    if (!value) return { date: '-', time: '' };
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return { date: '-', time: '' };
    return {
        date: date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'numeric', day: 'numeric' }),
        time: date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }),
    };
};

const orderDescription = (order: PaymentOrder) => {
    if (order.type === 'subscribe') return '订阅套餐';
    if (order.type === 'topup') return `Boost ${order.credits?.toLocaleString() || ''} 额度`.trim();
    return order.type || '-';
};

const orderStatusLabel = (status: string) => {
    const labels: Record<string, string> = {
        pending: '待支付',
        paid: '已支付',
        failed: '失败',
        canceled: '已取消',
        refunded: '已退款',
    };
    return labels[status] || status || '-';
};

export const transactionActionLabel = (tx: CreditTransaction) => {
    if (tx.reason === 'refund' && tx.ref_type === 'product_incident') return '事故退款';
    const raw = tx.action || tx.reason || '-';
    const labels: Record<string, string> = {
        chat: 'chat',
        heartbeat: 'heartbeat',
        subscribe: 'subscribe',
        topup: 'topup',
        tool: 'tool',
        refund: '退款',
    };
    return labels[raw] || raw;
};

export default function SubscriptionDetail() {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const [activeTab, setActiveTab] = useState<'ledger' | 'orders'>('ledger');
    const [ledgerPage, setLedgerPage] = useState(1);
    const [orderPage, setOrderPage] = useState(1);

    const { data: summary } = useQuery({
        queryKey: ['subscription-summary'],
        queryFn: () => fetchJson<SubscriptionSummary>('/subscription/summary'),
        refetchInterval: 30000,
    });
    const { data: transactions = [] } = useQuery({
        queryKey: ['subscription-credit-transactions', ledgerPage],
        queryFn: () => fetchJson<CreditTransaction[]>(`/subscription/credit-transactions?page=${ledgerPage}&limit=${PAGE_SIZE}`),
    });
    const { data: orders = [] } = useQuery({
        queryKey: ['subscription-orders', orderPage],
        queryFn: () => fetchJson<PaymentOrder[]>(`/subscription/orders?page=${orderPage}&limit=${PAGE_SIZE}`),
    });

    const planName = titleCasePlan(summary?.plan_code);
    const creditsTotal = summary?.total_granted ?? 0;
    const creditsUsed = summary?.consumed_credits ?? 0;
    const creditsProgress = creditsTotal > 0 ? Math.max(0, Math.min(100, (creditsUsed / creditsTotal) * 100)) : 0;
    const seatTotal = summary?.seats_total ?? 0;
    const seatUsed = summary?.seats_used ?? 0;
    const seatProgress = seatTotal > 0 ? Math.max(0, Math.min(100, (seatUsed / seatTotal) * 100)) : 0;
    const expiry = useMemo(() => formatDateParts(summary?.period_end), [summary?.period_end]);

    return (
        <div className="subscription-detail-page" style={{ maxWidth: 1040, margin: '0 auto', padding: '32px 18px 48px' }}>
            <div className="subscription-detail-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, marginBottom: 20 }}>
                <h1 className="page-title" style={{ margin: 0 }}>{t('subscription.detail.title', '套餐详情')}</h1>
                <div className="subscription-detail-actions" style={{ display: 'flex', gap: 10 }}>
                    <button className="btn btn-secondary" type="button" onClick={() => setActiveTab('orders')}>
                        <IconReceipt size={16} />
                        {t('subscription.detail.billingManagement', '账单管理')}
                    </button>
                    <button className="btn btn-secondary" type="button" onClick={() => navigate('/enterprise#subscription')}>
                        <IconSettings size={16} />
                        {t('subscription.detail.manageSubscription', '管理订阅')}
                    </button>
                </div>
            </div>

            <section className="card subscription-usage-card" style={{ padding: 32, marginBottom: 26 }}>
                <h2 style={{ margin: '0 0 28px', fontSize: 20, fontWeight: 700 }}>{t('subscription.detail.usageTitle', '套餐使用情况')}</h2>
                <div className="subscription-usage-grid" style={{ display: 'grid', gridTemplateColumns: 'minmax(280px, 0.9fr) minmax(360px, 1.35fr)', gap: 24 }}>
                    <div className="subscription-plan-card" style={{ border: '1px solid var(--border-subtle)', borderRadius: 12, minHeight: 178, padding: 24, background: 'var(--bg-secondary)' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                            <div style={iconBoxStyle}>
                                <IconCrown size={20} />
                            </div>
                            <div>
                                <div style={{ fontWeight: 700, fontSize: 18 }}>{planName}</div>
                                <div style={{ marginTop: 3, color: 'var(--text-secondary)', fontSize: 13 }}>
                                    {t('subscription.detail.monthlyPlan', '月度套餐')}
                                </div>
                            </div>
                        </div>
                        <div style={{ marginTop: 28, display: 'inline-flex', alignItems: 'center', gap: 8, border: '1px solid var(--border-subtle)', borderRadius: 8, padding: '10px 14px', background: 'var(--bg-primary)', color: 'var(--text-secondary)', fontSize: 13 }}>
                            <IconCalendar size={16} />
                            {summary?.period_end ? `${t('subscription.detail.validUntil', '有效期至')} ${expiry.date}` : t('subscription.detail.noExpiry', '长期有效')}
                        </div>
                    </div>

                    <div style={{ display: 'grid', gap: 14 }}>
                        <UsageMetric
                            icon={<IconBolt size={20} />}
                            accent="var(--text-primary)"
                            title={t('subscription.detail.creditsUsage', 'Credits 用量')}
                            value={`${creditsUsed.toLocaleString()} / ${creditsTotal.toLocaleString()}`}
                            suffix={t('subscription.detail.points', '积分')}
                            progress={creditsProgress}
                        />
                        <UsageMetric
                            icon={<IconBolt size={20} />}
                            accent="var(--text-secondary)"
                            title={t('subscription.detail.availableCredits', '可用 Credits')}
                            value={`${(summary?.available_balance ?? 0).toLocaleString()} / ${(summary?.balance ?? 0).toLocaleString()}`}
                            suffix={summary?.reserved ? `${t('subscription.detail.reservedCredits', '预占')} ${summary.reserved.toLocaleString()}` : t('subscription.detail.points', '积分')}
                            progress={summary?.balance ? Math.max(0, Math.min(100, ((summary.available_balance || 0) / summary.balance) * 100)) : 0}
                        />
                        <UsageMetric
                            icon={<IconUsers size={20} />}
                            accent="var(--warning, #f59e0b)"
                            title={t('subscription.detail.seatUsage', '坐席用量')}
                            value={`${seatUsed.toLocaleString()} / ${seatTotal.toLocaleString()}`}
                            suffix="Seats"
                            progress={seatProgress}
                        />
                    </div>
                </div>
            </section>

            <section>
                <div style={{ display: 'flex', gap: 24, borderBottom: '1px solid var(--border-subtle)', marginBottom: 24 }}>
                    <TabButton active={activeTab === 'ledger'} onClick={() => setActiveTab('ledger')} icon={<IconChartBar size={16} />}>
                        {t('subscription.detail.ledger', '消耗明细')}
                    </TabButton>
                    <TabButton active={activeTab === 'orders'} onClick={() => setActiveTab('orders')} icon={<IconReceipt2 size={16} />}>
                        {t('subscription.detail.orderHistory', '订单历史')}
                    </TabButton>
                </div>

                {activeTab === 'ledger' ? (
                    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                        <div style={{ overflowX: 'auto' }}>
                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                                <thead>
                                    <tr style={{ color: 'var(--text-tertiary)', textAlign: 'left', background: 'var(--bg-secondary)' }}>
                                        <TableHead>{t('common.time', '时间')}</TableHead>
                                        <TableHead>{t('subscription.detail.consumer', '消耗方')}</TableHead>
                                        <TableHead>{t('subscription.detail.actor', '发起方')}</TableHead>
                                        <TableHead>{t('subscription.detail.action', '动作')}</TableHead>
                                        <TableHead align="right">{t('subscription.detail.points', '积分')}</TableHead>
                                    </tr>
                                </thead>
                                <tbody>
                                    {transactions.map((tx) => {
                                        const parts = formatDateParts(tx.created_at);
                                        return (
                                            <tr key={tx.id}>
                                                <TableCell>
                                                    <div style={{ fontWeight: 600 }}>{parts.date}</div>
                                                    <div style={{ color: 'var(--text-tertiary)', fontSize: 11, marginTop: 4 }}>{parts.time}</div>
                                                </TableCell>
                                                <TableCell mono>{tx.consumer_label || tx.model || compactId(tx.agent_id)}</TableCell>
                                                <TableCell mono>{tx.actor_label || compactId(tx.user_id)}</TableCell>
                                                <TableCell>
                                                    <span style={{ display: 'inline-flex', alignItems: 'center', borderRadius: 6, padding: '4px 9px', background: 'rgba(59, 130, 246, 0.11)', color: '#3b82f6', fontWeight: 650, fontSize: 12 }}>
                                                        {transactionActionLabel(tx)}
                                                    </span>
                                                </TableCell>
                                                <TableCell align="right">
                                                    <span style={{ color: tx.delta < 0 ? 'var(--error)' : 'var(--success)', fontWeight: 700 }}>
                                                        {tx.delta > 0 ? '+' : ''}{tx.delta}
                                                    </span>
                                                </TableCell>
                                            </tr>
                                        );
                                    })}
                                    {transactions.length === 0 && (
                                        <EmptyRow colSpan={5} label={t('common.noData', '暂无数据')} />
                                    )}
                                </tbody>
                            </table>
                        </div>
                        <Pagination
                            page={ledgerPage}
                            hasNext={transactions.length === PAGE_SIZE}
                            onPrev={() => setLedgerPage((p) => Math.max(1, p - 1))}
                            onNext={() => setLedgerPage((p) => p + 1)}
                        />
                    </div>
                ) : (
                    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                        <div style={{ overflowX: 'auto' }}>
                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                                <thead>
                                    <tr style={{ color: 'var(--text-tertiary)', textAlign: 'left', background: 'var(--bg-secondary)' }}>
                                        <TableHead>{t('subscription.detail.date', '日期')}</TableHead>
                                        <TableHead>{t('subscription.detail.description', '描述')}</TableHead>
                                        <TableHead align="right">{t('subscription.detail.amount', '金额')}</TableHead>
                                        <TableHead>{t('subscription.detail.status', '状态')}</TableHead>
                                    </tr>
                                </thead>
                                <tbody>
                                    {orders.map((order) => {
                                        const parts = formatDateParts(order.created_at);
                                        return (
                                            <tr key={order.id}>
                                                <TableCell>
                                                    <div style={{ fontWeight: 600 }}>{parts.date}</div>
                                                    <div style={{ color: 'var(--text-tertiary)', fontSize: 11, marginTop: 4 }}>{parts.time}</div>
                                                </TableCell>
                                                <TableCell>{orderDescription(order)}</TableCell>
                                                <TableCell align="right">{formatMoney(order.currency, order.amount_cents)}</TableCell>
                                                <TableCell>
                                                    <span style={{ display: 'inline-flex', alignItems: 'center', borderRadius: 999, padding: '4px 10px', background: 'var(--bg-secondary)', color: 'var(--text-secondary)', fontWeight: 650, fontSize: 12 }}>
                                                        {orderStatusLabel(order.status)}
                                                    </span>
                                                </TableCell>
                                            </tr>
                                        );
                                    })}
                                    {orders.length === 0 && (
                                        <EmptyRow colSpan={4} label={t('subscription.detail.noOrders', '暂无订单记录')} />
                                    )}
                                </tbody>
                            </table>
                        </div>
                        <Pagination
                            page={orderPage}
                            hasNext={orders.length === PAGE_SIZE}
                            onPrev={() => setOrderPage((p) => Math.max(1, p - 1))}
                            onNext={() => setOrderPage((p) => p + 1)}
                        />
                    </div>
                )}
            </section>
        </div>
    );
}

const iconBoxStyle: CSSProperties = {
    width: 40,
    height: 40,
    borderRadius: 8,
    border: '1px solid var(--border-subtle)',
    background: 'var(--bg-primary)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
};

function UsageMetric({
    icon,
    accent,
    title,
    value,
    suffix,
    progress,
}: {
    icon: ReactNode;
    accent: string;
    title: string;
    value: string;
    suffix: string;
    progress: number;
}) {
    return (
        <div className="subscription-usage-metric" style={{ border: '1px solid var(--border-subtle)', borderRadius: 12, padding: 22, background: 'var(--bg-secondary)' }}>
            <div className="subscription-usage-metric-main" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <div style={{ ...iconBoxStyle, color: accent }}>{icon}</div>
                    <div style={{ fontWeight: 700, fontSize: 15 }}>{title}</div>
                </div>
                <div className="subscription-usage-metric-value" style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                    <span style={{ fontSize: 22, fontWeight: 760 }}>{value}</span>
                    <span style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{suffix}</span>
                </div>
            </div>
            <div style={{ height: 8, borderRadius: 999, background: 'var(--bg-primary)', marginTop: 18, overflow: 'hidden' }}>
                <div style={{ width: `${progress}%`, minWidth: progress > 0 ? 18 : 0, height: '100%', borderRadius: 999, background: 'var(--text-primary)' }} />
            </div>
        </div>
    );
}

function TabButton({ active, onClick, icon, children }: { active: boolean; onClick: () => void; icon: ReactNode; children: ReactNode }) {
    return (
        <button
            type="button"
            onClick={onClick}
            style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 8,
                border: 'none',
                background: 'transparent',
                padding: '12px 4px',
                borderBottom: active ? '2px solid var(--text-primary)' : '2px solid transparent',
                color: active ? 'var(--text-primary)' : 'var(--text-tertiary)',
                fontWeight: active ? 700 : 600,
                cursor: 'pointer',
            }}
        >
            {icon}
            {children}
        </button>
    );
}

function TableHead({ children, align = 'left' }: { children: ReactNode; align?: 'left' | 'right' }) {
    return (
        <th style={{ padding: '16px 24px', borderBottom: '1px solid var(--border-subtle)', textAlign: align, fontWeight: 650 }}>
            {children}
        </th>
    );
}

function TableCell({
    children,
    align = 'left',
    mono = false,
}: {
    children: ReactNode;
    align?: 'left' | 'right';
    mono?: boolean;
}) {
    return (
        <td style={{ padding: '16px 24px', borderBottom: '1px solid var(--border-subtle)', textAlign: align, color: 'var(--text-secondary)', fontFamily: mono ? 'var(--font-mono)' : undefined }}>
            {children}
        </td>
    );
}

function EmptyRow({ colSpan, label }: { colSpan: number; label: string }) {
    return (
        <tr>
            <td colSpan={colSpan} style={{ height: 164, textAlign: 'center', color: 'var(--text-tertiary)', borderBottom: '1px solid var(--border-subtle)' }}>
                <IconReceipt2 size={28} style={{ display: 'block', margin: '0 auto 12px', opacity: 0.55 }} />
                {label}
            </td>
        </tr>
    );
}

function Pagination({ page, hasNext, onPrev, onNext }: { page: number; hasNext: boolean; onPrev: () => void; onNext: () => void }) {
    return (
        <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 14, padding: '16px 24px', background: 'var(--bg-secondary)' }}>
            <button className="btn btn-secondary" type="button" disabled={page <= 1} onClick={onPrev} aria-label="上一页" style={{ width: 36, height: 36, padding: 0, justifyContent: 'center' }}>
                <IconChevronLeft size={16} />
            </button>
            <span style={{ color: 'var(--text-secondary)', fontSize: 13 }}>第 {page} 页</span>
            <button className="btn btn-secondary" type="button" disabled={!hasNext} onClick={onNext} aria-label="下一页" style={{ width: 36, height: 36, padding: 0, justifyContent: 'center' }}>
                <IconChevronRight size={16} />
            </button>
        </div>
    );
}
