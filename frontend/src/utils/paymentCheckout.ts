const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

export function normalizeHostname(hostname: string): string {
    return hostname.split(':')[0].toLowerCase();
}

export function needsPaymentDomainRedirect(
    paymentHost: string | null | undefined,
    currentHostname: string,
): boolean {
    if (!paymentHost) return false;
    return normalizeHostname(currentHostname) !== normalizeHostname(paymentHost);
}

export function shouldRedirectToPaymentDomain(
    nativePaymentEnabled: boolean | null | undefined,
    paymentHost: string | null | undefined,
    currentHostname: string,
): boolean {
    return Boolean(
        nativePaymentEnabled
        && needsPaymentDomainRedirect(paymentHost, currentHostname),
    );
}

/** Carry the current path onto the payment origin, keeping the JWT in the hash. */
export function buildPaymentDomainRedirectUrl(options: {
    paymentHost: string;
    currentHref: string;
    sessionToken?: string | null;
}): string {
    const current = new URL(options.currentHref);
    const target = new URL(current.href);
    const host = options.paymentHost.trim();
    const targetIsLoopback = LOOPBACK_HOSTS.has(normalizeHostname(host));
    target.hostname = host;
    if (!targetIsLoopback) {
        target.protocol = 'https:';
        target.port = '';
    }
    if (options.sessionToken) {
        const fragment = new URLSearchParams(target.hash.replace(/^#/, ''));
        fragment.set('session_token', options.sessionToken);
        target.hash = fragment.toString();
    }
    return target.toString();
}
