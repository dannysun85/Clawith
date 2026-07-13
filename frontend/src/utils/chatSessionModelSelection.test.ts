import { describe, expect, it } from 'vitest';

import {
    resolveChatSessionModality,
    resolveChatSessionTier,
} from './chatSessionModelSelection';

describe('chat session model selection', () => {
    it('keeps the session tier when the Agent default differs', () => {
        expect(resolveChatSessionTier('ultra', 'lite', ['lite', 'pro', 'ultra'])).toBe('ultra');
    });

    it('falls back to the Agent default for a legacy session', () => {
        expect(resolveChatSessionTier(null, 'pro', ['lite', 'pro', 'ultra'])).toBe('pro');
        expect(resolveChatSessionModality(null, 'image')).toBe('image');
    });

    it('normalizes a saved modality and repairs a disallowed saved tier', () => {
        expect(resolveChatSessionModality('VOICE', 'text')).toBe('audio');
        expect(resolveChatSessionTier('ultra', 'lite', ['lite'])).toBe('lite');
    });
});
