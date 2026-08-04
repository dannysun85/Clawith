type SameOriginTenantSwitchOptions<TUser> = {
    tenantId: string;
    accessToken?: string | null;
    validateToken: (token: string) => Promise<TUser>;
    establishAuth: (user: TUser, token: string) => Promise<unknown> | unknown;
    persistTenantId: (tenantId: string) => void;
    clearTenantId: () => void;
    currentTenantId: () => string | null;
    resolvedTenantId: (user: TUser) => string | null | undefined;
};

type TenantSwitchCandidateOptions<TUser> = {
    tenantId: string;
    accessToken?: string | null;
    validateToken: (token: string) => Promise<TUser>;
    resolvedTenantId: (user: TUser) => string | null | undefined;
};

function requireTenantAccessToken(token?: string | null): string {
    if (!token) throw new Error('No tenant access token returned');
    return token;
}

export async function validateTenantSwitchCandidate<TUser>({
    tenantId,
    accessToken,
    validateToken,
    resolvedTenantId,
}: TenantSwitchCandidateOptions<TUser>): Promise<TUser> {
    const candidateToken = requireTenantAccessToken(accessToken);

    const user = await validateToken(candidateToken);
    const validatedTenantId = resolvedTenantId(user);
    if (!validatedTenantId || validatedTenantId !== tenantId) {
        throw new Error('Tenant access token does not match the requested company');
    }
    return user;
}

export async function validateCrossOriginTenantSwitch<TUser>({
    tenantId,
    accessToken,
    validateToken,
    resolvedTenantId,
    resolveCurrentOriginTenant,
}: Omit<TenantSwitchCandidateOptions<TUser>, 'tenantId'> & {
    tenantId?: string | null;
    resolveCurrentOriginTenant: () => Promise<{ id?: string | null } | null>;
}): Promise<TUser> {
    const candidateToken = requireTenantAccessToken(accessToken);
    const user = await validateToken(candidateToken);
    const tokenTenantId = resolvedTenantId(user);
    if (!tokenTenantId || (tenantId && tokenTenantId !== tenantId)) {
        throw new Error('Tenant access token does not match the requested company');
    }
    const expectedTenantId = tenantId || tokenTenantId;
    const originTenant = await resolveCurrentOriginTenant();
    if (!originTenant?.id || originTenant.id !== expectedTenantId) {
        throw new Error('Current browser origin does not belong to the requested company');
    }
    return user;
}

/**
 * Validate the candidate tenant identity before committing any local state.
 * A rejected/expired token therefore leaves the platform-admin session and
 * tenant storage untouched instead of producing a mixed-identity browser.
 */
export async function commitSameOriginTenantSwitch<TUser>({
    tenantId,
    accessToken,
    validateToken,
    establishAuth,
    persistTenantId,
    clearTenantId,
    currentTenantId,
    resolvedTenantId,
}: SameOriginTenantSwitchOptions<TUser>): Promise<TUser> {
    const candidateToken = requireTenantAccessToken(accessToken);
    const user = await validateTenantSwitchCandidate({
        tenantId,
        accessToken: candidateToken,
        validateToken,
        resolvedTenantId,
    });
    const validatedTenantId = resolvedTenantId(user) as string;

    // Stage the tenant identifier only after candidate validation, and roll it
    // back if browser-session/auth establishment fails. setAuth itself commits
    // local token/user state only after its HttpOnly cookie request succeeds.
    const previousTenantId = currentTenantId();
    persistTenantId(validatedTenantId);
    try {
        await establishAuth(user, candidateToken);
    } catch (error) {
        if (previousTenantId) persistTenantId(previousTenantId);
        else clearTenantId();
        throw error;
    }
    return user;
}
