/** API service layer */

import type { Agent, TokenResponse, User, Task, ChatMessage } from '../types';
import { clearAuthStorage } from '../utils/authStorage';
import { buildWorkspaceDownloadUrl } from '../utils/authTransport';
import { reportClientIssue } from './productionIssueReporter';
import type { AgentChannelEndpoint } from '../utils/agentChannelSetup';
import { AppError, normalizeUnknownError, parseHttpError, parseHttpErrorResponse } from './apiError';

export { ApiError, AppError } from './apiError';
export type { ApiErrorContext, AppErrorContext, ErrorSource } from './apiError';

const API_BASE = '/api';

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
    const token = localStorage.getItem('token');
    const headers = new Headers(options.headers);
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);

    let res: Response;
    try {
        res = await fetch(`${API_BASE}${url}`, { ...options, headers });
    } catch (error) {
        reportClientIssue({
            category: 'api',
            error_code: error instanceof Error ? error.name : 'NetworkError',
            route: `${API_BASE}${url}`,
            operation: options.method || 'GET',
            metadata: { component: 'fetch' },
        });
        throw error;
    }

    if (!res.ok) {
        const apiError = await parseHttpErrorResponse(res);
        if (res.status >= 500) {
            reportClientIssue({
                category: 'api',
                error_code: `http_${res.status}`,
                route: `${API_BASE}${url}`,
                operation: options.method || 'GET',
                metadata: { status_code: res.status, component: 'fetch' },
            });
        }
        // Auto-logout on expired/invalid token (but not on auth endpoints — let them show errors)
        const isAuthEndpoint = url.startsWith('/auth/login')
            || url.startsWith('/auth/mfa/')
            || url.startsWith('/auth/register')
            || url.startsWith('/auth/verify-email')
            || url.startsWith('/auth/resend-verification')
            || url.startsWith('/auth/forgot-password')
            || url.startsWith('/auth/reset-password');
        const explicitAuthorization = new Headers(options.headers).get('Authorization');
        const storedAuthorization = token ? `Bearer ${token}` : null;
        const isCandidateTokenRequest = Boolean(
            explicitAuthorization
            && explicitAuthorization !== storedAuthorization,
        );
        if (res.status === 401 && !isAuthEndpoint && !isCandidateTokenRequest) {
            clearAuthStorage();
            window.location.href = '/login';
            throw apiError;
        }
        if (
            res.status === 403
            && (apiError.code === 'mfa_setup_required' || apiError.code === 'mfa_challenge_required')
            && !url.startsWith('/auth/mfa/')
            && window.location.pathname !== '/account/security'
        ) {
            window.location.href = `/account/security?reason=${encodeURIComponent(apiError.code)}`;
            throw apiError;
        }
        throw apiError;
    }

    if (res.status === 204) return undefined as T;
    return res.json();
}

/** Legacy/Internal generic fetcher */
export const fetchJson = request;

async function uploadFile(url: string, file: File, extraFields?: Record<string, string>): Promise<any> {
    const token = localStorage.getItem('token');
    const formData = new FormData();
    formData.append('file', file);
    if (extraFields) {
        for (const [k, v] of Object.entries(extraFields)) {
            formData.append(k, v);
        }
    }
    let res: Response;
    try {
        res = await fetch(`${API_BASE}${url}`, {
            method: 'POST',
            headers: token ? { Authorization: `Bearer ${token}` } : {},
            body: formData,
        });
    } catch (error) {
        throw normalizeUnknownError(error, { code: 'network_error', source: 'http', retryable: true });
    }
    if (!res.ok) {
        if (res.status >= 500) {
            reportClientIssue({
                category: 'api',
                error_code: `http_${res.status}`,
                route: `${API_BASE}${url}`,
                operation: 'POST',
                metadata: { status_code: res.status, component: 'upload' },
            });
        }
        throw await parseHttpErrorResponse(res);
    }
    return res.json();
}

// Upload with progress tracking via XMLHttpRequest.
// Returns { promise, abort } — call abort() to cancel the upload.
// Progress callback: 0-100 = upload phase, 101 = processing phase (server is parsing the file).
export function uploadFileWithProgress(
    url: string,
    file: File,
    onProgress?: (percent: number) => void,
    extraFields?: Record<string, string>,
    timeoutMs: number = 120_000,
): { promise: Promise<any>; abort: () => void } {
    const xhr = new XMLHttpRequest();
    const promise = new Promise<any>((resolve, reject) => {
        const token = localStorage.getItem('token');
        const formData = new FormData();
        formData.append('file', file);
        if (extraFields) {
            for (const [k, v] of Object.entries(extraFields)) {
                formData.append(k, v);
            }
        }
        xhr.open('POST', `${API_BASE}${url}`);
        if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

        // Upload phase: 0-100%
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable && onProgress) {
                onProgress(Math.round((e.loaded / e.total) * 100));
            }
        };
        // Upload bytes finished → enter processing phase
        xhr.upload.onload = () => {
            if (onProgress) onProgress(101); // 101 = "processing" sentinel
        };

        xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                try { resolve(JSON.parse(xhr.responseText)); } catch { resolve(undefined); }
            } else {
                reject(parseHttpError({
                    status: xhr.status,
                    statusText: xhr.statusText,
                    bodyText: xhr.responseText,
                    traceId: xhr.getResponseHeader('X-Trace-Id'),
                }));
            }
        };
        xhr.onerror = () => reject(new AppError({ message: 'Network error', code: 'network_error', source: 'http', retryable: true }));
        xhr.ontimeout = () => reject(new AppError({ message: 'Upload timed out', code: 'upload_timeout', source: 'http', retryable: true }));
        xhr.onabort = () => reject(new AppError({ message: 'Upload cancelled', code: 'upload_cancelled', source: 'http', retryable: false }));
        xhr.timeout = timeoutMs;
        xhr.send(formData);
    });
    return { promise, abort: () => xhr.abort() };
}

// ─── Auth ─────────────────────────────────────────────
export type MfaLoginChallenge = {
    requires_mfa: boolean;
    requires_mfa_setup: boolean;
    challenge_token: string;
    expires_in_seconds: number;
};

export type MfaSetupPayload = {
    challenge_token: string;
    secret: string;
    provisioning_uri: string;
    expires_in_seconds: number;
};

export type MfaTokenResponse = TokenResponse & {
    recovery_codes?: string[];
};

export type MfaStatus = {
    enabled: boolean;
    required: boolean;
    recommended?: boolean;
    confirmed_at: string | null;
    recovery_codes_remaining: number;
};

export const authApi = {
    registrationConfig: () =>
        request<{
            invitation_code_required: boolean;
            password_registration_available?: boolean;
        }>('/auth/registration-config'),

    register: (data: { username?: string; email: string; password: string; display_name: string; invitation_code?: string; provider?: string; provider_code?: string }) =>
        request<{ user_id: string; email: string; access_token: string; message: string; user?: any; needs_company_setup: boolean }>('/auth/register/init', { method: 'POST', body: JSON.stringify(data) }),

    login: (data: { login_identifier: string; password: string; tenant_id?: string }) =>
        request<TokenResponse | MfaLoginChallenge | { requires_tenant_selection: boolean; login_identifier: string; tenants: any[] }>('/auth/login', { method: 'POST', body: JSON.stringify(data) }),

    mfaStatus: () => request<MfaStatus>('/auth/mfa/status'),

    startMfaSetup: (currentPassword: string) =>
        request<MfaSetupPayload>('/auth/mfa/setup', {
            method: 'POST',
            body: JSON.stringify({ current_password: currentPassword }),
        }),

    startMfaBootstrap: (challengeToken: string) =>
        request<MfaSetupPayload>('/auth/mfa/bootstrap/setup', {
            method: 'POST',
            body: JSON.stringify({ challenge_token: challengeToken }),
        }),

    confirmMfaSetup: (challengeToken: string, code: string) =>
        request<MfaTokenResponse>('/auth/mfa/setup/confirm', {
            method: 'POST',
            body: JSON.stringify({ challenge_token: challengeToken, code }),
        }),

    verifyMfaChallenge: (challengeToken: string, code: string) =>
        request<MfaTokenResponse>('/auth/mfa/challenge/verify', {
            method: 'POST',
            body: JSON.stringify({ challenge_token: challengeToken, code }),
        }),

    rotateMfaRecoveryCodes: (currentPassword: string, code: string) =>
        request<{ access_token: string; recovery_codes: string[] }>('/auth/mfa/recovery-codes/rotate', {
            method: 'POST',
            body: JSON.stringify({ current_password: currentPassword, code }),
        }),

    disableMfa: (currentPassword: string, code: string) =>
        request<{ ok: boolean; requires_setup: boolean; access_token: string | null }>('/auth/mfa/disable', {
            method: 'POST',
            body: JSON.stringify({ current_password: currentPassword, code }),
        }),

    forgotPassword: (data: { email: string }) =>
        request<{ ok: boolean; message: string }>('/auth/forgot-password', { method: 'POST', body: JSON.stringify(data) }),

    resetPassword: (data: { token: string; new_password: string }) =>
        request<{ ok: boolean }>('/auth/reset-password', { method: 'POST', body: JSON.stringify(data) }),

    emailHint: (username: string) =>
        request<{ hint: string }>(`/auth/email-hint?username=${encodeURIComponent(username)}`),

    me: (accessToken?: string) => request<User>(
        '/auth/me',
        accessToken
            ? { headers: { Authorization: `Bearer ${accessToken}` } }
            : undefined,
    ),

    updateMe: (data: Partial<User> & { current_password?: string }) =>
        request<User>('/auth/me', { method: 'PATCH', body: JSON.stringify(data) }),

    verifyEmail: (token: string) =>
        request<{ ok: boolean; message: string; access_token: string; user: User; needs_company_setup: boolean }>('/auth/verify-email', { method: 'POST', body: JSON.stringify({ token }) }),

    resendVerification: (email: string) =>
        request<{ ok: boolean; message: string }>('/auth/resend-verification', { method: 'POST', body: JSON.stringify({ email }) }),

    getMyTenants: () =>
        request<Array<{
            tenant_id: string;
            tenant_name: string;
            tenant_slug: string;
            logo_url: string | null;
            membership_role: 'member' | 'org_admin' | 'org_owner';
        }>>('/auth/my-tenants'),

    switchTenant: (tenantId: string) =>
        request<{ access_token: string; target_tenant_id: string; redirect_url?: string; message?: string }>('/auth/switch-tenant', { method: 'POST', body: JSON.stringify({ tenant_id: tenantId }) }),
};

