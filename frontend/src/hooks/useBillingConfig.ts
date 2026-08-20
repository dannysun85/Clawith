import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '../services/api';

export interface BillingConfig {
    provider: string;
    usd_cny_rate: number;
    status: 'manual' | 'ready' | 'misconfigured' | 'unsupported' | string;
    checkout_enabled: boolean;
    native_payment_enabled: boolean;
    webhook_ready: boolean;
    missing_config: string[];
    issues: string[];
    next_action: string;
    /** Hostname of PAYMENT_BASE_URL (else PUBLIC_BASE_URL); real-money checkout is only accepted there. */
    payment_host?: string | null;
}

/** Active billing provider + USD→CNY display rate, shared by all billing pages. */
export function useBillingConfig() {
    return useQuery({
        queryKey: ['billing-config'],
        queryFn: () => fetchJson<BillingConfig>('/subscription/config'),
        staleTime: 5 * 60 * 1000,
    });
}
