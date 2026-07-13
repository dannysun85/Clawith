import { describe, expect, it } from 'vitest';

import { mediaUrlExtension } from './mediaUrl';

describe('mediaUrlExtension', () => {
    it('detects media extensions in direct URLs', () => {
        expect(mediaUrlExtension('/workspace/video.mp4')).toBe('mp4');
        expect(mediaUrlExtension('/workspace/audio.wav?token=secret')).toBe('wav');
    });

    it('detects media extensions carried by authenticated download path parameters', () => {
        expect(mediaUrlExtension(
            '/api/agents/agent-id/files/download?path=workspace/videos/demo.mp4&token=secret',
        )).toBe('mp4');
    });
});
