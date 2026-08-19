import { describe, expect, it } from 'vitest';

import {
    buildPlanUpdatePayload,
    formatOtherPlanFeatures,
    getOtherPlanFeatures,
    getPlanGenerationSettings,
    mergePlanGenerationFeatures,
    parseOtherPlanFeatures,
    planEditorFormIsDirty,
    planToEditorForm,
} from './planGenerationFeatures';

describe('SaaS plan generation feature helpers', () => {
    it('keeps chat routing and media generation settings separate', () => {
        expect(getPlanGenerationSettings(
            {
                generation_modalities: ['image', 'voice', 'music', 'video', 'text'],
                generation_tiers: ['LITE', 'pro'],
            },
            ['text'],
            ['ultra'],
        )).toEqual({
            modalities: ['image', 'audio', 'music', 'video'],
            tiers: ['lite', 'pro'],
            preservedModalities: ['text'],
            preservedTiers: [],
        });
    });

    it('matches the backend legacy fallback when generation keys are absent', () => {
        expect(getPlanGenerationSettings(
            { priority_support: true },
            ['text', 'vision', 'voice', 'multimodal', 'avatar'],
            ['pro'],
        )).toEqual({
            modalities: ['image', 'audio'],
            tiers: ['pro'],
            preservedModalities: [],
            preservedTiers: [],
        });
    });

    it('preserves legacy fallback tiers that the current editor cannot represent', () => {
        expect(getPlanGenerationSettings(
            {},
            ['text', 'image'],
            ['lite', 'standard'],
        )).toEqual({
            modalities: ['image'],
            tiers: ['lite'],
            preservedModalities: [],
            preservedTiers: ['standard'],
        });
    });

    it('round-trips generation controls without losing unrelated feature flags', () => {
        const original = {
            recommended: true,
            generation_modalities: ['image'],
            generation_tiers: ['lite'],
        };
        expect(getOtherPlanFeatures(original)).toEqual({ recommended: true });
        expect(formatOtherPlanFeatures(original)).toBe(JSON.stringify({ recommended: true }, null, 2));
        expect(mergePlanGenerationFeatures(
            { recommended: true },
            ['image', 'audio'],
            ['lite', 'pro'],
            ['avatar'],
            ['enterprise'],
        )).toEqual({
            recommended: true,
            generation_modalities: ['image', 'audio', 'avatar'],
            generation_tiers: ['lite', 'pro', 'enterprise'],
        });
    });

    it('preserves future and malformed explicit generation values for lossless edits', () => {
        expect(getPlanGenerationSettings(
            {
                generation_modalities: ['image', 'avatar', { future: true }],
                generation_tiers: ['ultra', 'enterprise', 4],
            },
            ['text'],
            ['lite'],
        )).toEqual({
            modalities: ['image'],
            tiers: ['ultra'],
            preservedModalities: ['avatar', { future: true }],
            preservedTiers: ['enterprise', 4],
        });
    });

    it('accepts only JSON objects for unrelated feature flags', () => {
        expect(parseOtherPlanFeatures('')).toEqual({});
        expect(parseOtherPlanFeatures('{"priority_support":true}')).toEqual({ priority_support: true });
        expect(() => parseOtherPlanFeatures('[]')).toThrow('其他特性必须是 JSON 对象');
        expect(() => parseOtherPlanFeatures('"flag"')).toThrow('其他特性必须是 JSON 对象');
    });

    it('builds a minimal CAS PATCH body with generation fields nested under features', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            allowed_tiers: ['lite', 'pro'],
            max_agents: 3,
            max_llm_calls_per_day: 1000,
            message_limit: 50,
            message_period: 'monthly',
            max_triggers: 20,
            credits_per_period: 1000,
            price_cents: 9900,
            features: {
                recommended: true,
                generation_modalities: ['image', 'avatar'],
                generation_tiers: ['lite', 'enterprise'],
            },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.price_cents = 10900;
        form.generation_modalities = ['image', 'audio'];

        expect(planEditorFormIsDirty(form, plan)).toBe(true);
        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            price_cents: 10900,
            features: {
                recommended: true,
                generation_modalities: ['image', 'audio', 'avatar'],
                generation_tiers: ['lite', 'enterprise'],
            },
        });
    });

    it('does not rewrite features when only an unrelated scalar changes', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            allowed_tiers: ['lite'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 100,
            price_cents: 0,
            features: { generation_modalities: ['image'], generation_tiers: ['lite'] },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.max_agents = 4;

        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            max_agents: 4,
        });
    });

    it('freezes both legacy generation fallbacks before chat routing changes', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text', 'image', 'voice'],
            allowed_tiers: ['lite', 'standard'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 100,
            price_cents: 0,
            features: { recommended: true },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.allowed_modalities = ['text'];
        form.allowed_tiers = ['ultra'];

        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            allowed_tiers: ['ultra'],
            features: {
                recommended: true,
                generation_modalities: ['image', 'audio'],
                generation_tiers: ['lite', 'standard'],
            },
        });
    });

    it('materializes only a missing generation modality fallback', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text', 'image'],
            allowed_tiers: ['lite'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 100,
            price_cents: 0,
            features: { generation_tiers: ['PRO', 'enterprise', 4] },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.allowed_modalities = ['text'];

        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            features: {
                generation_modalities: ['image'],
                generation_tiers: ['PRO', 'enterprise', 4],
            },
        });
    });

    it('materializes only a missing generation tier fallback', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            allowed_tiers: ['lite', 'pro'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 100,
            price_cents: 0,
            features: { generation_modalities: ['IMAGE', 'avatar', { future: true }] },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.allowed_tiers = ['ultra'];

        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            allowed_tiers: ['ultra'],
            features: {
                generation_modalities: ['IMAGE', 'avatar', { future: true }],
                generation_tiers: ['lite', 'pro'],
            },
        });
    });

    it('freezes the relevant fallback for a new plan with null features', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text', 'image'],
            allowed_tiers: ['lite'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 100,
            price_cents: 0,
            features: null,
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.allowed_modalities = ['text'];

        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            features: { generation_modalities: ['image'] },
        });
    });

    it('fails safely instead of overwriting a malformed generation setting', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text', 'image'],
            allowed_tiers: ['lite'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 100,
            price_cents: 0,
            features: { generation_modalities: { future_schema: true } },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.allowed_modalities = ['text'];

        expect(() => buildPlanUpdatePayload(form, plan)).toThrow(
            '媒体生成能力配置格式异常，请先调整媒体生成能力选项后再保存',
        );

        form.generation_modalities = ['image', 'audio'];
        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            features: { generation_modalities: ['image', 'audio'] },
        });
    });

    it('preserves malformed generation values during unrelated feature edits', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            allowed_tiers: ['lite'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 100,
            price_cents: 0,
            features: {
                recommended: true,
                generation_modalities: { future_schema: true },
                generation_tiers: 'lite',
            },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        form.features = '{"recommended":false}';

        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            features: {
                recommended: false,
                generation_modalities: { future_schema: true },
                generation_tiers: 'lite',
            },
        });
    });

    it('writes yearly pricing through dedicated editor fields into features', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            allowed_tiers: ['lite'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 1000,
            price_cents: 2000,
            features: {
                recommended: true,
                yearly_price_cents: 19200,
                yearly_discount_percent: 20,
            },
            is_active: true,
        };
        const form = planToEditorForm(plan);
        expect(form.yearly_price_cents).toBe(19200);
        expect(form.yearly_discount_percent).toBe(20);

        form.yearly_price_cents = 18000;
        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
            features: {
                recommended: true,
                yearly_price_cents: 18000,
                yearly_discount_percent: 20,
            },
        });
    });

    it('derives yearly editor defaults from the monthly price when unset', () => {
        const plan = {
            updated_at: '2026-07-13T12:00:00Z',
            allowed_modalities: ['text'],
            allowed_tiers: ['lite'],
            max_agents: 2,
            max_llm_calls_per_day: 100,
            message_limit: 20,
            message_period: 'monthly',
            max_triggers: 5,
            credits_per_period: 1000,
            price_cents: 2000,
            features: null,
            is_active: true,
        };
        const form = planToEditorForm(plan);
        expect(form.yearly_price_cents).toBe(Math.round(2000 * 12 * 0.8));
        expect(form.yearly_discount_percent).toBe(20);
        // Unchanged yearly fields do not trigger a features write.
        expect(buildPlanUpdatePayload(form, plan)).toEqual({
            expected_updated_at: '2026-07-13T12:00:00Z',
        });
    });
});
