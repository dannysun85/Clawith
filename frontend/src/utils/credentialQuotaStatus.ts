export interface CredentialQuotaStatusEntry {
    status: string;
    model?: string | null;
    error_code?: string | null;
}

export function summarizeCredentialQuota(
    modalityStatus?: Record<string, CredentialQuotaStatusEntry> | null,
): {
    blockedLabels: string[];
    sharedPlanBlocked: boolean;
    unsupportedModelLabels: string[];
} {
    const blockedResources = Object.entries(modalityStatus || {})
        .filter(([, value]) => value?.status === 'quota_exceeded');
    const resourceLabel = (
        [resource, value]: [string, CredentialQuotaStatusEntry],
    ) => {
        const modality = resource.split(':', 1)[0];
        return value.model ? `${modality} (${value.model})` : modality;
    };

    return {
        blockedLabels: blockedResources.map(resourceLabel),
        sharedPlanBlocked: blockedResources.some(
            ([resource]) => resource.split(':', 1)[0] === 'plan',
        ),
        unsupportedModelLabels: blockedResources
            .filter(([, value]) => value.error_code === 'UnsupportedModel')
            .map(resourceLabel),
    };
}
