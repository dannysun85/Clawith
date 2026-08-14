import { describe, expect, it } from 'vitest';

import {
    activeSessionMatchesRequestedSession,
    awaitCurrentChatPagination,
    chatPaginationRequestIdentityIsCurrent,
    chatSessionRequestIdentityIsCurrent,
    chatHistoryIsReady,
    isExternalChannelSession,
    mergeLoadedHistoryWithLiveMessages,
    resolveChatHistoryCursor,
    resolveRequestedChatSession,
    searchForSelectedChatSession,
    shouldRetrySessionHistoryResponse,
    upsertPersistedRealtimeMessage,
} from './agentChatLifecycle';

describe('agent chat lifecycle guards', () => {
    it('waits for the requested session before restoring a workspace deep link', () => {
        expect(activeSessionMatchesRequestedSession('session-2', null)).toBe(false);
        expect(activeSessionMatchesRequestedSession('session-2', 'session-1')).toBe(false);
        expect(activeSessionMatchesRequestedSession('session-2', 'session-2')).toBe(true);
        expect(activeSessionMatchesRequestedSession('', 'session-1')).toBe(true);
    });

    it('only considers history ready for the active agent and session', () => {
        expect(chatHistoryIsReady(null, 'agent-1', 'session-1')).toBe(false);
        expect(chatHistoryIsReady('agent-1:session-1', 'agent-1', 'session-2')).toBe(false);
        expect(chatHistoryIsReady('agent-1:session-1', 'agent-2', 'session-1')).toBe(false);
        expect(chatHistoryIsReady('agent-1:session-1', 'agent-1', 'session-1')).toBe(true);
    });

    it('replaces stale notification parameters after an explicit session switch', () => {
        expect(searchForSelectedChatSession(
            '?workspace_path=workspace%2Fvideos%2Fdemo.mp4&session_id=old&message_id=message-1&tab=chat',
            'new',
        )).toBe('?session_id=new&tab=chat');
    });

    it('resolves requested sessions within the viewer authorized scope', () => {
        const mine = [{ id: 'mine-1' }];
        const all = [{ id: 'mine-1' }, { id: 'other-1' }];
        expect(resolveRequestedChatSession('mine-1', mine, all, true)).toEqual({
            scope: 'mine',
            session: mine[0],
        });
        expect(resolveRequestedChatSession('other-1', mine, all, true)).toEqual({
            scope: 'all',
            session: all[1],
        });
        expect(resolveRequestedChatSession('other-1', mine, all, false)).toBeNull();
        expect(resolveRequestedChatSession('missing', mine, all, true)).toBeNull();
    });

    it('identifies externally sourced channel sessions without mixing web or agent chats', () => {
        expect(isExternalChannelSession({ source_channel: 'wechat' })).toBe(true);
        expect(isExternalChannelSession({ source_channel: 'Microsoft_Teams' })).toBe(true);
        expect(isExternalChannelSession({ source_channel: 'direct' })).toBe(false);
        expect(isExternalChannelSession({ source_channel: 'agent' })).toBe(false);
        expect(isExternalChannelSession({})).toBe(false);
    });

    it('retries only server-side history failures while attempts remain', () => {
        expect(shouldRetrySessionHistoryResponse(503, 0, 3)).toBe(true);
        expect(shouldRetrySessionHistoryResponse(503, 1, 3)).toBe(true);
        expect(shouldRetrySessionHistoryResponse(503, 2, 3)).toBe(false);
        expect(shouldRetrySessionHistoryResponse(404, 0, 3)).toBe(false);
    });

    it('rejects a session-list response after the login identity or token changes', () => {
        expect(chatSessionRequestIdentityIsCurrent('user-1', 'token-1', 'user-1', 'token-1')).toBe(true);
        expect(chatSessionRequestIdentityIsCurrent('user-1', 'token-1', 'user-2', 'token-1')).toBe(false);
        expect(chatSessionRequestIdentityIsCurrent('user-1', 'token-1', 'user-1', 'token-2')).toBe(false);
    });

    it('drops a delayed pagination response after account or session replacement', async () => {
        let resolvePage: (value: string[]) => void = () => undefined;
        const delayedPage = new Promise<string[]>((resolve) => {
            resolvePage = resolve;
        });
        const request = {
            userId: 'user-1', token: 'token-1', agentId: 'agent-1', sessionId: 'session-1',
        };
        let current = { ...request };
        const accepted = awaitCurrentChatPagination(
            () => delayedPage,
            request,
            () => current,
        );

        current = { ...current, userId: 'user-2', sessionId: 'session-2' };
        resolvePage(['private-old-page']);

        await expect(accepted).resolves.toBeNull();
        expect(chatPaginationRequestIdentityIsCurrent(request, current)).toBe(false);
    });

    it('uses server history headers instead of synthetic inline-part cursors', () => {
        const headers = new Headers({
            'X-History-Next-Before': '2026-07-16T10:00:00+00:00',
            'X-History-Next-Before-Id': 'source-10',
            'X-History-Has-More': 'true',
        });

        expect(resolveChatHistoryCursor(headers, [{
            id: 'source-10:part:0',
            created_at: 'synthetic-time',
            source_message_id: 'source-fallback',
            source_created_at: 'fallback-time',
        }], 20)).toEqual({
            before: '2026-07-16T10:00:00+00:00',
            beforeId: 'source-10',
            hasMore: true,
        });
    });

    it('falls back to source-message identity for expanded legacy responses', () => {
        expect(resolveChatHistoryCursor(new Headers(), [{
            id: 'message-20:part:0',
            created_at: '2026-07-16T10:00:00+00:00',
            source_message_id: 'message-20',
            source_created_at: '2026-07-16T09:59:59+00:00',
        }], 1)).toEqual({
            before: '2026-07-16T09:59:59+00:00',
            beforeId: 'message-20',
            hasMore: true,
        });
    });

    it('honors the raw-row has-more header when one row expands into many parts', () => {
        const headers = new Headers({ 'X-History-Has-More': 'false' });
        const expanded = Array.from({ length: 12 }, (_, index) => ({
            id: `message-1:part:${index}`,
            source_message_id: 'message-1',
            source_created_at: '2026-07-16T10:00:00+00:00',
        }));

        expect(resolveChatHistoryCursor(headers, expanded, 10).hasMore).toBe(false);
    });

    it('preserves the id tie-breaker across pages with identical timestamps', () => {
        const first = resolveChatHistoryCursor(new Headers({
            'X-History-Next-Before': '2026-07-16T10:00:00+00:00',
            'X-History-Next-Before-Id': 'message-b',
            'X-History-Has-More': 'true',
        }), [{ id: 'message-b' }], 1);
        const second = resolveChatHistoryCursor(new Headers({
            'X-History-Next-Before': '2026-07-16T10:00:00+00:00',
            'X-History-Next-Before-Id': 'message-a',
            'X-History-Has-More': 'false',
        }), [{ id: 'message-a' }], 1);

        expect(first.before).toBe(second.before);
        expect(first.beforeId).toBe('message-b');
        expect(second.beforeId).toBe('message-a');
        expect(second.hasMore).toBe(false);
    });

    it('keeps realtime messages received while history is loading without duplicating ids', () => {
        const history = [
            { id: 'history-1', content: 'first' },
            { id: 'shared', content: 'persisted copy' },
        ];
        const live = [
            { id: 'shared', content: 'websocket copy' },
            { id: 'live-1', content: 'arrived during load' },
            { content: 'local transient message' },
        ];

        expect(mergeLoadedHistoryWithLiveMessages(history, live)).toEqual([
            history[0],
            history[1],
            live[1],
            live[2],
        ]);
    });

    it('deduplicates a client-id user message in either history race order', () => {
        type Message = { id?: string; content: string };
        const history: Message[] = [{ id: 'persisted-1', content: 'hello' }];
        const optimistic: Message[] = [{ id: 'persisted-1', content: 'hello' }];

        const hydrated = mergeLoadedHistoryWithLiveMessages(history, optimistic);
        expect(hydrated).toEqual(history);
        expect(hydrated).toHaveLength(1);
    });

    it('converges a persisted tool call for history-first and realtime-first delivery', () => {
        type ToolMessage = { id?: string; status: 'running' | 'done'; result?: string };
        const persisted: ToolMessage = { id: 'tool-message-1', status: 'done', result: 'ready' };
        const realtime: ToolMessage = { id: 'tool-message-1', status: 'done', result: 'ready' };

        const running: ToolMessage = { status: 'running' };
        expect(upsertPersistedRealtimeMessage([persisted, running], realtime, 1)).toEqual([persisted]);

        const finalized = upsertPersistedRealtimeMessage([running], realtime, 0);
        expect(finalized).toEqual([persisted]);
        expect(mergeLoadedHistoryWithLiveMessages([persisted], finalized)).toEqual([persisted]);
    });

    it('converges a persisted trigger notification for either delivery order', () => {
        const persisted = [{ id: 'trigger-message-1', content: 'scheduled result' }];
        const realtime = { id: 'trigger-message-1', content: 'scheduled result' };

        expect(upsertPersistedRealtimeMessage(persisted, realtime)).toEqual(persisted);
        expect(mergeLoadedHistoryWithLiveMessages(persisted, [realtime])).toEqual(persisted);
    });
});
