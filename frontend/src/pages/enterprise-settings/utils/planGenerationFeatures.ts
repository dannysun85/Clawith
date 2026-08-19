import { canonicalizeModalities } from '../../../constants/modalities';

export const GENERATION_MODALITIES = ['image', 'audio', 'music', 'video'] as const;
export const GENERATION_TIERS = ['lite', 'pro', 'ultra'] as const;

type PlanFeatures = Record<string, unknown> | null | undefined;

export interface PlanEditorSnapshot {
    updated_at: string;
    allowed_modalities: string[] | null;
    allowed_tiers: string[] | null;
    max_agents: number;
    max_llm_calls_per_day: number;
    message_limit: number;
    message_period: string;
    max_triggers: number;
    credits_per_period: number;
    price_cents: number;
    features: Record<string, unknown> | null;
    is_active: boolean;
}

export interface PlanEditorForm {
    allowed_modalities: string[];
    allowed_tiers: string[];
    generation_modalities: string[];
    generation_tiers: string[];
    max_agents: number;
    max_llm_calls_per_day: number;
    message_limit: number;
    message_period: string;
    max_triggers: number;
    credits_per_period: number;
    price_cents: number;
    yearly_price_cents: number;
    yearly_discount_percent: number;
    features: string;
    is_active: boolean;
}

function explicitList(value: unknown): unknown[] | null {
    if (!Array.isArray(value)) return null;
    return value;
}

function normalizedStrings(values: unknown[]): string[] {
    return values
        .filter((item): item is string => typeof item === 'string')
        .map((item) => item.trim().toLowerCase())
        .filter(Boolean);
}

export function getPlanGenerationSettings(
    features: PlanFeatures,
    allowedModalities: string[] | null | undefined,
    allowedTiers: string[] | null | undefined,
): {
    modalities: string[];
    tiers: string[];
    preservedModalities: unknown[];
    preservedTiers: unknown[];
} {
    const explicitModalities = explicitList(features?.generation_modalities);
    const explicitTiers = explicitList(features?.generation_tiers);
    const mediaModalities = new Set<string>(GENERATION_MODALITIES);
    const knownTiers = new Set<string>(GENERATION_TIERS);
    const normalizedModalities = canonicalizeModalities(
        explicitModalities === null
            ? (allowedModalities ?? [])
            : normalizedStrings(explicitModalities),
    );
    const normalizedTiers = explicitTiers === null
        ? (allowedTiers || []).map((value) => String(value).trim().toLowerCase()).filter(Boolean)
        : normalizedStrings(explicitTiers);

    return {
        modalities: normalizedModalities.filter((value) => mediaModalities.has(value)),
        tiers: normalizedTiers.filter((value) => knownTiers.has(value)),
        preservedModalities: explicitModalities === null
            ? []
            : explicitModalities.filter((value) => {
                if (typeof value !== 'string') return true;
                const canonical = canonicalizeModalities([value])[0];
                return !canonical || !mediaModalities.has(canonical);
            }),
        preservedTiers: explicitTiers === null
            ? normalizedTiers.filter((value) => !knownTiers.has(value))
            : explicitTiers.filter((value) => {
                if (typeof value !== 'string') return true;
                return !knownTiers.has(value.trim().toLowerCase());
            }),
    };
}

export function getOtherPlanFeatures(features: PlanFeatures): Record<string, unknown> {
    if (!features) return {};
    const {
        generation_modalities: _generationModalities,
        generation_tiers: _generationTiers,
        yearly_price_cents: _yearlyPriceCents,
        yearly_discount_percent: _yearlyDiscountPercent,
        ...otherFeatures
    } = features;
    return otherFeatures;
}

export function formatOtherPlanFeatures(features: PlanFeatures): string {
    const otherFeatures = getOtherPlanFeatures(features);
    return Object.keys(otherFeatures).length > 0 ? JSON.stringify(otherFeatures, null, 2) : '';
}

export function mergePlanGenerationFeatures(
    otherFeatures: Record<string, unknown>,
    modalities: string[],
    tiers: string[],
    preservedModalities: unknown[] = [],
    preservedTiers: unknown[] = [],
): Record<string, unknown> {
    return {
        ...otherFeatures,
        generation_modalities: [...modalities, ...preservedModalities],
        generation_tiers: [...tiers, ...preservedTiers],
    };
}

export function parseOtherPlanFeatures(raw: string): Record<string, unknown> {
    if (!raw.trim()) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error('其他特性必须是 JSON 对象');
    }
    return parsed as Record<string, unknown>;
}

export function planToEditorForm(plan: PlanEditorSnapshot): PlanEditorForm {
    const generation = getPlanGenerationSettings(plan.features, plan.allowed_modalities, plan.allowed_tiers);
    const features = plan.features ?? {};
    const yearlyPrice = typeof features.yearly_price_cents === 'number' && features.yearly_price_cents > 0
        ? features.yearly_price_cents
        : Math.round(plan.price_cents * 12 * 0.8);
    const yearlyDiscount = typeof features.yearly_discount_percent === 'number'
        ? features.yearly_discount_percent
        : (plan.price_cents > 0 ? 20 : 0);
    return {
        allowed_modalities: plan.allowed_modalities || [],
        allowed_tiers: plan.allowed_tiers || [],
        generation_modalities: generation.modalities,
        generation_tiers: generation.tiers,
        max_agents: plan.max_agents,
        max_llm_calls_per_day: plan.max_llm_calls_per_day,
        message_limit: plan.message_limit,
        message_period: plan.message_period || 'permanent',
        max_triggers: plan.max_triggers,
        credits_per_period: plan.credits_per_period,
        price_cents: plan.price_cents,
        yearly_price_cents: yearlyPrice,
        yearly_discount_percent: yearlyDiscount,
        features: formatOtherPlanFeatures(plan.features),
        is_active: plan.is_active,
    };
}

