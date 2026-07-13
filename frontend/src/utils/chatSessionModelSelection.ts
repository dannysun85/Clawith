import { canonicalizeModality } from '../constants/modalities';
import { resolveAllowedTier, type SaasTier } from '../constants/tiers';

export function resolveChatSessionTier(
    sessionTier: string | null | undefined,
    agentDefaultTier: string | null | undefined,
    allowedTiers?: string[],
): SaasTier | null {
    return resolveAllowedTier(sessionTier || agentDefaultTier, allowedTiers);
}

export function resolveChatSessionModality(
    sessionModality: string | null | undefined,
    agentDefaultModality: string | null | undefined,
): string {
    return canonicalizeModality(sessionModality || agentDefaultModality || 'text');
}
