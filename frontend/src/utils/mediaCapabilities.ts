export type MediaModality = 'image' | 'audio' | 'music' | 'video';

export interface MediaCapability {
    modality: MediaModality;
    tool_name: string;
    available: boolean;
    allowed_by_plan: boolean;
    pool_available: boolean;
    tool_enabled: boolean;
    reason: 'plan_denied' | 'pool_unavailable' | 'agent_tool_disabled' | null;
    allowed_tiers: string[];
    /**
     * Technical availability and commercial-quality readiness are separate
     * signals.  A degraded route may have a provider in the pool while still
     * requiring an explicit quality decision before it can be used.
     */
    capability_status?: 'available' | 'degraded' | 'unavailable';
    available_providers?: string[];
    route_reason?: string | null;
    next_action?: string | null;
}

export interface MediaCapabilitiesResponse {
    capabilities: MediaCapability[];
}

const PROMPTS: Record<MediaModality, { zh: string; en: string }> = {
    image: {
        zh: '请生成一张图片，并返回可直接预览的图片文件：',
        en: 'Create an image and return a directly previewable image file: ',
    },
    audio: {
        zh: '请将下面的内容生成语音，并返回可直接播放的音频文件：',
        en: 'Create speech audio and return a directly playable audio file from: ',
    },
    music: {
        zh: '请生成一首音乐，风格与歌词如下：',
        en: 'Create a song with this style and lyrics: ',
    },
    video: {
        zh: '请生成一个视频，持续查询直到生成完成，并返回可直接播放或下载的视频文件：',
        en: 'Create a video, keep checking until generation completes, and return a directly playable or downloadable video file: ',
    },
};

const ACTION_LABELS: Record<MediaModality, { zh: string; en: string }> = {
    image: { zh: '图片', en: 'Image' },
    audio: { zh: '语音', en: 'Speech' },
    music: { zh: '音乐', en: 'Music' },
    video: { zh: '视频', en: 'Video' },
};

export function buildMediaPrompt(modality: MediaModality, language: 'zh' | 'en'): string {
    return PROMPTS[modality][language];
}

export function mediaCapabilityShortLabel(modality: MediaModality, language: 'zh' | 'en'): string {
    return ACTION_LABELS[modality][language];
}

export function mediaCapabilityState(
    capability: MediaCapability,
    language: 'zh' | 'en',
): { disabled: boolean; label: string; action: 'upgrade' | 'open_tools' | 'contact_admin' | null } {
    if (capability.available && capability.capability_status === 'degraded') {
        const actionLabel = mediaCapabilityShortLabel(capability.modality, language);
        // Quick media generation is allowed to use the platform-managed
        // fallback.  The formal Deliverable workflow still requires an
        // explicit `allow_degraded` confirmation before it can be launched.
        // Keep provider/tier details out of the customer-facing composer: the
        // route is an internal platform decision and should not leak into the
        // normal task entry point.
        return {
            disabled: false,
            label: language === 'zh'
                ? `生成${actionLabel}（当前仅有应急质量线路；正式交付需先确认质量差异）`
                : `Generate ${actionLabel.toLowerCase()} (only an emergency-quality route is available; formal delivery requires confirming the quality difference)`,
            action: null,
        };
    }
    if (capability.available) {
        const actionLabel = mediaCapabilityShortLabel(capability.modality, language);
        return {
            disabled: false,
            label: language === 'zh'
                ? `生成${actionLabel}（点击后填写需求，不会自动发送）`
                : `Generate ${actionLabel.toLowerCase()} (insert a request; not sent automatically)`,
            action: null,
        };
    }
    if (!capability.allowed_by_plan) {
        return {
            disabled: true,
            label: language === 'zh' ? '当前套餐不包含此生成能力' : 'Your plan does not include this generation capability',
            action: 'upgrade',
        };
    }
    if (!capability.tool_enabled) {
        return {
            disabled: true,
            label: language === 'zh' ? '请先在 Agent 工具中启用此能力' : 'Enable this capability in Agent tools first',
            action: 'open_tools',
        };
    }
    return {
        disabled: true,
        label: language === 'zh' ? '平台生成账号池暂不可用，请联系管理员' : 'The generation account pool is unavailable; contact an administrator',
        action: 'contact_admin',
    };
}
