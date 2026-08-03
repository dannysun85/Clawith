import { describe, expect, it } from 'vitest';

import {
    buildMediaPrompt,
    mediaCapabilityShortLabel,
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
        expect(buildMediaPrompt('image', 'zh')).toContain('可直接预览的图片文件');
        expect(buildMediaPrompt('video', 'en')).toContain('keep checking until generation completes');
        expect(mediaCapabilityShortLabel('audio', 'zh')).toBe('语音');
    });

    it('reports available and actionable blocked states', () => {
        expect(mediaCapabilityState(base, 'zh').disabled).toBe(false);
        expect(mediaCapabilityState(base, 'zh').label).toContain('生成图片');
        expect(mediaCapabilityState({ ...base, available: false, tool_enabled: false, reason: 'agent_tool_disabled' }, 'zh')).toEqual(
            expect.objectContaining({ disabled: true, action: 'open_tools' }),
        );
        expect(mediaCapabilityState({ ...base, available: false, allowed_by_plan: false, reason: 'plan_denied' }, 'en')).toEqual(
            expect.objectContaining({ disabled: true, action: 'upgrade' }),
        );
    });

    it('keeps a degraded route usable for quick generation while formal delivery gates remain separate', () => {
        const state = mediaCapabilityState({
            ...base,
            modality: 'video',
            tool_name: 'generate_video_minimax',
            capability_status: 'degraded',
            available_providers: ['minimax'],
            route_reason: 'commercial_primary_unavailable',
        }, 'zh');

        expect(state).toEqual(expect.objectContaining({ disabled: false, action: null }));
        expect(state.label).toContain('当前仅有应急质量线路');
        expect(state.label).toContain('正式交付需先确认质量差异');
    });

    it('shows the server-provided account-tier explanation for a degraded route', () => {
        const state = mediaCapabilityState({
            ...base,
            modality: 'video',
            tool_name: 'generate_video_minimax',
            capability_status: 'degraded',
            available_providers: ['minimax'],
            route_reason: 'commercial_primary_unavailable',
            next_action: '火山 Agent Plan 当前为 plan=small，不包含视频资格；当前仅有 MiniMax 应急视频线路。',
        }, 'zh');

        expect(state.disabled).toBe(false);
        expect(state.label).toContain('正式交付需先确认质量差异');
        expect(state.label).not.toContain('plan=small');
        expect(state.label).not.toContain('不包含视频资格');
    });
});