export function planEditorFormIsDirty(form: PlanEditorForm, plan: PlanEditorSnapshot): boolean {
    const original = planToEditorForm(plan);
    return (Object.keys(original) as Array<keyof PlanEditorForm>).some(
        (key) => JSON.stringify(form[key]) !== JSON.stringify(original[key]),
    );
}

export function buildPlanUpdatePayload(
    form: PlanEditorForm,
    plan: PlanEditorSnapshot,
): Record<string, unknown> {
    const original = planToEditorForm(plan);
    const generation = getPlanGenerationSettings(plan.features, plan.allowed_modalities, plan.allowed_tiers);
    const otherFeatures = parseOtherPlanFeatures(form.features);
    const payload: Record<string, unknown> = { expected_updated_at: plan.updated_at };

    const scalarAndChatKeys: Array<keyof PlanEditorForm> = [
        'allowed_modalities',
        'allowed_tiers',
        'max_agents',
        'max_llm_calls_per_day',
        'message_limit',
        'message_period',
        'max_triggers',
        'credits_per_period',
        'price_cents',
        'is_active',
    ];
    for (const key of scalarAndChatKeys) {
        if (JSON.stringify(form[key]) !== JSON.stringify(original[key])) payload[key] = form[key];
    }

    const generationModalitiesChanged =
        JSON.stringify(form.generation_modalities) !== JSON.stringify(original.generation_modalities);
    const generationTiersChanged =
        JSON.stringify(form.generation_tiers) !== JSON.stringify(original.generation_tiers);
    const chatModalitiesChanged =
        JSON.stringify(form.allowed_modalities) !== JSON.stringify(original.allowed_modalities);
    const chatTiersChanged =
        JSON.stringify(form.allowed_tiers) !== JSON.stringify(original.allowed_tiers);
    const featureSource = plan.features ?? {};
    const hasFeature = (key: string): boolean => Object.prototype.hasOwnProperty.call(featureSource, key);
    const hasExplicitGenerationModalities = Array.isArray(featureSource.generation_modalities);
    const hasExplicitGenerationTiers = Array.isArray(featureSource.generation_tiers);
    const hasMalformedGenerationModalities =
        hasFeature('generation_modalities') && !hasExplicitGenerationModalities;
    const hasMalformedGenerationTiers =
        hasFeature('generation_tiers') && !hasExplicitGenerationTiers;
    const freezeFallbackModalities = chatModalitiesChanged && !hasExplicitGenerationModalities;
    const freezeFallbackTiers = chatTiersChanged && !hasExplicitGenerationTiers;

    if (freezeFallbackModalities && hasMalformedGenerationModalities && !generationModalitiesChanged) {
        throw new Error('媒体生成能力配置格式异常，请先调整媒体生成能力选项后再保存');
    }
    if (freezeFallbackTiers && hasMalformedGenerationTiers && !generationTiersChanged) {
        throw new Error('媒体生成档位配置格式异常，请先调整媒体生成档位选项后再保存');
    }

    const yearlyChanged =
        form.yearly_price_cents !== original.yearly_price_cents ||
        form.yearly_discount_percent !== original.yearly_discount_percent;

    if (
        generationModalitiesChanged ||
        generationTiersChanged ||
        form.features !== original.features ||
        freezeFallbackModalities ||
        freezeFallbackTiers ||
        yearlyChanged
    ) {
        const nextFeatures: Record<string, unknown> = { ...otherFeatures };
        // Yearly pricing lives in features but is edited via dedicated fields;
        // preserve untouched values when the yearly fields were not edited.
        if (yearlyChanged) {
            nextFeatures.yearly_price_cents = form.yearly_price_cents;
            nextFeatures.yearly_discount_percent = form.yearly_discount_percent;
        } else {
            if (hasFeature('yearly_price_cents')) nextFeatures.yearly_price_cents = featureSource.yearly_price_cents;
            if (hasFeature('yearly_discount_percent')) nextFeatures.yearly_discount_percent = featureSource.yearly_discount_percent;
        }

        if (generationModalitiesChanged) {
            nextFeatures.generation_modalities = [
                ...form.generation_modalities,
                ...generation.preservedModalities,
            ];
        } else if (freezeFallbackModalities) {
            // A legacy plan without an explicit generation setting inherits from
            // allowed_modalities. Freeze the current entitlement before changing
            // the chat routing field so a chat-only edit cannot alter media access.
            nextFeatures.generation_modalities = [
                ...generation.modalities,
                ...generation.preservedModalities,
            ];
        } else if (hasFeature('generation_modalities')) {
            nextFeatures.generation_modalities = featureSource.generation_modalities;
        }

        if (generationTiersChanged) {
            nextFeatures.generation_tiers = [
                ...form.generation_tiers,
                ...generation.preservedTiers,
            ];
        } else if (freezeFallbackTiers) {
            // generation_tiers has the same legacy fallback relationship with
            // allowed_tiers and must be materialized independently.
            nextFeatures.generation_tiers = [
                ...generation.tiers,
                ...generation.preservedTiers,
            ];
        } else if (hasFeature('generation_tiers')) {
            nextFeatures.generation_tiers = featureSource.generation_tiers;
        }

        payload.features = nextFeatures;
    }
    return payload;
}
