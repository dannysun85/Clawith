import { describe, expect, it } from 'vitest';

import {
    resolveChatSessionModality,
    resolveChatSessionTier,
    resolveOutboundChatRoute,
    shouldApplyChatTierPreferenceResponse,
} from './chatSessionModelSelection';

describe('chat session model selection', () => {
    it('keeps the session tier when the Agent default differs', () => {
        expect(resolveChatSessionTier('ultra', 'lite', ['lite', 'pro', 'ultra'])).toBe('ultra');
    });

    it('keeps the latest user choice when navigating across Agents', () => {
        expect(resolveChatSessionTier('lite', 'pro', ['lite', 'pro', 'ultra'], 'ultra')).toBe('ultra');
    });

    it('falls back to the Agent default for a legacy session', () => {
        expect(resolveChatSessionTier(null, 'pro', ['lite', 'pro', 'ultra'])).toBe('pro');
        expect(resolveChatSessionModality(null, 'image')).toBe('image');
    });

    it('normalizes a saved modality and repairs a disallowed saved tier', () => {
        expect(resolveChatSessionModality('VOICE', 'text')).toBe('audio');
        expect(resolveChatSessionTier('ultra', 'lite', ['lite'])).toBe('lite');
        expect(resolveChatSessionTier('lite', 'lite', ['lite'], 'ultra')).toBe('lite');
    });

    it('keeps attachment media routing request-scoped', () => {
        expect(resolveOutboundChatRoute('text', true, false)).toEqual({
            modality: 'image',
            ephemeral: true,
        });
        expect(resolveOutboundChatRoute('text', true, true)).toEqual({
            modality: 'video',
            ephemeral: true,
        });
        expect(resolveOutboundChatRoute('TEXT', false, false)).toEqual({
            modality: 'text',
            ephemeral: false,
        });
    });

    it('rejects delayed or older preference responses', () => {
        expect(shouldApplyChatTierPreferenceResponse(2, 3, 'user-a', 'user-a', 8, 7)).toBe(false);
        expect(shouldApplyChatTierPreferenceResponse(3, 3, 'user-a', 'user-a', 6, 7)).toBe(false);
        expect(shouldApplyChatTierPreferenceResponse(3, 3, 'user-a', 'user-a', 8, 7)).toBe(true);
    });

    it('rejects a delayed success response after the login user changes', () => {
        expect(shouldApplyChatTierPreferenceResponse(3, 3, 'user-a', 'user-b', 8, 1)).toBe(false);
    });

    it('rejects a delayed conflict response after the login user changes', () => {
        expect(shouldApplyChatTierPreferenceResponse(3, 3, 'user-a', 'user-b', 9, 1)).toBe(false);
    });
});
