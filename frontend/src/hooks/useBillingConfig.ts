import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '../services/api';

export interface BillingConfig {
    provider: string;
    usd_cny_rate: number;
}

/** Active billing provider + USD→CNY display rate, shared by all billing pages. */
export function useBillingConfig() {
    return useQuery({
        queryKey: ['billing-config'],
        queryFn: () => fetchJson<BillingConfig>('/subscription/config'),
        staleTime: 5 * 60 * 1000,
    });
}