// ─── Tenants ──────────────────────────────────────────
export type TenantDepartureResult = {
    status: string;
    fallback_tenant_id?: string | null;
    access_token?: string | null;
    deletion_scheduled_for?: string | null;
};

export type TenantLeavePreflight = {
    version: number;
    tenant_id: string;
    membership_id: string;
    can_leave: boolean;
    requires_acknowledgement: boolean;
    blockers: Array<{ code: string; count: number; message: string }>;
    summary: {
        owned_agents: number;
        open_tasks: number;
        pending_approvals: number;
        open_deliverables: number;
        delegated_agents: number;
        personal_credentials: number;
        pending_ownership_transfers: number;
    };
    owned_agents: Array<{
        id: string;
        name: string;
        status: string;
        access_mode: string;
        is_personal_assistant: boolean;
        required_action: 'delete' | 'handover_or_delete';
    }>;
    open_tasks: Array<{ id: string; title: string; status: string; agent_id: string }>;
    pending_approvals: Array<{ id: string; action_type: string; agent_id?: string | null }>;
    open_deliverables: Array<{ id: string; status: string; work_type: string; agent_id: string }>;
    delegated_agents: Array<{ id: string; name: string }>;
    effects_on_leave: Record<string, string>;
};

export type TenantOwnershipTransfer = {
    id: string;
    tenant_id: string;
    current_owner_user_id: string;
    proposed_owner_user_id: string;
    status: string;
    expires_at: string;
    accepted_at?: string | null;
    cancelled_at?: string | null;
    created_at: string;
};

export type TenantTokenUsageBucket = {
    total_tokens: number;
    cache_read_tokens: number;
    cache_creation_tokens: number;
    cache_hit_rate: number;
};

export type TenantTokenUsage = {
    today: TenantTokenUsageBucket;
    month: TenantTokenUsageBucket;
    total: TenantTokenUsageBucket;
};

export const tenantApi = {
    selfCreate: (data: { name: string; timezone?: string; country_region?: string }, idempotencyKey?: string) =>
        request<any>('/tenants/self-create', {
            method: 'POST',
            headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
            body: JSON.stringify(data),
        }),

    join: (invitationCode: string) =>
        request<any>('/tenants/join', { method: 'POST', body: JSON.stringify({ invitation_code: invitationCode }) }),

    acceptInvitation: (invitationId: string) =>
        request<any>(`/tenants/invitations/${invitationId}/accept`, { method: 'POST' }),

    declineInvitation: (invitationId: string) =>
        request<{ status: string }>(`/tenants/invitations/${invitationId}/decline`, { method: 'POST' }),

    registrationConfig: () =>
        request<{ allow_self_create_company: boolean }>('/tenants/registration-config'),

    resolveByDomain: (domain: string) =>
        request<any>(`/tenants/resolve-by-domain?domain=${encodeURIComponent(domain)}`),

    me: () =>
        request<{ id: string; name: string; default_model_id: string | null; [k: string]: any }>('/tenants/me'),

    update: (tenantId: string, data: {
        name?: string;
        timezone?: string;
        country_region?: string;
        company_size?: string;
        allow_member_private_agents?: boolean;
        default_approval_policy?: string;
    }) => request<any>(`/tenants/${tenantId}`, { method: 'PUT', body: JSON.stringify(data) }),

    tokenUsage: () =>
        request<TenantTokenUsage>('/tenants/me/token-usage'),

    requestOwnershipTransfer: (tenantId: string, data: { new_owner_user_id: string; current_password: string }) =>
        request<any>(`/tenants/${tenantId}/ownership-transfers`, { method: 'POST', body: JSON.stringify(data) }),

    pendingOwnershipTransfer: (tenantId: string) =>
        request<{ item: TenantOwnershipTransfer | null }>(`/tenants/${tenantId}/ownership-transfers/pending`),

    acceptOwnershipTransfer: (tenantId: string, transferId: string) =>
        request<any>(`/tenants/${tenantId}/ownership-transfers/${transferId}/accept`, { method: 'POST' }),

    cancelOwnershipTransfer: (tenantId: string, transferId: string) =>
        request<any>(`/tenants/${tenantId}/ownership-transfers/${transferId}`, { method: 'DELETE' }),

    leavePreflight: (tenantId: string) =>
        request<TenantLeavePreflight>(`/tenants/${tenantId}/leave-preflight`),

    leave: (tenantId: string, acknowledgeResponsibilities = false) =>
        request<TenantDepartureResult>(`/tenants/${tenantId}/leave`, {
            method: 'POST',
            body: JSON.stringify({
                confirmation: 'LEAVE',
                acknowledge_responsibilities: acknowledgeResponsibilities,
            }),
        }),

    scheduleDeletion: (tenantId: string, data: { company_name: string; current_password: string }) =>
        request<TenantDepartureResult>(`/tenants/${tenantId}`, { method: 'DELETE', body: JSON.stringify(data) }),

    restore: (tenantId: string, currentPassword?: string) =>
        request<any>(`/tenants/${tenantId}/restore`, {
            method: 'POST',
            body: JSON.stringify({ current_password: currentPassword || null }),
        }),
};

export type PendingOrganizationInvitation = {
    id: string;
    tenant_id: string;
    tenant_name: string;
    role: 'member' | 'org_admin' | 'org_owner';
    expires_at: string;
    created_at: string;
};

export type OrganizationInvitation = {
    id: string;
    target_email: string;
    role: 'member' | 'org_admin' | 'org_owner';
    token_prefix: string;
    status: string;
    delivery_mode: 'email' | 'manual_link';
    delivery_status: string;
    delivery?: {
        id: string;
        status: string;
        recipient_mask: string;
        attempt_count: number;
        max_attempts: number;
        next_attempt_at?: string | null;
        last_error_code?: string | null;
        smtp_accepted_at?: string | null;
    } | null;
    expires_at: string;
    accepted_at?: string | null;
    created_at: string;
};

