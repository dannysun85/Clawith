import { useQuery } from '@tanstack/react-query';
import { enterpriseApi, fetchJson } from '../services/api';
import { canonicalizeModalities, canonicalizeModality } from '../constants/modalities';

/** Tenant entitlements from /subscription/my-entitlements (drives plan-gating). */
export interface Entitlements {
    plan_id?: string | null;
    plan_code?: string;
    max_agents: number;
    max_llm_calls_per_day: number;
    message_limit: number;
    max_triggers: number;
    credits_per_period: number;
    allowed_modalities: string[];
    allowed_tiers: string[];
    generation_modalities: string[];
    generation_tiers: string[];
    subscription_status?: string;
    period_end?: string;
}

/** LLM model as returned by /enterprise/llm-models (now includes modality/tier). */
export interface LlmModelInfo {
    id: string;
    provider: string;
    model: string;
    label?: string;
    base_url?: string;
    api_key_masked?: string;
    enabled?: boolean;
    supports_vision?: boolean;
    max_output_tokens?: number | null;
    request_timeout?: number | null;
    temperature?: number | null;
    modality?: string; // text/image/audio/music/video/multimodal; legacy vision maps to image
    tier?: string; // premium/standard/basic
}

/**
 * Whether a model is usable under the tenant's plan.
 * No entitlements / empty allowed sets → no restriction (backward-compatible fallback).
 */
export function isModelAllowed(m: LlmModelInfo, ent: Entitlements | null | undefined): boolean {
    if (!ent) return true;
    const mod = canonicalizeModality(m.modality);
    const tier = m.tier || 'standard';
    const allowedModalities = canonicalizeModalities(ent.allowed_modalities);
    const modOK = !allowedModalities.length || allowedModalities.includes(mod);
    const tierOK = !ent.allowed_tiers?.length || ent.allowed_tiers.includes(tier);
    return modOK && tierOK;
}

export interface UseLlmModelsOptions {
    /** Model id to keep in the picker even if disabled/plan-disallowed (the agent's
     *  currently-saved model) — prevents silently losing the selection. */
    keepModelId?: string;
}

export interface UseLlmModelsResult {
    /** Models to render: plan-allowed + enabled, plus the kept-current model if blocked. */
    models: LlmModelInfo[];
    /** Plan-allowed + enabled models (excludes kept-current). */
    allowed: LlmModelInfo[];
    /** All enabled models (unfiltered by plan). */
    allEnabled: LlmModelInfo[];
    /** The kept-current model id if it is disabled or plan-disallowed (for ⚠️ UX). */
    blockedCurrentId?: string;
    entitlements: Entitlements | null | undefined;
    isLoading: boolean;
}

/** Tenant-facing subscription capabilities; never exposes real model metadata. */
export function useEntitlements() {
    return useQuery({
        queryKey: ['subscription-entitlements'],
        queryFn: () => fetchJson<Entitlements | null>('/subscription/my-entitlements'),
    });
}

/**
 * Fetches LLM models + tenant entitlements and filters the model list by the
 * tenant's plan (allowed_modalities / allowed_tiers). Shared by all agent model
 * pickers so plan-gating is consistent. TanStack Query dedupes the underlying
 * requests across components.
 *
 * LlmTab (admin model management) does NOT use this — it shows all models.
 */
export function useLlmModels(options: UseLlmModelsOptions = {}): UseLlmModelsResult {
    const { keepModelId } = options;
    const { data: rawModels = [], isLoading: loadingModels } = useQuery({
        queryKey: ['llm-models'],
        queryFn: enterpriseApi.llmModels,
    });
    const { data: entitlements, isLoading: loadingEnt } = useEntitlements();

    const all = rawModels as LlmModelInfo[];
    const allEnabled = all.filter((m) => m.enabled !== false);
    const allowed = allEnabled.filter((m) => isModelAllowed(m, entitlements));

    const kept = keepModelId ? all.find((m) => m.id === keepModelId) : undefined;
    const blockedCurrentId =
        kept && (!kept.enabled || !isModelAllowed(kept, entitlements)) ? kept.id : undefined;
    const models = blockedCurrentId && kept ? [...allowed, kept] : allowed;

    return {
        models,
        allowed,
        allEnabled,
        blockedCurrentId,
        entitlements,
        isLoading: loadingModels || loadingEnt,
    };
}

/** Return the tenant's allowed SaaS tiers (lite/pro/ultra) from entitlements. */
export function useAllowedTiers(): string[] {
    const { data: entitlements } = useEntitlements();
    return entitlements?.allowed_tiers || [];
}
