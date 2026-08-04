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
            || url.startsWith('/auth/register')
            || url.startsWith('/auth/verify-email')
            || url.startsWith('/auth/resend-verification')
            || url.startsWith('/auth/forgot-password')
            || url.startsWith('/auth/reset-password');
        if (res.status === 401 && !isAuthEndpoint) {
            clearAuthStorage();
            window.location.href = '/login';
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
export const authApi = {
    registrationConfig: () =>
        request<{ invitation_code_required: boolean }>('/auth/registration-config'),

    register: (data: { username?: string; email: string; password: string; display_name: string; invitation_code?: string; provider?: string; provider_code?: string }) =>
        request<{ user_id: string; email: string; access_token: string; message: string; user?: any; needs_company_setup: boolean }>('/auth/register', { method: 'POST', body: JSON.stringify(data) }),

    login: (data: { login_identifier: string; password: string; tenant_id?: string }) =>
        request<TokenResponse | { requires_tenant_selection: boolean; login_identifier: string; tenants: any[] }>('/auth/login', { method: 'POST', body: JSON.stringify(data) }),

    forgotPassword: (data: { email: string }) =>
        request<{ ok: boolean; message: string }>('/auth/forgot-password', { method: 'POST', body: JSON.stringify(data) }),

    resetPassword: (data: { token: string; new_password: string }) =>
        request<{ ok: boolean }>('/auth/reset-password', { method: 'POST', body: JSON.stringify(data) }),

    emailHint: (username: string) =>
        request<{ hint: string }>(`/auth/email-hint?username=${encodeURIComponent(username)}`),

    me: () => request<User>('/auth/me'),

    updateMe: (data: Partial<User> & { current_password?: string }) =>
        request<User>('/auth/me', { method: 'PATCH', body: JSON.stringify(data) }),

    verifyEmail: (token: string) =>
        request<{ ok: boolean; message: string; access_token: string; user: User; needs_company_setup: boolean }>('/auth/verify-email', { method: 'POST', body: JSON.stringify({ token }) }),

    resendVerification: (email: string) =>
        request<{ ok: boolean; message: string }>('/auth/resend-verification', { method: 'POST', body: JSON.stringify({ email }) }),

    getMyTenants: () =>
        request<any[]>('/auth/my-tenants'),

    switchTenant: (tenantId: string) =>
        request<{ access_token: string; redirect_url?: string; message?: string }>('/auth/switch-tenant', { method: 'POST', body: JSON.stringify({ tenant_id: tenantId }) }),
};

// ─── Tenants ──────────────────────────────────────────
export const tenantApi = {
    selfCreate: (data: { name: string }) =>
        request<any>('/tenants/self-create', { method: 'POST', body: JSON.stringify(data) }),

    join: (invitationCode: string) =>
        request<any>('/tenants/join', { method: 'POST', body: JSON.stringify({ invitation_code: invitationCode }) }),

    registrationConfig: () =>
        request<{ allow_self_create_company: boolean }>('/tenants/registration-config'),

    resolveByDomain: (domain: string) =>
        request<any>(`/tenants/resolve-by-domain?domain=${encodeURIComponent(domain)}`),

    me: () =>
        request<{ id: string; name: string; default_model_id: string | null; [k: string]: any }>('/tenants/me'),

    tokenUsage: () =>
        request<any>('/tenants/me/token-usage'),
};

export const onboardingApi = {
    status: () =>
        request<any>('/onboarding/status'),

    start: (entryMode: 'create' | 'join') =>
        request<any>('/onboarding/start', { method: 'POST', body: JSON.stringify({ entry_mode: entryMode }) }),

    createPersonalAssistant: (data: { name: string; personality: string; work_style: string; boundaries?: string }) =>
        request<any>('/onboarding/personal-assistant', { method: 'POST', body: JSON.stringify(data) }),

    complete: () =>
        request<any>('/onboarding/complete', { method: 'POST' }),
};

export const adminApi = {
    listCompanies: () =>
        request<any[]>('/admin/companies'),

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

export type WorkTaskDraft = {
    title: string;
    intent: string;
    work_type: 'general' | 'image' | 'video' | 'presentation' | 'document';
    priority: 'low' | 'medium' | 'high' | 'urgent';
    executor_kind: 'personal_assistant' | 'agent_employee' | 'temporary_expert' | 'group';
    agent_id?: string;
    expert_role?: string;
    group_id?: string;
    group_session_id?: string;
    group_agent_participant_ids?: string[];
};

export type WorkTaskPreflight = {
    confirmation_fingerprint: string;
    capability_status: 'available' | 'degraded' | 'unavailable';
    estimated_credits?: number | null;
    cost_note: string;
    approval_required: boolean;
    reasons: string[];
    next_action?: string | null;
    work_statement: {
        version: number;
        objective: string;
        title: string;
        work_type: WorkTaskDraft['work_type'];
        expected_output: string;
        delivery_mode: 'task_only';
        priority: WorkTaskDraft['priority'];
        executor: {
            kind: WorkTaskDraft['executor_kind'];
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

export const workApi = {
    list: (limit = 50) => request<WorkIndex>(`/work?limit=${limit}`),

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

// ─── Deliverable Workbench ───────────────────────────
export type DeliverableWorkType = 'presentation' | 'poster' | 'video' | 'report' | 'spreadsheet';

export interface DeliverableWorkflowField {
    key: string;
    label_zh: string;
    label_en: string;
    kind: 'text' | 'textarea' | 'number' | 'select';
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
            stage: 'final';
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
