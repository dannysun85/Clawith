import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconCircleCheck, IconX } from '@tabler/icons-react';
import { toDataURL } from 'qrcode';
import { fetchJson } from '../services/api';
import { formatMoneyCny } from '../utils/money';
import { useBillingConfig } from '../hooks/useBillingConfig';

interface OrderStatus {
    id: string;
    status: string;
}

interface WeChatPayModalProps {
    orderId: string;
    codeUrl: string;
    amountCents: number;
    currency: string;
    onPaid: () => void;
    onClose: () => void;
}

/** WeChat Pay Native (扫码) checkout: renders the code_url as a QR code and polls order status. */
export function WeChatPayModal({ orderId, codeUrl, amountCents, currency, onPaid, onClose }: WeChatPayModalProps) {
    const { t } = useTranslation();
    const { data: billingConfig } = useBillingConfig();
    const rate = billingConfig?.usd_cny_rate;
    const [qrDataUrl, setQrDataUrl] = useState('');
    const [qrFailed, setQrFailed] = useState(false);
    const [paid, setPaid] = useState(false);
    const onPaidRef = useRef(onPaid);
    onPaidRef.current = onPaid;

    useEffect(() => {
        let cancelled = false;
        setQrDataUrl('');
        setQrFailed(false);
        void toDataURL(codeUrl, { width: 220, margin: 2, color: { dark: '#16151d', light: '#ffffff' } })
            .then((dataUrl) => {
                if (!cancelled) setQrDataUrl(dataUrl);
            })
            .catch(() => {
                if (!cancelled) setQrFailed(true);
            });
        return () => {
            cancelled = true;
        };
    }, [codeUrl]);

    const { data: order } = useQuery({
        queryKey: ['subscription-checkout-status', orderId],
        queryFn: () => fetchJson<OrderStatus>(`/subscription/checkout/${orderId}/status`),
        refetchInterval: (query) => {
            const current = query.state.data as OrderStatus | undefined;
            return !current || current.status === 'pending' ? 2000 : false;
        },
        refetchIntervalInBackground: true,
    });

    useEffect(() => {
        if (order?.status !== 'paid' || paid) return;
        setPaid(true);
        onPaidRef.current();
    }, [order?.status, paid]);

    return (
        <div
            style={{
                position: 'fixed',
                inset: 0,
                zIndex: 1000,
                background: 'rgba(0, 0, 0, 0.5)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: 20,
            }}
            onClick={onClose}
        >
            <div
                className="card"
                style={{ width: 360, padding: 28, textAlign: 'center', position: 'relative' }}
                onClick={(event) => event.stopPropagation()}
            >
                <button
                    type="button"
                    aria-label={t('common.close', '关闭')}
                    onClick={onClose}
                    style={{
                        position: 'absolute',
                        top: 12,
                        right: 12,
                        border: 'none',
                        background: 'transparent',
                        cursor: 'pointer',
                        color: 'var(--text-tertiary)',
                        display: 'flex',
                    }}
                >
                    <IconX size={18} />
                </button>
                {paid ? (
                    <div style={{ padding: '28px 0 12px' }}>
                        <IconCircleCheck size={44} color="var(--success)" />
                        <h3 style={{ margin: '14px 0 6px', fontSize: 18 }}>
                            {t('enterprise.subscription.wechatPaid', '支付成功')}
                        </h3>
                        <p style={{ margin: '0 0 18px', color: 'var(--text-tertiary)', fontSize: 13 }}>
                            {t('enterprise.subscription.wechatPaidDesc', '套餐和 Credits 已更新。')}
                        </p>
                        <button type="button" className="btn btn-primary" style={{ width: '100%', justifyContent: 'center' }} onClick={onClose}>
                            {t('common.done', '完成')}
                        </button>
                    </div>
                ) : (
                    <>
                        <h3 style={{ margin: '0 0 4px', fontSize: 18 }}>
                            {t('enterprise.subscription.wechatPayTitle', '微信扫码支付')}
                        </h3>
                        <div style={{ fontSize: 26, fontWeight: 750, margin: '6px 0 14px' }}>
                            {formatMoneyCny(currency, amountCents, rate)}
                        </div>
                        <div
                            style={{
                                width: 224,
                                height: 224,
                                margin: '0 auto',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                border: '1px solid var(--border-subtle)',
                                borderRadius: 8,
                                background: '#fff',
                            }}
                        >
                            {qrDataUrl ? (
                                <img src={qrDataUrl} width={220} height={220} alt={t('enterprise.subscription.wechatPayQr', '微信支付二维码')} />
                            ) : (
                                <span style={{ color: 'var(--text-tertiary)', fontSize: 13, padding: 12 }}>
                                    {qrFailed
                                        ? t('enterprise.subscription.wechatQrFailed', '二维码生成失败，请重新下单')
                                        : t('enterprise.subscription.wechatQrLoading', '正在生成二维码…')}
                                </span>
                            )}
                        </div>
                        <p style={{ margin: '14px 0 0', color: 'var(--text-tertiary)', fontSize: 13 }}>
                            {t('enterprise.subscription.wechatPayHint', '请使用微信「扫一扫」完成支付，支付成功后会自动确认。')}
                        </p>
                    </>
                )}
            </div>
        </div>
    );
}
