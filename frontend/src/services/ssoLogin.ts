import { fetchJson } from './api';

export interface SsoProviderMetadata {
    provider_type: string;
    name: string;
}

interface SsoAuthorization extends SsoProviderMetadata {
    url: string;
}

type JsonFetcher = (url: string, options?: RequestInit) => Promise<unknown>;

export async function loadTenantSsoProviders(
    tenantId: string,
    fetcher: JsonFetcher = fetchJson,
): Promise<SsoProviderMetadata[]> {
    return await fetcher(
        `/sso/providers?tenant_id=${encodeURIComponent(tenantId)}`,
    ) as SsoProviderMetadata[];
}

export async function createTenantSsoAuthorization(
    tenantId: string,
    providerType: string,
    fetcher: JsonFetcher = fetchJson,
): Promise<string> {
    const session = await fetcher(
        `/sso/session?tenant_id=${encodeURIComponent(tenantId)}`,
        { method: 'POST' },
    ) as { session_id: string };
    const providers = await fetcher(
        `/sso/config?sid=${encodeURIComponent(session.session_id)}`,
    ) as SsoAuthorization[];
    const selected = providers.find(provider => provider.provider_type === providerType);
    if (!selected?.url) {
        throw new Error('The selected SSO provider is unavailable.');
    }
    return selected.url;
}
