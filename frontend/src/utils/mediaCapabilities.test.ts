import { describe, expect, it } from 'vitest';

import {
    buildMediaPrompt,
    mediaCapabilityState,
    type MediaCapability,
} from './mediaCapabilities';

const base: MediaCapability = {
    modality: 'image',
    tool_name: 'generate_image_minimax',
    available: true,
    allowed_by_plan: true,
    pool_available: true,
    tool_enabled: true,
    reason: null,
    allowed_tiers: ['lite'],
};

describe('media capability helpers', () => {
    it('creates a draft prompt and never auto-submits paid generation', () => {
        expect(buildMediaPrompt('image', 'zh')).toContain('请生成一张图片');
        expect(buildMediaPrompt('video', 'en')).toContain('Create a video');
    });

    it('reports available and actionable blocked states', () => {
        expect(mediaCapabilityState(base, 'zh').disabled).toBe(false);
        expect(mediaCapabilityState({ ...base, available: false, tool_enabled: false, reason: 'agent_tool_disabled' }, 'zh')).toEqual(
            expect.objectContaining({ disabled: true, action: 'open_tools' }),
        );
        expect(mediaCapabilityState({ ...base, available: false, allowed_by_plan: false, reason: 'plan_denied' }, 'en')).toEqual(
            expect.objectContaining({ disabled: true, action: 'upgrade' }),
        );
    });
});
