import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '../services/api';
import type { Agent } from '../types';
import type { Entitlements } from './useLlmModels';

export const SUBSCRIPTION_UPGRADE_PATH = '/account/subscription';

type AgentWithExpiry = Agent & { is_expired?: boolean };
type SeatUsage = {
    seats_total: number;
    seats_used: number;
    pending_invites: number;
};

type AgentCreationLimitOptions = {
    enabled?: boolean;
};

export function countActiveAgents(agents: AgentWithExpiry[]): number {
    return agents.filter((agent) => (
        agent.status !== 'stopped'
        && agent.status !== 'error'
        && agent.is_expired !== true
        && agent.is_system !== true
    )).length;
}

export function agentLimitMessage(isChinese: boolean, used: number, limit: number): string {
    return isChinese
        ? `当前套餐最多可创建 ${limit} 个智能体，已使用 ${used}/${limit}。请升级套餐后继续。`
        : `Your current plan allows ${limit} agents and has used ${used}/${limit}. Upgrade your plan to continue.`;
}

export function useAgentCreationLimit(
    providedAgents?: AgentWithExpiry[] | null,
    options: AgentCreationLimitOptions = {},
) {
    const enabled = options.enabled ?? true;
    const { data: entitlements, isLoading: loadingEntitlements } = useQuery({
        queryKey: ['subscription-entitlements'],
        queryFn: () => fetchJson<Entitlements | null>('/subscription/my-entitlements'),
        enabled,
    });

    const { data: seats, isLoading: loadingSeats } = useQuery({
        queryKey: ['subscription-seats'],
        queryFn: () => fetchJson<SeatUsage>('/subscription/seats'),
        refetchInterval: 30000,
        enabled,
    });

    const agents = (providedAgents ?? []) as AgentWithExpiry[];
    const activeCount = useMemo(() => countActiveAgents(agents), [agents]);
    const used = Number(seats?.seats_used ?? activeCount);
    const maxAgents = Number(seats?.seats_total ?? entitlements?.max_agents ?? 0);
    const isLimited = maxAgents > 0 && used >= maxAgents;

    return {
        activeCount: used,
        maxAgents,
        isLimited,
        isLoading: loadingEntitlements || loadingSeats,
        entitlements,
    };
}