export const governanceApi = {
    pendingInvitations: () =>
        request<{ items: PendingOrganizationInvitation[] }>('/governance/me/pending-invitations'),

    organizationInvitations: (tenantId: string) =>
        request<{ items: OrganizationInvitation[] }>(`/governance/organizations/${tenantId}/invitations`),

    createOrganizationInvitation: (
        tenantId: string,
        data: { email: string; role: 'member' | 'org_admin'; expires_in_days: number },
        idempotencyKey = crypto.randomUUID(),
    ) => request<OrganizationInvitation>(`/governance/organizations/${tenantId}/invitations`, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify(data),
    }),

    resendOrganizationInvitation: (
        tenantId: string,
        invitationId: string,
        idempotencyKey = crypto.randomUUID(),
    ) => request<OrganizationInvitation>(`/governance/organizations/${tenantId}/invitations/${invitationId}/resend`, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
    }),

    issueOrganizationInvitationManualLink: (
        tenantId: string,
        invitationId: string,
        currentPassword: string,
    ) => request<OrganizationInvitation & { manual_url: string; one_time_display: true }>(
        `/governance/organizations/${tenantId}/invitations/${invitationId}/manual-link`,
        { method: 'POST', body: JSON.stringify({ current_password: currentPassword }) },
    ),

    revokeOrganizationInvitation: (tenantId: string, invitationId: string) =>
        request<{ status: string }>(`/governance/organizations/${tenantId}/invitations/${invitationId}`, {
            method: 'DELETE',
        }),

    joinLinks: (tenantId: string) =>
        request<{ items: any[] }>(`/governance/organizations/${tenantId}/join-links`),

    createJoinLink: (tenantId: string, data: { max_uses: number; expires_in_days: number }) =>
        request<any>(`/governance/organizations/${tenantId}/join-links`, {
            method: 'POST',
            body: JSON.stringify(data),
        }),

    revokeJoinLink: (tenantId: string, linkId: string) =>
        request<{ status: string }>(`/governance/organizations/${tenantId}/join-links/${linkId}`, {
            method: 'DELETE',
        }),

    registrationGrants: () =>
        request<{ items: any[] }>('/governance/platform/registration-grants'),

    createRegistrationGrants: (data: { count: number; max_uses: number; expires_in_days: number | null }) =>
        request<{ items: any[] }>('/governance/platform/registration-grants', {
            method: 'POST',
            body: JSON.stringify(data),
        }),

    revokeRegistrationGrant: (grantId: string) =>
        request<{ status: string }>(`/governance/platform/registration-grants/${grantId}`, { method: 'DELETE' }),

    createSupportSession: (data: {
        tenant_id: string;
        reason: string;
        scopes: string[];
        duration_minutes: number;
    }) => request<any>('/governance/platform/support-sessions', {
        method: 'POST',
        body: JSON.stringify(data),
    }),

    endSupportSession: (sessionId: string) =>
        request<{ status: string }>(`/governance/platform/support-sessions/${sessionId}`, { method: 'DELETE' }),

    supportTenantSummary: (sessionId: string, tenantId: string) =>
        request<{
            support_session_id: string;
            tenant_id: string;
            scopes_applied: string[];
            metadata?: {
                name: string;
                slug: string;
                is_active: boolean;
                timezone: string;
                country_region: string;
                sso_enabled: boolean;
                created_at?: string;
            };
            diagnostics?: {
                memberships_total: number;
                memberships_active: number;
                agents_total: number;
                agents_active: number;
            };
        }>(`/governance/platform/support-sessions/${sessionId}/tenants/${tenantId}/summary`),

    ownershipResolutions: () =>
        request<{ items: any[] }>('/governance/platform/ownership-resolutions'),

    resolveOwnership: (resolutionId: string, data: { owner_user_id: string; reason: string }) =>
        request<any>(`/governance/platform/ownership-resolutions/${resolutionId}/resolve`, {
            method: 'POST',
            body: JSON.stringify(data),
        }),
};

export const membershipApi = {
    list: () => request<any[]>('/users/'),
    updateRole: (userId: string, role: 'member' | 'org_admin') =>
        request<any>(`/users/${userId}/role`, { method: 'PATCH', body: JSON.stringify({ role }) }),
    deactivationPreflight: (userId: string) =>
        request<TenantLeavePreflight>(`/users/${userId}/deactivation-preflight`),
    deactivate: (userId: string, acknowledgeResponsibilities = false) =>
        request<{ status: string }>(`/users/${userId}/deactivate`, {
            method: 'POST',
            body: JSON.stringify({ acknowledge_responsibilities: acknowledgeResponsibilities }),
        }),
    reactivate: (userId: string) =>
        request<{ status: string }>(`/users/${userId}/reactivate`, { method: 'POST' }),
    resetMfa: (userId: string, data: { current_password: string; reason: string }) =>
        request<{ ok: boolean; target_user_id: string; requires_setup: boolean }>(`/auth/mfa/admin/reset/${userId}`, {
            method: 'POST',
            body: JSON.stringify(data),
        }),
};

export const onboardingApi = {
    status: () =>
        request<any>('/onboarding/status'),

    start: (entryMode: 'create' | 'join') =>
        request<any>('/onboarding/start', { method: 'POST', body: JSON.stringify({ entry_mode: entryMode }) }),

    initializeCompany: (data: {
        name: string;
        timezone: string;
        country_region: string;
        company_size: string;
        allow_member_private_agents: boolean;
        default_approval_policy: string;
    }) => request<any>('/onboarding/company', { method: 'POST', body: JSON.stringify(data) }),

    completeProfile: (data: {
        display_name: string;
        title: string;
        timezone: string;
        work_hours_start: string;
        work_hours_end: string;
    }) => request<any>('/onboarding/profile', { method: 'POST', body: JSON.stringify(data) }),

    createPersonalAssistant: (data: { name: string; personality: string; work_style: string; proactivity?: string; boundaries?: string }) =>
        request<any>('/onboarding/personal-assistant', { method: 'POST', body: JSON.stringify(data) }),

    complete: () =>
        request<any>('/onboarding/complete', { method: 'POST' }),
};

export const adminApi = {
    listCompanies: () =>
        request<any[]>('/admin/companies'),

    listTenantDeletions: () =>
        request<{
            items: Array<{
                tenant_id: string;
                tenant_name: string;
                is_active: boolean;
                deletion_requested_at: string;
                eligible_at: string;
                is_due: boolean;
                job_status: 'scheduled' | 'dry_run_passed' | 'purging' | 'held' | 'failed';
                attempt_count: number;
                last_error_code: string | null;
                plan_digest: string | null;
                holds: Array<{
                    id: string;
                    hold_type: 'legal' | 'operations';
                    reason_code: string;
                    created_at: string;
                }>;
            }>;
            tombstones: Array<{
                tenant_id: string;
                purged_at: string;
                reason_code: string;
                receipt_hash: string;
                rows_total: number;
                schema_version: number;
            }>;
        }>('/admin/tenant-deletions'),

    dryRunTenantDeletion: (tenantId: string) =>
        request<any>(`/admin/tenant-deletions/${tenantId}/dry-run`, { method: 'POST' }),

    createTenantDeletionHold: (
        tenantId: string,
        data: { hold_type: 'legal' | 'operations'; reason_code: string },
    ) => request<any>(`/admin/tenant-deletions/${tenantId}/holds`, {
        method: 'POST',
        body: JSON.stringify(data),
    }),

    releaseTenantDeletionHold: (tenantId: string, holdId: string, reasonCode: string) =>
        request<any>(`/admin/tenant-deletions/${tenantId}/holds/${holdId}/release`, {
            method: 'POST',
            body: JSON.stringify({ reason_code: reasonCode }),
        }),

    createCompany: (data: { name: string }) =>
        request<any>('/admin/companies', { method: 'POST', body: JSON.stringify(data) }),

    updateCompany: (id: string, data: any) =>
        request<any>(`/tenants/${id}`, { method: 'PUT', body: JSON.stringify(data) }),

    toggleCompany: (id: string) =>
        request<any>(`/admin/companies/${id}/toggle`, { method: 'PUT' }),

    getPlatformSettings: () =>
        request<any>('/admin/platform-settings'),

    updatePlatformSettings: (data: any) =>
        request<any>('/admin/platform-settings', { method: 'PUT', body: JSON.stringify(data) }),

    listRegistrationCodes: (params: { page?: number; page_size?: number; search?: string } = {}) => {
        const qs = new URLSearchParams();
        if (params.page) qs.set('page', String(params.page));
        if (params.page_size) qs.set('page_size', String(params.page_size));
        if (params.search) qs.set('search', params.search);
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return request<any>(`/admin/registration-codes${suffix}`);
    },

    createRegistrationCodes: (data: { count: number; max_uses: number }) =>
        request<any>('/admin/registration-codes', { method: 'POST', body: JSON.stringify(data) }),

    deactivateRegistrationCode: (id: string) =>
        request<any>(`/admin/registration-codes/${id}`, { method: 'DELETE' }),
};

