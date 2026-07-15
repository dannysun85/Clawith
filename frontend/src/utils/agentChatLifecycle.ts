export function activeSessionMatchesRequestedSession(
    requestedSessionId: string,
    activeSessionId: unknown,
): boolean {
    const activeId = activeSessionId == null ? '' : String(activeSessionId);
    if (!activeId) return false;
    return !requestedSessionId || activeId === requestedSessionId;
}

export function chatHistoryIsReady(
    readyRuntimeKey: string | null,
    agentId: string,
    sessionId: unknown,
): boolean {
    if (sessionId == null) return false;
    return readyRuntimeKey === `${agentId}:${String(sessionId)}`;
}

export function searchForSelectedChatSession(
    currentSearch: string,
    sessionId: unknown,
): string {
    const params = new URLSearchParams(currentSearch);
    params.set('session_id', String(sessionId));
    params.delete('workspace_path');
    params.delete('message_id');
    const query = params.toString();
    return query ? `?${query}` : '';
}

export type RequestedChatSessionScope = 'mine' | 'all';

export function resolveRequestedChatSession<T extends { id: unknown }>(
    requestedSessionId: string,
    mineSessions: T[],
    allSessions: T[],
    canViewAll: boolean,
): { scope: RequestedChatSessionScope; session: T } | null {
    if (!requestedSessionId) return null;
    const mine = mineSessions.find(
        (session) => String(session.id) === requestedSessionId,
    );
    if (mine) return { scope: 'mine', session: mine };
    if (!canViewAll) return null;
    const visible = allSessions.find(
        (session) => String(session.id) === requestedSessionId,
    );
    return visible ? { scope: 'all', session: visible } : null;
}

export function shouldRetrySessionHistoryResponse(
    status: number,
    attempt: number,
    maxAttempts: number,
): boolean {
    return status >= 500 && attempt + 1 < maxAttempts;
}

export function chatSessionRequestIdentityIsCurrent(
    requestUserId: unknown,
    requestToken: unknown,
    currentUserId: unknown,
    currentToken: unknown,
): boolean {
    return String(requestUserId ?? '') === String(currentUserId ?? '')
        && String(requestToken ?? '') === String(currentToken ?? '');
}

export function mergeLoadedHistoryWithLiveMessages<T extends { id?: unknown }>(
    historyMessages: T[],
    currentMessages: T[],
): T[] {
    const historicalIds = new Set(
        historyMessages
            .map((message) => message.id)
            .filter((id) => id != null && String(id) !== '')
            .map(String),
    );
    return [
        ...historyMessages,
        ...currentMessages.filter((message) => (
            message.id == null
            || String(message.id) === ''
            || !historicalIds.has(String(message.id))
        )),
    ];
}

export function upsertPersistedRealtimeMessage<T extends { id?: unknown }>(
    messages: T[],
    incoming: T,
    fallbackReplaceIndex: number | null = null,
): T[] {
    const incomingId = incoming.id == null ? '' : String(incoming.id);
    if (incomingId) {
        const persistedIndex = messages.findIndex(
            (message) => message.id != null && String(message.id) === incomingId,
        );
        if (persistedIndex >= 0) {
            return messages.flatMap((message, index) => {
                if (index === fallbackReplaceIndex && index !== persistedIndex) return [];
                if (index === persistedIndex) return [{ ...message, ...incoming }];
                return [message];
            });
        }
    }
    if (
        fallbackReplaceIndex != null
        && fallbackReplaceIndex >= 0
        && fallbackReplaceIndex < messages.length
    ) {
        return [
            ...messages.slice(0, fallbackReplaceIndex),
            { ...messages[fallbackReplaceIndex], ...incoming },
            ...messages.slice(fallbackReplaceIndex + 1),
        ];
    }
    return [...messages, incoming];
}
