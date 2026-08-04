export function safeWorkspaceMediaPath(value: unknown): string | null {
    const path = typeof value === 'string' ? value.trim().replace(/\\/g, '/') : '';
    if (!path.startsWith('workspace/')) return null;
    if (path.split('/').some((segment) => segment === '..')) return null;
    return path;
}

export function workspaceMediaPathFromArtifactRefs(
    value: unknown,
    agentId: string,
): string | null {
    if (!Array.isArray(value) || !agentId.trim()) return null;
    const prefix = `workspace://${agentId}/`;
    for (const ref of value) {
        if (typeof ref !== 'string' || !ref.startsWith(prefix)) continue;
        const path = safeWorkspaceMediaPath(ref.slice(prefix.length));
        if (path) return path;
    }
    return null;
}

const COMPLETION_TOOLS: Record<string, string> = {
    image: 'generate_image_minimax',
    audio: 'generate_speech_minimax',
    music: 'generate_music_minimax',
    video: 'generate_video_minimax',
};

export function safeMediaCompletionTool(modality: unknown, toolName: unknown): string {
    const normalizedModality = typeof modality === 'string' ? modality.trim().toLowerCase() : '';
    const expected = COMPLETION_TOOLS[normalizedModality];
    if (!expected) return 'media_generation';
    const requested = typeof toolName === 'string' ? toolName.trim() : '';
    return requested === expected ? requested : expected;
}

const HISTORY_MEDIA_TOOLS = new Set([
    'generate_image_siliconflow',
    'generate_image_openai',
    'generate_image_google',
    'generate_image_custom',
    'generate_image_minimax',
    'check_image_generation',
    'generate_speech_minimax',
    'generate_music_minimax',
    'generate_video_minimax',
    'check_video_minimax',
    'compose_video_audio',
]);

const WORKSPACE_MEDIA_PATH_PATTERN =
    /workspace\/[^\r\n"'<>]*?\.(?:png|jpe?g|gif|webp|bmp|mp3|wav|m4a|aac|ogg|flac|mp4|mov|webm|mkv)(?=$|[\s`)\]}>,，。；;])/gi;

export interface PersistedMediaToolMessage {
    role?: unknown;
    toolName?: unknown;
    toolStatus?: unknown;
    toolResult?: unknown;
    content?: unknown;
}

function workspaceMediaPathsFromText(value: unknown): string[] {
    const text = typeof value === 'string' ? value : '';
    const paths: string[] = [];
    for (const match of text.matchAll(new RegExp(WORKSPACE_MEDIA_PATH_PATTERN))) {
        const path = safeWorkspaceMediaPath(match[0]);
        if (path) paths.push(path);
    }
    return paths;
}

/**
 * Recover the last successfully generated media artifact from durable chat history.
 *
 * Realtime completion events open the workspace panel immediately, but those events
 * are transient. Durable tool-call rows are the source of truth after a reload.
 */
export function latestCompletedWorkspaceMediaPath(
    messages: PersistedMediaToolMessage[],
): string | null {
    const successfulToolPaths = new Set<string>();
    for (const message of messages) {
        const toolName = typeof message.toolName === 'string' ? message.toolName.trim() : '';
        if (
            message.role !== 'tool_call'
            || message.toolStatus !== 'done'
            || !HISTORY_MEDIA_TOOLS.has(toolName)
        ) {
            continue;
        }
        const result = typeof message.toolResult === 'string'
            ? message.toolResult
            : message.content;
        workspaceMediaPathsFromText(result).forEach((path) => successfulToolPaths.add(path));
    }

    // The final assistant receipt identifies the customer-facing artifact when
    // several tool rows in one Run share the same database timestamp and their
    // UUID tie-break order does not reflect execution order. It may only select
    // a path that also exists in a successful durable media tool receipt.
    for (let index = messages.length - 1; index >= 0; index -= 1) {
        const message = messages[index];
        if (message.role !== 'assistant') continue;
        const referencedPaths = workspaceMediaPathsFromText(message.content);
        for (let pathIndex = referencedPaths.length - 1; pathIndex >= 0; pathIndex -= 1) {
            if (successfulToolPaths.has(referencedPaths[pathIndex])) {
                return referencedPaths[pathIndex];
            }
        }
    }

    for (let index = messages.length - 1; index >= 0; index -= 1) {
        const message = messages[index];
        const toolName = typeof message.toolName === 'string' ? message.toolName.trim() : '';
        if (
            message.role !== 'tool_call'
            || message.toolStatus !== 'done'
            || !HISTORY_MEDIA_TOOLS.has(toolName)
        ) {
            continue;
        }
        const result = typeof message.toolResult === 'string'
            ? message.toolResult
            : (typeof message.content === 'string' ? message.content : '');
        const paths = workspaceMediaPathsFromText(result);
        if (paths.length > 0) return paths[paths.length - 1];
    }
    return null;
}

export function appendUniqueById<T extends { id?: string }>(
    messages: T[],
    incoming: T,
): T[] {
    if (incoming.id && messages.some((message) => message.id === incoming.id)) {
        return messages;
    }
    return [...messages, incoming];
}
