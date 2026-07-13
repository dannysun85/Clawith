export interface CredentialQuotaStatusEntry {
    status: string;
    model?: string | null;
}

export function summarizeCredentialQuota(
    modalityStatus?: Record<string, CredentialQuotaStatusEntry> | null,
): { blockedLabels: string[]; sharedPlanBlocked: boolean } {
    const blockedResources = Object.entries(modalityStatus || {})
        .filter(([, value]) => value?.status === 'quota_exceeded');

    return {
        blockedLabels: blockedResources.map(([resource, value]) => {
            const modality = resource.split(':', 1)[0];
            return value.model ? `${modality} (${value.model})` : modality;
        }),
        sharedPlanBlocked: blockedResources.some(
            ([resource]) => resource.split(':', 1)[0] === 'plan',
        ),
    };
}
