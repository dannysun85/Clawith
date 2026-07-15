import { describe, expect, it } from 'vitest';

import {
    activeSessionMatchesRequestedSession,
    chatSessionRequestIdentityIsCurrent,
    chatHistoryIsReady,
    mergeLoadedHistoryWithLiveMessages,
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
