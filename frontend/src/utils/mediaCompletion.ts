export function safeWorkspaceMediaPath(value: unknown): string | null {
    const path = typeof value === 'string' ? value.trim().replace(/\\/g, '/') : '';
    if (!path.startsWith('workspace/')) return null;
    if (path.split('/').some((segment) => segment === '..')) return null;
    return path;
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
