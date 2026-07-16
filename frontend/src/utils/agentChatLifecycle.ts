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

export type ChatPaginationRequestIdentity = {
    userId: unknown;
    token: unknown;
    agentId: unknown;
    sessionId: unknown;
};

export function chatPaginationRequestIdentityIsCurrent(
    request: ChatPaginationRequestIdentity,
    current: ChatPaginationRequestIdentity,
): boolean {
    return chatSessionRequestIdentityIsCurrent(
        request.userId,
        request.token,
        current.userId,
        current.token,
    )
        && String(request.agentId ?? '') === String(current.agentId ?? '')
        && String(request.sessionId ?? '') === String(current.sessionId ?? '');
}

export async function awaitCurrentChatPagination<T>(
    load: () => Promise<T>,
    request: ChatPaginationRequestIdentity,
    getCurrent: () => ChatPaginationRequestIdentity,
    signal?: AbortSignal,
): Promise<T | null> {
    const value = await load();
    if (signal?.aborted) return null;
    return chatPaginationRequestIdentityIsCurrent(request, getCurrent())
        ? value
        : null;
}

export type ChatHistoryCursor = {
    before: string | null;
    beforeId: string | null;
    hasMore: boolean;
};

export function resolveChatHistoryCursor(
    headers: Pick<Headers, 'get'>,
    messages: Array<{
        id?: unknown;
        created_at?: unknown;
        source_message_id?: unknown;
        source_created_at?: unknown;
    }>,
    legacyPageSize: number,
): ChatHistoryCursor {
    const oldest = messages[0];
    const before = headers.get('X-History-Next-Before')
        || (oldest?.source_created_at == null ? '' : String(oldest.source_created_at))
        || (oldest?.created_at == null ? '' : String(oldest.created_at));
    const beforeId = headers.get('X-History-Next-Before-Id')
        || (oldest?.source_message_id == null ? '' : String(oldest.source_message_id))
        || (oldest?.id == null ? '' : String(oldest.id));
    const hasMoreHeader = headers.get('X-History-Has-More');
    return {
        before: before || null,
        beforeId: beforeId || null,
        hasMore: hasMoreHeader == null
            ? messages.length >= legacyPageSize
            : hasMoreHeader === 'true',
    };
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
