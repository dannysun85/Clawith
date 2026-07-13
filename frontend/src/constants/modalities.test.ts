import { describe, expect, it } from 'vitest';

import { canonicalizeModalities, canonicalizeModality } from './modalities';

describe('modality canonicalization', () => {
    it('normalizes aliases and casing', () => {
        expect(canonicalizeModality(' Vision ')).toBe('image');
        expect(canonicalizeModality('VOICE')).toBe('audio');
        expect(canonicalizeModality(null)).toBe('text');
    });

    it('deduplicates canonical modalities while preserving order', () => {
        expect(canonicalizeModalities(['vision', 'image', 'tts', 'audio', 'video'])).toEqual([
            'image',
            'audio',
            'video',
        ]);
    });

    it('returns an empty list for missing input', () => {
        expect(canonicalizeModalities()).toEqual([]);
    });
});
