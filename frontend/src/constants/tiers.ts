export type SaasTier = 'lite' | 'pro' | 'ultra';

export const SAAS_TIERS: SaasTier[] = ['lite', 'pro', 'ultra'];

export function resolveAllowedTier(
    preferred: string | null | undefined,
    allowedTiers?: string[],
    fallback: SaasTier = 'pro',
): SaasTier | null {
    const normalizedPreferred = preferred?.trim().toLowerCase();
    const preferredTier = SAAS_TIERS.find((tier) => tier === normalizedPreferred);

    if (allowedTiers && allowedTiers.length > 0) {
        const normalizedAllowed = new Set(allowedTiers.map((tier) => tier.trim().toLowerCase()));
        const supportedAllowed = SAAS_TIERS.filter((tier) => normalizedAllowed.has(tier));
        if (preferredTier && supportedAllowed.includes(preferredTier)) return preferredTier;
        return supportedAllowed[0] || null;
    }

    return preferredTier || fallback;
}
