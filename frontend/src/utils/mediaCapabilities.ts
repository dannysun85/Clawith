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
}

export interface MediaCapabilitiesResponse {
    capabilities: MediaCapability[];
}

const PROMPTS: Record<MediaModality, { zh: string; en: string }> = {
    image: {
        zh: '请生成一张图片：',
        en: 'Create an image: ',
    },
    audio: {
        zh: '请将下面的内容生成语音：',
        en: 'Create speech audio from: ',
    },
    music: {
        zh: '请生成一首音乐，风格与歌词如下：',
        en: 'Create a song with this style and lyrics: ',
    },
    video: {
        zh: '请生成一个视频：',
        en: 'Create a video: ',
    },
};

export function buildMediaPrompt(modality: MediaModality, language: 'zh' | 'en'): string {
    return PROMPTS[modality][language];
}

export function mediaCapabilityState(
    capability: MediaCapability,
    language: 'zh' | 'en',
): { disabled: boolean; label: string; action: 'upgrade' | 'open_tools' | 'contact_admin' | null } {
    if (capability.available) {
        return {
            disabled: false,
            label: language === 'zh' ? '插入生成需求（不会自动发送）' : 'Insert a generation request (not sent automatically)',
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
