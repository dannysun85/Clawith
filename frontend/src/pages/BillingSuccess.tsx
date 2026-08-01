import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router';
import { IconCircleCheck, IconCircleX, IconLoader2 } from '@tabler/icons-react';
import { fetchJson } from '../services/api';

type PaymentOrder = {
    id: string;
    provider: string;
    status: 'pending' | 'paid' | 'failed' | 'canceled' | string;
    amount_cents: number;
    currency: string;
};

const statusCopy: Record<string, { title: string; body: string }> = {
    pending: { title: '正在确认支付', body: '支付结果确认后会自动刷新套餐。' },
    paid: { title: '支付已完成', body: '套餐和 Credits 已更新。' },
    failed: { title: '支付失败', body: '订单未完成，余额不会变化。' },
    canceled: { title: '支付已取消', body: '订单已取消，余额不会变化。' },
};

export default function BillingSuccess() {
    const [params] = useSearchParams();
    const orderId = params.get('order_id') || '';
    const navigate = useNavigate();
    const qc = useQueryClient();

    const { data: order, isLoading, error } = useQuery({
        queryKey: ['subscription-checkout-status', orderId],
        enabled: !!orderId,
        queryFn: () => fetchJson<PaymentOrder>(`/subscription/checkout/${orderId}/status`),
        refetchInterval: (query) => {
            const current = query.state.data as PaymentOrder | undefined;
            return !current || current.status === 'pending' ? 2000 : false;
        },
        refetchIntervalInBackground: true,
    });

    useEffect(() => {
        if (order?.status !== 'paid') return;
        qc.invalidateQueries({ queryKey: ['subscription-entitlements'] });
        qc.invalidateQueries({ queryKey: ['subscription-usage'] });
        qc.invalidateQueries({ queryKey: ['subscription-seats'] });
        qc.invalidateQueries({ queryKey: ['subscription-summary'] });
        qc.invalidateQueries({ queryKey: ['subscription-orders'] });
        qc.invalidateQueries({ queryKey: ['subscription-credit-transactions'] });
        const timer = window.setTimeout(() => navigate('/account/subscription'), 1200);
        return () => window.clearTimeout(timer);
    }, [navigate, order?.status, qc]);

    const status = order?.status || 'pending';
    const copy = statusCopy[status] || statusCopy.pending;
    const icon = status === 'paid'
        ? <IconCircleCheck size={34} color="var(--success)" />
        : status === 'failed' || status === 'canceled'
            ? <IconCircleX size={34} color="var(--error)" />
            : <IconLoader2 size={34} className="spin" />;

    return (
        <div style={{ maxWidth: 560, margin: '80px auto', padding: '0 18px' }}>
            <div className="card" style={{ padding: 28, textAlign: 'center' }}>
                <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 14 }}>
                    {icon}
                </div>
                <h1 style={{ margin: '0 0 8px', fontSize: 24 }}>{isLoading ? '正在加载订单' : copy.title}</h1>
                <p style={{ margin: '0 0 18px', color: 'var(--text-secondary)' }}>
                    {error ? '无法读取订单状态，请返回套餐详情页查看。' : copy.body}
                </p>
                {orderId && (
                    <div style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-tertiary)', fontSize: 12, marginBottom: 18 }}>
                        {orderId}
                    </div>
                )}
                <button className="btn btn-primary" type="button" onClick={() => navigate('/account/subscription')}>
                    返回套餐详情
                </button>
            </div>
        </div>
    );
}
