import { describe, expect, it } from 'vitest';

import { runtimeErrorChatCopy, runtimeErrorDisablesReconnect } from './runtimeError';

describe('runtimeErrorChatCopy', () => {
    it('explains a revoked web-chat credential in Chinese', () => {
        expect(runtimeErrorChatCopy(
            { message: 'Web Chat authorization is no longer active', code: 'chat_authorization_revoked' },
            true,
        )).toContain('刷新页面');
    });

    it('stops reconnecting after the chat credential is revoked', () => {
        expect(runtimeErrorDisablesReconnect({
            message: 'Web Chat authorization is no longer active',
            code: 'chat_authorization_revoked',
        })).toBe(true);
    });
});
