import { canonicalizeModality } from '../constants/modalities';
import { resolveAllowedTier, type SaasTier } from '../constants/tiers';

export function resolveChatSessionTier(
    sessionTier: string | null | undefined,
    agentDefaultTier: string | null | undefined,
    allowedTiers?: string[],
    userPreferredTier?: string | null,
): SaasTier | null {
    return resolveAllowedTier(userPreferredTier || sessionTier || agentDefaultTier, allowedTiers);
}

export function resolveChatSessionModality(
    sessionModality: string | null | undefined,
    agentDefaultModality: string | null | undefined,
): string {
    return canonicalizeModality(sessionModality || agentDefaultModality || 'text');
}

export function resolveOutboundChatRoute(
    persistentModality: string,
    hasImageAttachment: boolean,
    hasVideoAttachment: boolean,
): { modality: string; ephemeral: boolean } {
    if (hasVideoAttachment) return { modality: 'video', ephemeral: true };
    if (hasImageAttachment) return { modality: 'image', ephemeral: true };
    return { modality: canonicalizeModality(persistentModality), ephemeral: false };
}

export function shouldApplyChatTierPreferenceResponse(
    responseSequence: number,
    latestSequence: number,
    requestUserId: string | null | undefined,
    currentUserId: string | null | undefined,
    incomingRevision: number | null | undefined,
    currentRevision: number | null | undefined,
): boolean {
    if (responseSequence !== latestSequence) return false;
    if (!requestUserId || requestUserId !== currentUserId) return false;
    if (incomingRevision == null) return true;
    return incomingRevision >= (currentRevision ?? 0);
}
