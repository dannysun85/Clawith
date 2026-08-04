const MANAGED_MEDIA_TOOL_TITLES: Readonly<Record<string, string>> = {
    generate_image_minimax: 'Generate Image',
    check_image_generation: 'Check Image',
    generate_speech_minimax: 'Generate Speech',
    generate_music_minimax: 'Generate Music',
    generate_video_minimax: 'Generate Video',
    check_video_minimax: 'Check Video',
    compose_video_audio: 'Compose Video Audio',
    compose_video_audio_minimax: 'Compose Video Audio',
};

const MANAGED_MEDIA_ROUTING_KEYS = new Set([
    'apikey',
    'credential',
    'credentialid',
    'jobid',
    'model',
    'modelid',
    'modelname',
    'provider',
    'providername',
    'providertaskid',
    'requestid',
    'taskid',
    'traceid',
]);

const MEDIA_ARTIFACT_PATH_PATTERN =
    /workspace\/[^\s"'<>]*\.(?:png|jpe?g|gif|webp|bmp|mp3|wav|m4a|aac|ogg|flac|mp4|mov|webm|mkv)/i;

function escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function replaceManagedMediaIdentifiers(value: string): string {
    let output = value;
    for (const [name, title] of Object.entries(MANAGED_MEDIA_TOOL_TITLES)) {
        output = output.replace(new RegExp(`\\b${escapeRegExp(name)}\\b`, 'gi'), title);
    }
    return output;
}

function looksLikeMediaReceipt(value: string): boolean {
    return MEDIA_ARTIFACT_PATH_PATTERN.test(value)
        || /(?:图片|视频|语音|音频|音乐).{0,20}(?:已生成|生成完成|制作完成)/i.test(value)
        || /(?:image|video|audio|speech|music).{0,20}(?:generated|ready|completed)/i.test(value);
}

function removeManagedRoutingLines(value: string): string {
    const lines = value.split(/\r?\n/);
    return lines
        .filter((line) => !/^\s*(?:[-*]\s*)?(?:\*\*|__)?(?:提供方|服务商|provider(?:[ _-]?name)?|模型|model(?:[ _-]?(?:id|name))?|generated[ _-]?by|(?:provider[ _-]?)?task[ _-]?id|任务\s*id|job[ _-]?id|request[ _-]?id|trace[ _-]?id)(?:\*\*|__)?\s*[:：].*$/i.test(line))
        .join('\n')
        .replace(/\s+(?:task\s*id|任务\s*id)\s*[:：]\s*[0-9a-f-]{8,}(?=\s|$)/gi, '')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

function sanitizeManagedMediaValue(value: unknown): unknown {
    if (Array.isArray(value)) return value.map(sanitizeManagedMediaValue);
    if (value && typeof value === 'object') {
        return Object.fromEntries(
            Object.entries(value as Record<string, unknown>)
                .filter(([key]) => !MANAGED_MEDIA_ROUTING_KEYS.has(key.toLowerCase().replace(/[^a-z0-9]/g, '')))
                .map(([key, nestedValue]) => [key, sanitizeManagedMediaValue(nestedValue)]),
        );
    }
    if (typeof value === 'string') return replaceManagedMediaIdentifiers(value);
    return value;
}

/**
 * Return a customer-safe title for a runtime tool identifier.
 *
 * The managed media identifiers retain their historical provider suffixes for
 * protocol compatibility. Provider selection is an internal routing concern,
 * so those suffixes must not leak into the customer-facing execution trace.
 */
export function toolDisplayName(name: string): string {
    const normalized = (name || 'tool').trim();
    const managedTitle = MANAGED_MEDIA_TOOL_TITLES[normalized.toLowerCase()];
    if (managedTitle) return managedTitle;

    return normalized
        .replace(/^mcp[_:-]/i, '')
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/\b\w/g, ch => ch.toUpperCase());
}

export function isManagedMediaTool(name: string): boolean {
    return Boolean(MANAGED_MEDIA_TOOL_TITLES[(name || '').trim().toLowerCase()]);
}

/**
 * Remove routing-only fields from managed media arguments before rendering.
 * Runtime payloads remain unchanged; this is a customer-facing projection.
 */
export function customerSafeToolArgs(name: string, args: unknown): unknown {
    if (!isManagedMediaTool(name)) return args;
    return sanitizeManagedMediaValue(args);
}

/**
 * Hide provider/model/task routing receipts while retaining customer paths and outcomes.
 */
export function customerSafeToolResult(name: string, result: unknown): string {
    if (isManagedMediaTool(name) && typeof result === 'string') {
        try {
            const parsed = JSON.parse(result);
            return JSON.stringify(sanitizeManagedMediaValue(parsed), null, 2) || '';
        } catch {
            // Plain-text receipts are projected below.
        }
    }
    const value = typeof result === 'string'
        ? result
        : (JSON.stringify(isManagedMediaTool(name) ? sanitizeManagedMediaValue(result) : result ?? '', null, 2) || '');
    const normalized = replaceManagedMediaIdentifiers(value);
    return isManagedMediaTool(name) ? removeManagedRoutingLines(normalized) : normalized;
}

/**
 * Project historical assistant receipts and reasoning through the same provider-neutral
 * vocabulary. Provider/model lines are removed only when the text is a media receipt.
 */
export function customerSafeAssistantText(value: unknown): string {
    const normalized = replaceManagedMediaIdentifiers(typeof value === 'string' ? value : '');
    return looksLikeMediaReceipt(normalized) ? removeManagedRoutingLines(normalized) : normalized;
}

/**
 * Never project raw model reasoning into a tenant-facing conversation.
 *
 * The durable runtime may retain reasoning for provider continuity and audit,
 * but that payload can contain system instructions, credentials, hidden route
 * choices, or speculative intermediate text. Tool receipts remain visible as
 * the customer-verifiable execution trace.
 */
export function customerSafeThinkingText(
    value: unknown,
    replacement = 'Internal reasoning is private. Tool execution records remain available.',
): string {
    return typeof value === 'string' && value.trim()
        ? replacement
        : '';
}

export function customerSafeAnalysisText(
    kind: 'thinking' | 'assistant_progress',
    value: unknown,
    privateReasoningReplacement?: string,
): string {
    return kind === 'thinking'
        ? customerSafeThinkingText(value, privateReasoningReplacement)
        : customerSafeAssistantText(value);
}