// ─── Agents ───────────────────────────────────────────
export const agentApi = {
    list: (tenantId?: string) => request<Agent[]>(`/agents/${tenantId ? `?tenant_id=${tenantId}` : ''}`),

    get: (id: string) => request<Agent>(`/agents/${id}`),

    create: (data: any) =>
        request<any>('/agents/', { method: 'POST', body: JSON.stringify(data) }),

    update: (id: string, data: Partial<Agent>) =>
        request<Agent>(`/agents/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),

    delete: (id: string) =>
        request<void>(`/agents/${id}`, { method: 'DELETE' }),

    start: (id: string) =>
        request<Agent>(`/agents/${id}/start`, { method: 'POST' }),

    stop: (id: string) =>
        request<Agent>(`/agents/${id}/stop`, { method: 'POST' }),

    recover: (id: string) =>
        request<Agent>(`/agents/${id}/recover`, { method: 'POST' }),

    updateLegacyAssistantDisposition: (
        id: string,
        data: {
            action: 'archive' | 'convert_to_employee' | 'restore_history';
            expected_disposition: 'active' | 'archived' | 'converted';
        },
    ) => request<Agent>(`/agents/${id}/legacy-assistant-disposition`, {
        method: 'POST',
        body: JSON.stringify(data),
    }),

    metrics: (id: string) =>
        request<any>(`/agents/${id}/metrics`),

    collaborators: (id: string) =>
        request<any[]>(`/agents/${id}/collaborators`),

    templates: () =>
        request<any[]>('/agents/templates'),

    capabilityReadiness: (id: string) =>
        request<any>(`/agents/${id}/capability-readiness`),

    // OpenClaw gateway
    generateApiKey: (id: string) =>
        request<{ api_key: string; message: string }>(`/agents/${id}/api-key`, { method: 'POST' }),

    gatewayMessages: (id: string) =>
        request<any[]>(`/agents/${id}/gateway-messages`),
};

export type WorkArtifact = {
    id: string;
    artifact_type: string;
    status: string;
    workspace_path: string;
    revision_number: number;
};

export type WorkItem = {
    id: string;
    kind: 'task' | 'deliverable';
    title: string;
    intent: string;
    origin_type: string;
    executor_kind: string;
    executor_snapshot: Record<string, any>;
    work_statement: Record<string, any>;
    formal_delivery_spec?: Record<string, string | number>;
    confirmed_at?: string | null;
    agent_id: string;
    agent_name: string;
    task_id?: string | null;
    task_status?: string | null;
    priority?: string | null;
    run_id?: string | null;
    execution_status: string;
    deliverable_id?: string | null;
    work_type?: string | null;
    deliverable_status?: string | null;
    artifact_status?: string | null;
    review_status?: string | null;
    approval_status?: string | null;
    delivery_status: string;
    delivery_mode: 'task_only' | 'formal_deliverable';
    user_stage: string;
    artifacts: WorkArtifact[];
    latest_update?: string | null;
    latest_update_at?: string | null;
    deep_link: string;
    formal_delivery_link?: string | null;
    created_at: string;
    updated_at: string;
};

export type WorkExecutorKind = 'personal_assistant' | 'agent_employee' | 'temporary_expert' | 'group';

export type WorkTaskDraft = {
    title: string;
    intent: string;
    work_type: 'general' | 'image' | 'video' | 'presentation' | 'document';
    priority: 'low' | 'medium' | 'high' | 'urgent';
    routing_mode?: 'auto' | 'manual';
    executor_kind?: WorkExecutorKind;
    agent_id?: string;
    expert_role?: string;
    group_id?: string;
    group_session_id?: string;
    group_agent_participant_ids?: string[];
    source_kind?: 'workbench' | 'group_message';
    source_group_id?: string;
    source_session_id?: string;
    source_message_id?: string;
    source_message_cursor?: string;
};

export type WorkTaskPreflight = {
    confirmation_fingerprint: string;
    capability_status: 'available' | 'degraded' | 'unavailable';
    estimated_credits?: number | null;
    cost_note: string;
    approval_required: boolean;
    reasons: string[];
    next_action?: string | null;
    executor_proposal: {
        policy_version: string;
        chosen_executor_kind: WorkExecutorKind;
        agent_id: string;
        agent_name: string;
        reason_codes: string[];
        confidence: number;
        candidates_considered: Array<Record<string, unknown>>;
        capability_snapshot: Record<string, unknown>;
        fallback?: Record<string, unknown> | null;
    };
    work_statement: {
        version: number;
        objective: string;
        title: string;
        work_type: WorkTaskDraft['work_type'];
        expected_output: string;
        delivery_mode: 'task_only';
        priority: WorkTaskDraft['priority'];
        executor: {
            kind: WorkExecutorKind;
            agent_id: string;
            agent_name: string;
            expert_role?: string | null;
            group_id?: string | null;
            group_name?: string | null;
            group_session_id?: string | null;
            group_session_title?: string | null;
            participants?: Array<{
                participant_id: string;
                agent_id: string;
                agent_name: string;
                responsibility: 'primary_owner' | 'collaborator';
            }>;
        };
        capability_preflight: {
            status: 'available' | 'degraded' | 'unavailable';
            scope: string;
            provider_selection: 'platform_managed';
        };
        cost: {
            estimated_credits?: number | null;
            basis: string;
            formal_media_requires_separate_preflight: boolean;
        };
        approval: {
            required_to_start: boolean;
            runtime_actions_checked_separately: boolean;
        };
        completion_criteria: string[];
    };
};

export type WorkIndex = {
    items: WorkItem[];
    personal_assistant_agent_id?: string | null;
    next_cursor?: string | null;
};

export type WorkStatusAxes = {
    execution: 'not_started' | 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled';
    artifact: 'missing' | 'candidate' | 'approved' | 'rejected' | 'superseded';
    quality: 'not_required' | 'open' | 'passed' | 'blocked' | 'incomplete' | 'superseded';
    runtime_approval: 'not_required' | 'pending' | 'approved' | 'rejected' | 'executing' | 'succeeded' | 'failed' | 'ambiguous';
    delivery_approval: 'not_required' | 'pending' | 'approved' | 'request_changes' | 'cancelled';
    delivery: 'not_requested' | 'pending' | 'reconciling' | 'delivered' | 'failed' | 'cancelled';
};

export type WorkNextAction = {
    id: string;
    task_id?: string | null;
    kind: 'quality_review' | 'runtime_approval' | 'delivery_approval' | 'task_recovery' | 'delivery_recovery';
    status: 'open';
    title: string;
    reason_code: string;
    source_type: string;
    source_id: string;
    action_url: string;
    created_at: string;
    due_at?: string | null;
    version?: string | null;
};

export type WorkTimelineEvent = {
    id: string;
    type: string;
    occurred_at: string;
    source_type: string;
    source_id: string;
    status?: string | null;
    title: string;
    summary?: string | null;
    actor_type?: string | null;
    actor_id?: string | null;
    metadata: Record<string, unknown>;
};

export type WorkTaskDetail = {
    detail_scope: 'full' | 'collaboration';
    summary: WorkItem;
    status_axes: WorkStatusAxes;
    timeline: WorkTimelineEvent[];
    next_actions: WorkNextAction[];
    runs: Array<{
        id: string;
        agent_id?: string | null;
        parent_run_id?: string | null;
        root_run_id?: string | null;
        run_kind: string;
        latest_event?: string | null;
        delivery_status: string;
        created_at: string;
        updated_at: string;
    }>;
    deliverables: Array<{
        id: string;
        agent_id: string;
        session_id: string;
        work_type: string;
        status: string;
        current_stage: string;
        current_execution_id?: string | null;
        version: number;
        created_at: string;
        updated_at: string;
    }>;
    artifacts: Array<WorkArtifact & {
        request_id: string;
        execution_id?: string | null;
        artifact_key: string;
        mime_type?: string | null;
        content_hash: string;
        created_at: string;
    }>;
    reviews: Array<{
        id: string;
        request_id: string;
        status: string;
        modality: string;
        minimum_reviewers: number;
        assigned_reviewer_count: number;
        current_user_assignment_status?: string | null;
        version: number;
        created_at: string;
        updated_at: string;
    }>;
    approvals: Array<{
        id: string;
        kind: 'runtime' | 'delivery';
        source_id: string;
        status: string;
        action_type: string;
        execution_status?: string | null;
        created_at: string;
        resolved_at?: string | null;
    }>;
    links: Record<string, string>;
};

export type PersonalSubscriptionUsage = {
    attribution_status: 'partial' | 'unavailable';
    attribution_note: string;
    consumed_credits: number;
    attributed_transactions: number;
    llm_calls_limit: number;
    message_limit: number;
    max_triggers: number;
};

export const subscriptionApi = {
    getMyUsage: () => request<PersonalSubscriptionUsage>('/subscription/usage/me'),
    getEntitlements: () => request<{
        plan_code?: string | null;
        max_agents: number;
        max_llm_calls_per_day: number;
        message_limit: number;
        max_triggers: number;
        subscription_status?: string | null;
        period_end?: string | null;
    } | null>('/subscription/my-entitlements'),
};

export const workApi = {
    list: (limit = 50) => request<WorkIndex>(`/work?limit=${limit}`),

    getTask: (taskId: string) => request<WorkItem>(`/work/tasks/${taskId}`),

    getTaskDetail: (taskId: string) => request<WorkTaskDetail>(`/work/tasks/${taskId}/detail`),

    getInbox: (params: { limit?: number; cursor?: string; kind?: WorkNextAction['kind'] } = {}) => {
        const query = new URLSearchParams();
        query.set('limit', String(params.limit || 50));
        if (params.cursor) query.set('cursor', params.cursor);
        if (params.kind) query.set('kind', params.kind);
        return request<{ items: WorkNextAction[]; next_cursor?: string | null }>(`/work/inbox?${query}`);
    },

    getInboxCount: () => request<{ count: number }>('/work/inbox/count'),

    preflightTask: (data: WorkTaskDraft) => request<WorkTaskPreflight>('/work/tasks/preflight', {
        method: 'POST',
        body: JSON.stringify(data),
    }),

    createTask: (data: WorkTaskDraft & {
        client_request_id: string;
        confirmation_fingerprint: string;
    }) => request<{ item: WorkItem; created: boolean }>('/work/tasks', {
        method: 'POST',
        body: JSON.stringify(data),
    }),

    retryTask: (taskId: string, clientRequestId: string) => request<{
        item: WorkItem;
        run_id: string;
        created: boolean;
    }>(`/work/tasks/${taskId}/retry`, {
        method: 'POST',
        body: JSON.stringify({ client_request_id: clientRequestId }),
    }),
};

// ─── Tasks ────────────────────────────────────────────
export const taskApi = {
    list: (agentId: string, status?: string, type?: string) => {
        const params = new URLSearchParams();
        if (status) params.set('status_filter', status);
        if (type) params.set('type_filter', type);
        return request<Task[]>(`/agents/${agentId}/tasks/?${params}`);
    },

    create: (agentId: string, data: any) =>
        request<Task>(`/agents/${agentId}/tasks/`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, taskId: string, data: Partial<Task>) =>
        request<Task>(`/agents/${agentId}/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify(data) }),

    getLogs: (agentId: string, taskId: string) =>
        request<{ id: string; task_id: string; content: string; created_at: string }[]>(`/agents/${agentId}/tasks/${taskId}/logs`),

    trigger: (agentId: string, taskId: string) =>
        request<any>(`/agents/${agentId}/tasks/${taskId}/trigger`, { method: 'POST' }),
};

