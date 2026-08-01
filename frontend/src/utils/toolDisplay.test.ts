import { describe, expect, it } from 'vitest';

import { toolDisplayName } from './toolDisplay';

describe('toolDisplayName', () => {
    it.each([
        ['generate_image_minimax', 'Generate Image'],
        ['generate_speech_minimax', 'Generate Speech'],
        ['generate_music_minimax', 'Generate Music'],
        ['generate_video_minimax', 'Generate Video'],
        ['check_video_minimax', 'Check Video'],
        ['compose_video_audio_minimax', 'Compose Video Audio'],
    ])('hides the legacy provider suffix for %s', (toolName, expected) => {
        expect(toolDisplayName(toolName)).toBe(expected);
        expect(toolDisplayName(toolName)).not.toMatch(/minimax/i);
    });

    it('keeps the existing readable formatting for other tool identifiers', () => {
        expect(toolDisplayName('duckduckgo_search')).toBe('Duckduckgo Search');
        expect(toolDisplayName('mcp:company_lookup')).toBe('Company Lookup');
    });
});
