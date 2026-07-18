export function safeWorkspaceMediaPath(value: unknown): string | null {
    const path = typeof value === 'string' ? value.trim().replace(/\\/g, '/') : '';
    if (!path.startsWith('workspace/')) return null;
    if (path.split('/').some((segment) => segment === '..')) return null;
    return path;
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

export function appendUniqueById<T extends { id?: string }>(
    messages: T[],
    incoming: T,
): T[] {
    if (incoming.id && messages.some((message) => message.id === incoming.id)) {
        return messages;
    }
    return [...messages, incoming];
}