// ─── Files ────────────────────────────────────────────
export const fileApi = {
    list: (agentId: string, path: string = '') =>
        request<any[]>(`/agents/${agentId}/files/?path=${encodeURIComponent(path)}`),

    read: (agentId: string, path: string) =>
        request<{ path: string; content: string }>(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`),

    write: (agentId: string, path: string, content: string) =>
        request(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            body: JSON.stringify({ content }),
        }),

    autosave: (agentId: string, path: string, content: string, sessionId?: string | null) =>
        request<{ status: string; path: string; revision_id?: string }>(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            body: JSON.stringify({ content, autosave: true, session_id: sessionId || undefined }),
        }),

    delete: (agentId: string, path: string) =>
        request(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, {
            method: 'DELETE',
        }),

    preview: (agentId: string, path: string) =>
        request<any>(`/agents/${agentId}/files/preview?path=${encodeURIComponent(path)}`),

    lock: (agentId: string, path: string, sessionId?: string | null) =>
        request<any>(`/agents/${agentId}/files/locks`, {
            method: 'POST',
            body: JSON.stringify({ path, session_id: sessionId || undefined }),
        }),

    unlock: (agentId: string, path: string) =>
        request<any>(`/agents/${agentId}/files/locks?path=${encodeURIComponent(path)}`, {
            method: 'DELETE',
        }),

    revisions: (agentId: string, path: string) =>
        request<any[]>(`/agents/${agentId}/files/revisions?path=${encodeURIComponent(path)}`),

    restoreRevision: (agentId: string, revisionId: string) =>
        request<any>(`/agents/${agentId}/files/restore`, {
            method: 'POST',
            body: JSON.stringify({ revision_id: revisionId }),
        }),

    upload: (agentId: string, file: File, path: string = 'workspace/knowledge_base', onProgress?: (pct: number) => void) =>
        onProgress
            ? uploadFileWithProgress(`/agents/${agentId}/files/upload?path=${encodeURIComponent(path)}`, file, onProgress).promise
            : uploadFile(`/agents/${agentId}/files/upload?path=${encodeURIComponent(path)}`, file),

    importSkill: (agentId: string, skillId: string) =>
        request<any>(`/agents/${agentId}/files/import-skill`, {
            method: 'POST',
            body: JSON.stringify({ skill_id: skillId }),
        }),

    downloadUrl: (agentId: string, path: string, options?: { inline?: boolean }) => {
        return buildWorkspaceDownloadUrl(agentId, path, options);
    },
};

export type FocusApiItem = {
    id: string;
    agent_id: string;
    key: string;
    title?: string | null;
    description: string;
    status: 'in_progress' | 'completed';
    kind: 'normal' | 'system';
    source: string;
    metadata?: Record<string, any>;
    sort_order: number;
    completed_at?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
};

// ─── Focus ───────────────────────────────────────────
export const focusApi = {
    list: (agentId: string, includeCompleted = true) =>
        request<FocusApiItem[]>(`/agents/${agentId}/focus/?include_completed=${includeCompleted ? 'true' : 'false'}`),

    upsert: (agentId: string, data: { key?: string; title?: string | null; description: string; status?: string; kind?: string; source?: string; metadata?: Record<string, any> }) =>
        request<FocusApiItem>(`/agents/${agentId}/focus/`, { method: 'POST', body: JSON.stringify(data) }),

    complete: (agentId: string, key: string) =>
        request<FocusApiItem>(`/agents/${agentId}/focus/${encodeURIComponent(key)}/complete`, { method: 'POST' }),
};

// ─── Channel Config ───────────────────────────────────
export const channelApi = {
    get: (agentId: string) =>
        request<any>(`/agents/${agentId}/channel?missing_ok=true`),

    create: (agentId: string, data: any) =>
        request<any>(`/agents/${agentId}/channel`, { method: 'POST', body: JSON.stringify(data) }),

    configure: (agentId: string, endpoint: AgentChannelEndpoint, data: any) =>
        request<any>(`/agents/${agentId}/${endpoint}`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, data: any) =>
        request<any>(`/agents/${agentId}/channel`, { method: 'PUT', body: JSON.stringify(data) }),

    delete: (agentId: string) =>
        request<void>(`/agents/${agentId}/channel`, { method: 'DELETE' }),

    webhookUrl: (agentId: string) =>
        request<{ webhook_url: string }>(`/agents/${agentId}/channel/webhook-url`).catch(() => null),
};

// ─── Enterprise ───────────────────────────────────────
export const enterpriseApi = {
    llmModels: () => {
        const tid = localStorage.getItem('current_tenant_id');
        return request<any[]>(`/enterprise/llm-models${tid ? `?tenant_id=${tid}` : ''}`);
    },

    platformLlmModels: () =>
        request<any[]>('/enterprise/llm-models?platform_only=true'),

    setDefaultModel: (modelId: string) =>
        request<void>(`/enterprise/llm-models/${modelId}/set-default`, { method: 'POST' }),
    templates: () => request<any[]>('/agents/templates'),

    // Enterprise Knowledge Base
    kbFiles: (path: string = '') =>
        request<any[]>(`/enterprise/knowledge-base/files?path=${encodeURIComponent(path)}`),

    kbUpload: (file: File, subPath: string = '') =>
        uploadFile(`/enterprise/knowledge-base/upload?sub_path=${encodeURIComponent(subPath)}`, file),

    kbRead: (path: string) =>
        request<{ path: string; content: string }>(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`),

    kbWrite: (path: string, content: string) =>
        request(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            body: JSON.stringify({ content }),
        }),

    kbDelete: (path: string) =>
        request(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`, {
            method: 'DELETE',
        }),
};

// ─── Activity Logs ────────────────────────────────────
export const activityApi = {
    list: (agentId: string, limit = 50) =>
        request<any[]>(`/agents/${agentId}/activity?limit=${limit}`),
};

// ─── Workforce Topology ──────────────────────────────
export type WorkforceTopologyWork = {
    id: string;
    title: string;
    summary: string;
    stage: 'executing' | 'review' | 'approval' | 'blocked' | 'completed';
    active_count: number;
    recently_completed_count: number;
    deep_link: string;
    updated_at: string;
};

export type WorkforceTopologyExecution = {
    id: string;
    run_id?: string | null;
    source_type: 'direct_chat' | 'group' | 'a2a' | 'task' | 'trigger' | 'heartbeat' | 'deliverable' | 'media';
    status: 'queued' | 'running' | 'waiting_user' | 'waiting_agent' | 'waiting_external' | 'completed' | 'failed' | 'cancelled';
    phase?: string | null;
    title: string;
    summary: string;
    details_visible: boolean;
    active_count: number;
    recently_finished_count: number;
    deep_link: string;
    updated_at: string;
};

export type WorkforceTopologyNode = {
    id: string;
    name: string;
    avatar_url?: string | null;
    role_description: string;
    status: 'creating' | 'running' | 'idle' | 'stopped' | 'error' | string;
    last_active_at?: string | null;
    tokens_used_today: number | null;
    cache_read_tokens_today: number | null;
    max_tokens_per_day?: number | null;
    is_expired: boolean;
    is_system: boolean;
    visibility: 'company' | 'private' | 'custom';
    can_manage: boolean;
    execution?: WorkforceTopologyExecution | null;
    work?: WorkforceTopologyWork | null;
};

export type WorkforceTopologyRelationshipEdge = {
    id: string;
    source_agent_id: string;
    target_agent_id: string;
    relation: string;
    updated_at?: string | null;
};

export type WorkforceTopologyActivityEdge = {
    agent_a_id: string;
    agent_b_id: string;
    interaction_count: number;
    last_activity_at: string;
};

export type WorkforceTopologyActivity = {
    id: string;
    agent_id: string;
    summary: string;
    created_at: string;
};

export type WorkforceTopology = {
    company_id: string;
    company_name: string;
    window_hours: number;
    generated_at: string;
    scope_contract: {
        execution: 'company_visible_redacted';
        work: 'viewer_owned';
        analytics: 'governor_or_managed';
    };
    nodes: WorkforceTopologyNode[];
    relationship_edges: WorkforceTopologyRelationshipEdge[];
    activity_edges: WorkforceTopologyActivityEdge[];
    recent_activities: WorkforceTopologyActivity[];
};

export const workforceApi = {
    topology: (windowHours = 24) =>
        request<WorkforceTopology>(`/workforce/topology?window_hours=${windowHours}`),
};

// ─── Company CEO (P1 observer + opt-in P2 coordinator) ─────────────
export type CeoOrchestratorSettings = {
    feature_available: boolean;
    coordination_feature_available: boolean;
    configured: boolean;
    ceo_agent_id: string | null;
    enabled: boolean;
    enabled_by_user_id: string | null;
    enabled_at: string | null;
    briefing_enabled: boolean;
    morning_meeting_enabled: boolean;
    meeting_group_id: string | null;
    daily_credit_cap: number;
    monthly_credit_cap: number;
    meeting_member_agent_ids: string[];
    coordination_enabled: boolean;
    auto_dispatch_enabled: boolean;
    coordination_enabled_by_user_id: string | null;
    coordination_enabled_at: string | null;
    max_parallel_delegations: number;
    operating_mode: 'disabled' | 'observer' | 'coordinator' | 'coordinator_auto';
};

export type CeoOrchestratorStatus = {
    feature_available: boolean;
    configured: boolean;
    ceo_agent_id: string | null;
    enabled: boolean;
};

export type CeoCompanyBrief = {
    snapshot: {
        company_name: string;
        window_hours: number;
        generated_at: string;
        employee_total: number;
        employee_active_in_window: number;
        work_executing: number;
        work_review: number;
        work_approval: number;
        work_blocked: number;
        work_completed_recent: number;
        blocked_items: { agent_name: string; title: string; stage: string }[];
        in_progress_items: { agent_name: string; title: string; stage: string }[];
        okr_tracked_members: number;
        okr_reports_today_submitted: number;
        okr_reports_today_missing: number;
        truncated: boolean;
    };
    markdown: string;
};

export const ceoApi = {
    status: () => request<CeoOrchestratorStatus>('/companies/current/ceo/status'),

    settings: () => request<CeoOrchestratorSettings>('/companies/current/ceo/settings'),

    enable: (data: {
        member_agent_ids?: string[];
        briefing_enabled?: boolean;
        morning_meeting_enabled?: boolean;
        daily_credit_cap?: number;
        monthly_credit_cap?: number;
    }) => request<CeoOrchestratorSettings>('/companies/current/ceo/enable', {
        method: 'POST',
        body: JSON.stringify(data),
    }),

    disable: () => request<CeoOrchestratorSettings>('/companies/current/ceo/disable', {
        method: 'POST',
    }),

    updateSettings: (data: Partial<{
        briefing_enabled: boolean;
        morning_meeting_enabled: boolean;
        daily_credit_cap: number;
        monthly_credit_cap: number;
        member_agent_ids: string[];
        coordination_enabled: boolean;
        auto_dispatch_enabled: boolean;
        max_parallel_delegations: number;
    }>) => request<CeoOrchestratorSettings>('/companies/current/ceo/settings', {
        method: 'PATCH',
        body: JSON.stringify(data),
    }),

    companyBrief: (agentId: string, windowHours = 168) =>
        request<CeoCompanyBrief>(`/agents/${agentId}/company-brief?window_hours=${windowHours}`),

    startMeeting: (agentId: string, kind: 'morning' | 'weekly') =>
        request<{
            trigger_execution_id: string;
            status: string;
            meeting_group_id: string | null;
            kind: string;
        }>(`/agents/${agentId}/meetings/${kind}/start`, { method: 'POST' }),
};

// ─── Deliverable Workbench ───────────────────────────
export type DeliverableWorkType = 'presentation' | 'poster' | 'video' | 'report' | 'spreadsheet';

export interface DeliverableWorkflowField {
    key: string;
    label_zh: string;
    label_en: string;
    kind: 'text' | 'textarea' | 'number' | 'select' | 'json';
    required: boolean;
    default: string | number | null;
    minimum: number | null;
    maximum: number | null;
    options: string[];
    placeholder_zh: string;
    placeholder_en: string;
}

export interface DeliverableWorkflow {
    workflow_id: string;
    workflow_version: string;
    work_type: DeliverableWorkType;
    label_zh: string;
    label_en: string;
    description_zh: string;
    description_en: string;
    fields: DeliverableWorkflowField[];
    approval_policy: string[];
    output_contract: string[];
    required_capability: 'presentation' | 'image' | 'video' | 'document';
    launch_policy: 'agent_runtime' | 'dry_run';
}

export interface DeliverableCreditEstimate {
    mode: 'estimate' | 'usage_based';
    minimum: number | null;
    maximum: number | null;
    billing_unit: string;
    candidates?: number;
    per_candidate_credits?: number;
}

export interface DeliverableCreativeBriefSummary {
    schema_version: string;
    status: 'draft' | 'clarifying' | 'confirmed';
    missing_fields: string[];
    brief_sha256?: string | null;
    candidate_count?: number | null;
}

export interface DeliverableBrief extends DeliverableCreativeBriefSummary {
    brief: Record<string, unknown> | null;
    updated_at: string | null;
}

export interface CandidateQaSummary {
    schema_version: string | null;
    status: string | null;
    score: number | null;
    artifact_sha256: string | null;
    checks: Array<{ name: string; status: string }>;
    subject_similarity: Record<string, unknown>;
}

export interface DeliverablePreflight {
    workflow_id: string;
    workflow_version: string;
    available: boolean;
    launchable: boolean;
    reasons: string[];
    capability_status: 'available' | 'degraded' | 'unavailable';
    next_action: string;
    tier: 'lite' | 'pro' | 'ultra';
    normalized_spec: Record<string, string | number>;
    credit_estimate: DeliverableCreditEstimate;
    creates_reservation: false;
    creative_brief?: DeliverableCreativeBriefSummary;
}

export interface DeliverableArtifactRevision {
    id: string;
    request_id: string;
    parent_revision_id: string | null;
    execution_id?: string | null;
    unit_id?: string | null;
    artifact_key: string;
    artifact_type: string;
    stage_key?: string | null;
    unit_key?: string | null;
    workspace_path: string;
    mime_type: string | null;
    content_hash: string;
    size_bytes: number | null;
    revision_number: number;
    status: string;
    evaluation: Record<string, unknown>;
    approved_by_user_id: string | null;
    approved_at: string | null;
    created_at: string;
}

export interface DeliverableExecutionUnit {
    id: string;
    execution_id: string;
    stage_key: string;
    unit_key: string;
    status: 'pending' | 'running' | 'blocked' | 'reconciling' | 'succeeded' | 'failed' | 'cancelled' | 'superseded';
    dependency_hash: string;
    attempt_count: number;
    input_snapshot: Record<string, unknown>;
    result_snapshot: Record<string, unknown>;
    quality_evaluation: Record<string, unknown>;
    qa_summary?: CandidateQaSummary | null;
    last_error_code: string | null;
    next_retry_at: string | null;
    started_at: string | null;
    completed_at: string | null;
    created_at: string;
    updated_at: string;
}

export interface DeliverableApprovalReceipt {
    id: string;
    execution_id: string;
    actor_user_id: string;
    client_action_id: string;
    request_version: number;
    stage: 'brief' | 'outline' | 'composition' | 'storyboard' | 'final';
    action: 'approve' | 'request_changes' | 'cancel';
    instruction: string | null;
    target_units: string[];
    receipt: Record<string, unknown>;
    created_at: string;
}

export interface DeliverableSelectionReceipt {
    id: string;
    execution_id: string;
    selected_unit_key: string;
    candidate_scores: Array<Record<string, unknown>>;
    selection_reason: string;
    cost_breakdown: Record<string, unknown>;
    actor: 'auto' | 'user';
    actor_user_id: string | null;
    client_selection_id: string;
    created_at: string;
}

export interface DeliverableExecution {
    id: string;
    request_id: string;
    execution_number: number;
    kind: 'initial' | 'revision' | 'recovery';
    status: 'ready' | 'running' | 'blocked' | 'reconciling' | 'waiting_approval' | 'succeeded' | 'failed' | 'cancelled';
    current_stage: string;
    workflow_id: string;
    workflow_version: string;
    contract_snapshot: Record<string, unknown>;
    preflight_snapshot: Record<string, unknown>;
    revision_instruction: string | null;
    blocked_reason: string | null;
    last_error_code: string | null;
    launched_at: string | null;
    completed_at: string | null;
    created_at: string;
    updated_at: string;
    units: DeliverableExecutionUnit[];
    approvals: DeliverableApprovalReceipt[];
    selections?: DeliverableSelectionReceipt[];
}

export interface DeliverableApprovalReadiness {
    approvable: boolean;
    quality_gate_required: boolean;
    quality_status: 'not_required' | 'pending' | 'passed' | 'blocked' | 'incomplete' | 'invalid';
    blockers: string[];
    receipt_ref: string | null;
}

export interface DeliverableRequest {
    id: string;
    tenant_id: string;
    created_by_user_id: string;
    agent_id: string;
    session_id: string;
    agent_run_id: string | null;
    current_execution_id?: string | null;
    task_id?: string | null;
    client_request_id: string;
    work_type: DeliverableWorkType;
    workflow_id: string;
    workflow_version: string;
    goal: string;
    inputs: Array<{ type: 'workspace_file'; path: string; name?: string }>;
    spec: Record<string, string | number>;
    tier: 'lite' | 'pro' | 'ultra';
    approval_policy: string[];
    output_contract: string[];
    status: 'draft' | 'ready' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'cancelled';
    current_stage: string;
    version: number;
    contract_revision?: number;
    latest_preflight?: Record<string, unknown> | null;
    last_error_code: string | null;
    launched_at: string | null;
    completed_at: string | null;
    created_at: string;
    updated_at: string;
    artifacts: DeliverableArtifactRevision[];
    // Optional during rolling frontend/backend deployments. New backends
    // always return it; old backends keep the legacy approval behavior.
    approval_readiness?: DeliverableApprovalReadiness;
}

export interface DeliverableQualityReviewer {
    user_id: string;
    display_name: string;
    role: string;
    eligible: boolean;
    ineligible_reason: string | null;
}

export interface DeliverableQualityReviewAssignment {
    reviewer_user_id: string;
    reviewer_display_name: string | null;
    reviewer_role: string | null;
    status: 'assigned' | 'submitted';
    is_current_user: boolean;
    submitted_at: string | null;
}

export interface DeliverableQualityReview {
    id: string;
    request_id: string;
    modality: 'image' | 'video' | 'presentation';
    status: 'open' | 'passed' | 'blocked' | 'incomplete' | 'superseded';
    version: number;
    minimum_reviewers: number;
    assigned_reviewer_count: number;
    submitted_reviewer_count: number;
    artifact_hashes: Record<string, string>;
    brief: string;
    requirements: string[];
    hard_gates: string[];
    quality_dimensions: string[];
    required_evidence_kinds: string[];
    automated_evidence: Array<{
        kind: string;
        status: 'complete' | 'partial' | 'unavailable';
        source_ref: string | null;
        findings: string[];
    }>;
    assignments: DeliverableQualityReviewAssignment[];
    artifacts: Array<{
        id: string;
        artifact_key: string;
        artifact_type: string;
        content_hash: string;
        revision_number: number;
        download_url: string;
    }>;
    current_user_can_manage: boolean;
    current_user_can_submit: boolean;
    current_user_can_add_evidence: boolean;
    receipt_ref: string | null;
    created_at: string;
    sealed_at: string | null;
}

export const deliverableApi = {
    workflows: (agentId: string, tier: 'lite' | 'pro' | 'ultra') => request<{ workflows: DeliverableWorkflow[] }>(
        `/deliverables/workflows?agent_id=${encodeURIComponent(agentId)}&tier=${encodeURIComponent(tier)}`,
    ),
    preflight: (data: {
        agent_id: string;
        work_type: DeliverableWorkType;
        workflow_id: string;
        workflow_version: string;
        goal?: string;
        inputs?: Array<{ type: 'workspace_file'; path: string; name?: string }>;
        spec: Record<string, string | number>;
        tier: 'lite' | 'pro' | 'ultra';
    }) => request<DeliverablePreflight>('/deliverables/preflight', {
        method: 'POST',
        body: JSON.stringify(data),
    }),
    create: (data: {
        client_request_id: string;
        agent_id: string;
        session_id: string;
        task_id?: string;
        work_type: DeliverableWorkType;
        workflow_id: string;
        workflow_version: string;
        goal: string;
        inputs: Array<{ type: 'workspace_file'; path: string; name?: string }>;
        spec: Record<string, string | number>;
        tier: 'lite' | 'pro' | 'ultra';
        approval_policy: string[];
        output_contract: string[];
    }) => request<DeliverableRequest>('/deliverables/requests', {
        method: 'POST',
        body: JSON.stringify(data),
    }),
    list: (agentId: string, sessionId?: string) => {
        const query = new URLSearchParams({ agent_id: agentId });
        if (sessionId) query.set('session_id', sessionId);
        return request<DeliverableRequest[]>(`/deliverables/requests?${query.toString()}`);
    },
    get: (requestId: string) => request<DeliverableRequest>(`/deliverables/requests/${requestId}`),
    executions: (requestId: string) => request<DeliverableExecution[]>(
        `/deliverables/requests/${requestId}/executions`,
    ),
    brief: (requestId: string) => request<DeliverableBrief>(
        `/deliverables/requests/${requestId}/brief`,
    ),
    clarify: (requestId: string, data: { expected_version: number; answers: Record<string, string | number> }) =>
        request<DeliverableBrief>(`/deliverables/requests/${requestId}/clarifications`, {
            method: 'POST',
            body: JSON.stringify(data),
        }),
    artifactDownloadUrl: (artifactId: string, options?: { inline?: boolean }) => {
        const query = new URLSearchParams();
        if (options?.inline) query.set('inline', 'true');
        const suffix = query.toString();
        return `/api/deliverables/artifacts/${encodeURIComponent(artifactId)}/download${suffix ? `?${suffix}` : ''}`;
    },
    action: (requestId: string, action: 'submit' | 'approve' | 'request_changes' | 'cancel', expectedVersion: number) =>
        request<DeliverableRequest>(`/deliverables/requests/${requestId}/actions`, {
            method: 'POST',
            body: JSON.stringify({ action, expected_version: expectedVersion }),
        }),
    approval: (
        requestId: string,
        data: {
            expected_version: number;
            client_action_id: string;
            stage: 'brief' | 'outline' | 'composition' | 'storyboard' | 'final';
            action: 'approve' | 'request_changes' | 'cancel';
            instruction?: string;
            target_units?: string[];
        },
    ) => request<DeliverableRequest>(`/deliverables/requests/${requestId}/approvals`, {
        method: 'POST',
        body: JSON.stringify(data),
    }),
    qualityReviewers: (requestId: string) =>
        request<DeliverableQualityReviewer[]>(
            `/deliverables/requests/${requestId}/quality-reviewers`,
        ),
    latestQualityReview: (requestId: string) =>
        request<DeliverableQualityReview | null>(
            `/deliverables/requests/${requestId}/quality-reviews/latest`,
        ),
    createQualityReview: (
        requestId: string,
        data: {
            client_review_id: string;
            expected_request_version: number;
            reviewer_user_ids: string[];
        },
    ) => request<DeliverableQualityReview>(
        `/deliverables/requests/${requestId}/quality-reviews`,
        {
            method: 'POST',
            body: JSON.stringify(data),
        },
    ),
    qualityReview: (reviewId: string) =>
        request<DeliverableQualityReview>(
            `/deliverables/quality-reviews/${reviewId}`,
        ),
    submitQualityReview: (
        reviewId: string,
        data: {
            client_submission_id: string;
            expected_version: number;
            hard_gates: Record<string, { passed: boolean; evidence: string[] }>;
            dimensions: Record<string, { score: number; evidence: string[] }>;
            human_evidence: Record<string, {
                status: 'complete' | 'partial' | 'unavailable';
                findings: string[];
            }>;
            notes: string[];
        },
    ) => request<DeliverableQualityReview>(
        `/deliverables/quality-reviews/${reviewId}/submissions`,
        {
            method: 'POST',
            body: JSON.stringify(data),
        },
    ),
    addQualityReviewEvidence: (
        reviewId: string,
        data: {
            client_evidence_id: string;
            expected_version: number;
            kind: 'ocr' | 'frame_ocr';
            status: 'complete' | 'partial' | 'unavailable';
            source_ref: string;
            findings: string[];
        },
    ) => request<DeliverableQualityReview>(
        `/deliverables/quality-reviews/${reviewId}/evidence`,
        {
            method: 'POST',
            body: JSON.stringify(data),
        },
    ),
};

// ─── Messages ─────────────────────────────────────────
export const messageApi = {
    inbox: (limit = 50) =>
        request<any[]>(`/messages/inbox?limit=${limit}`),

    unreadCount: () =>
        request<{ unread_count: number }>('/messages/unread-count'),

    markRead: (messageId: string) =>
        request<void>(`/messages/${messageId}/read`, { method: 'PUT' }),

    markAllRead: () =>
        request<void>('/messages/read-all', { method: 'PUT' }),
};

// ─── Schedules ────────────────────────────────────────
export const scheduleApi = {
    list: (agentId: string) =>
        request<any[]>(`/agents/${agentId}/schedules/`),

    create: (agentId: string, data: { name: string; instruction: string; cron_expr: string }) =>
        request<any>(`/agents/${agentId}/schedules/`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, scheduleId: string, data: any) =>
        request<any>(`/agents/${agentId}/schedules/${scheduleId}`, { method: 'PATCH', body: JSON.stringify(data) }),

    delete: (agentId: string, scheduleId: string) =>
        request<void>(`/agents/${agentId}/schedules/${scheduleId}`, { method: 'DELETE' }),

    trigger: (agentId: string, scheduleId: string) =>
        request<any>(`/agents/${agentId}/schedules/${scheduleId}/run`, { method: 'POST' }),

    history: (agentId: string, scheduleId: string) =>
        request<any[]>(`/agents/${agentId}/schedules/${scheduleId}/history`),
};

// ─── Skills ───────────────────────────────────────────
export const skillApi = {
    list: () => request<any[]>('/skills/'),
    get: (id: string) => request<any>(`/skills/${id}`),
    create: (data: any) =>
        request<any>('/skills/', { method: 'POST', body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
        request<any>(`/skills/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    delete: (id: string) =>
        request<void>(`/skills/${id}`, { method: 'DELETE' }),
    // Path-based browse for FileBrowser
    browse: {
        list: (path: string) => request<any[]>(`/skills/browse/list?path=${encodeURIComponent(path)}`),
        read: (path: string) => request<{ content: string }>(`/skills/browse/read?path=${encodeURIComponent(path)}`),
        write: (path: string, content: string) =>
            request<any>('/skills/browse/write', { method: 'PUT', body: JSON.stringify({ path, content }) }),
        delete: (path: string) =>
            request<any>(`/skills/browse/delete?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),
    },
    // ClawHub marketplace integration
    clawhub: {
        search: (q: string) => request<any[]>(`/skills/clawhub/search?q=${encodeURIComponent(q)}`),
        detail: (slug: string) => request<any>(`/skills/clawhub/detail/${slug}`),
        install: (slug: string) => request<any>('/skills/clawhub/install', { method: 'POST', body: JSON.stringify({ slug }) }),
    },
    importFromUrl: (url: string) =>
        request<any>('/skills/import-from-url', { method: 'POST', body: JSON.stringify({ url }) }),
    previewUrl: (url: string) =>
        request<any>('/skills/import-from-url/preview', { method: 'POST', body: JSON.stringify({ url }) }),
    // Tenant-level settings
    settings: {
        getToken: () => request<{ configured: boolean; source: string; masked: string; clawhub_configured: boolean; clawhub_masked: string }>('/skills/settings/token'),
        setToken: (github_token: string) =>
            request<any>('/skills/settings/token', { method: 'PUT', body: JSON.stringify({ github_token }) }),
        setClawhubKey: (clawhub_key: string) =>
            request<any>('/skills/settings/token', { method: 'PUT', body: JSON.stringify({ clawhub_key }) }),
    },
    // Agent-level import (writes to agent workspace)
    agentImport: {
        fromClawhub: (agentId: string, slug: string) =>
            request<any>(`/agents/${agentId}/files/import-from-clawhub`, { method: 'POST', body: JSON.stringify({ slug }) }),
        fromUrl: (agentId: string, url: string) =>
            request<any>(`/agents/${agentId}/files/import-from-url`, { method: 'POST', body: JSON.stringify({ url }) }),
    },
};

// ─── Triggers (Aware Engine) ──────────────────────────
export const triggerApi = {
    list: (agentId: string) =>
        request<any[]>(`/agents/${agentId}/triggers`),

    update: (agentId: string, triggerId: string, data: any) =>
        request<any>(`/agents/${agentId}/triggers/${triggerId}`, { method: 'PATCH', body: JSON.stringify(data) }),

    delete: (agentId: string, triggerId: string) =>
        request<void>(`/agents/${agentId}/triggers/${triggerId}`, { method: 'DELETE' }),
};

// ─── Agent Credentials ────────────────────────────────
export const credentialApi = {
    list: (agentId: string) =>
        request<any[]>(`/agents/${agentId}/credentials/`),

    create: (agentId: string, data: any) =>
        request<any>(`/agents/${agentId}/credentials/`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, credentialId: string, data: any) =>
        request<any>(`/agents/${agentId}/credentials/${credentialId}`, { method: 'PUT', body: JSON.stringify(data) }),

    delete: (agentId: string, credentialId: string) =>
        request<void>(`/agents/${agentId}/credentials/${credentialId}`, { method: 'DELETE' }),
};

// ─── AgentBay Take Control ────────────────────────────
export const controlApi = {
    click: (agentId: string, data: { session_id: string; x: number; y: number; button?: string }) =>
        request<any>(`/agents/${agentId}/control/click`, { method: 'POST', body: JSON.stringify(data) }),

    type: (agentId: string, data: { session_id: string; text: string }) =>
        request<any>(`/agents/${agentId}/control/type`, { method: 'POST', body: JSON.stringify(data) }),

    pressKeys: (agentId: string, data: { session_id: string; keys: string[] }) =>
        request<any>(`/agents/${agentId}/control/press_keys`, { method: 'POST', body: JSON.stringify(data) }),

    /** Simulate a natural human drag (Bezier curve trajectory) for slider CAPTCHAs. */
    drag: (agentId: string, data: { session_id: string; from_x: number; from_y: number; to_x: number; to_y: number; duration_ms?: number }) =>
        request<any>(`/agents/${agentId}/control/drag`, { method: 'POST', body: JSON.stringify(data) }),

    screenshot: (agentId: string, data: { session_id: string }) =>
        request<any>(`/agents/${agentId}/control/screenshot`, { method: 'POST', body: JSON.stringify(data) }),

    lock: (agentId: string, data: { session_id: string; platform_hint?: string; env_type?: string }) =>
        request<any>(`/agents/${agentId}/control/lock`, { method: 'POST', body: JSON.stringify(data) }),

    unlock: (agentId: string, data: { session_id: string; export_cookies?: boolean; platform_hint?: string }) =>
        request<any>(`/agents/${agentId}/control/unlock`, { method: 'POST', body: JSON.stringify(data) }),
};

// ─── Experience Library ───────────────────────────────
export interface ExperienceEntry {
    id: string;
    // Set only for an edit draft derived from a published/retired source entry.
    draft_of_id: string | null;
    tenant_id: string | null;
    title: string;
    body: string;           // 正文 — free-form markdown
    applicability: string;  // 适用条件与失效信号 — the agent's read-or-skip preview; required to publish
    status: 'draft' | 'published' | 'retired';
    tags: string[];
    // Legacy response fields; published Experience is tenant-wide.
    visibility_scope: 'company' | 'department' | 'user';
    visibility_scope_id: string | null;
    origin: 'chat' | 'legacy_plaza';
    origin_session_id: string | null;
    origin_agent_id: string | null;
    source_task_id: string | null;
    source_deliverable_request_id: string | null;
    created_by: string;
    reviewed_by: string | null;
    last_reviewed_at: string | null;
    retired_at: string | null;
    created_at: string;
    updated_at: string | null;
    created_by_name?: string | null;
    origin_agent_name?: string | null;
    // Whether the caller may edit/review/retire/re-publish (single-entry fetch only; null in lists).
    can_manage?: boolean | null;
}

export type ExperienceView = 'team' | 'mine' | 'all';

export const experienceApi = {
    list: (params: { view?: ExperienceView; status?: string; tag?: string; q?: string } = {}) => {
        const qs = new URLSearchParams();
        if (params.view) qs.set('view', params.view);
        if (params.status) qs.set('status', params.status);
        if (params.tag) qs.set('tag', params.tag);
        if (params.q) qs.set('q', params.q);
        const s = qs.toString();
        return request<ExperienceEntry[]>(`/experience/entries${s ? `?${s}` : ''}`);
    },
    get: (id: string) => request<ExperienceEntry>(`/experience/entries/${id}`),
    createDraftFromContent: (data: { agent_id: string; content: string; session_id?: string }) =>
        request<ExperienceEntry>('/experience/drafts', { method: 'POST', body: JSON.stringify(data) }),
    // Distill chat content into title / body / applicability WITHOUT persisting (human confirms in the editor).
    distill: (data: { agent_id: string; content: string; session_id?: string }) =>
        request<{ title: string; body: string; applicability: string; tags: string[]; extracted: boolean }>(
            '/experience/distill', { method: 'POST', body: JSON.stringify(data) }),
    create: (data: Partial<ExperienceEntry>) =>
        request<ExperienceEntry>('/experience/entries', { method: 'POST', body: JSON.stringify(data) }),
    createRevision: (id: string, data: Partial<ExperienceEntry>) =>
        request<ExperienceEntry>(`/experience/entries/${id}/draft`, { method: 'POST', body: JSON.stringify(data) }),
    update: (id: string, data: Partial<ExperienceEntry>) =>
        request<ExperienceEntry>(`/experience/entries/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
    publish: (id: string) =>
        request<ExperienceEntry>(`/experience/entries/${id}/publish`, { method: 'POST' }),
    retire: (id: string) =>
        request<ExperienceEntry>(`/experience/entries/${id}/retire`, { method: 'POST' }),
    remove: (id: string) =>
        request<{ deleted: boolean }>(`/experience/entries/${id}`, { method: 'DELETE' }),
    review: (id: string) =>
        request<ExperienceEntry>(`/experience/entries/${id}/review`, { method: 'POST' }),
    references: (id: string) =>
        request<{ entry_id: string; read_count: number; cited_count: number }>(`/experience/entries/${id}/references`),
    stats: () =>
        request<{ total: number; today: number; cited: number; top_contributors: { name: string; count: number }[] }>('/experience/stats'),
};

// ─── Org structure (synced from Feishu/DingTalk/WeCom; empty until org sync runs) ───
export interface OrgDepartmentItem {
    id: string;
    name: string;
    path?: string;
    parent_id?: string | null;
    member_count?: number;
}

export const orgApi = {
    departments: () =>
        request<{ items: OrgDepartmentItem[]; total_member: number }>('/enterprise/org/departments'),
};
