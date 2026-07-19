import { describe, expect, it } from 'vitest';

import {
    appendUniqueById,
    safeMediaCompletionTool,
    safeWorkspaceMediaPath,
    workspaceMediaPathFromArtifactRefs,
} from './mediaCompletion';

describe('media completion helpers', () => {
    it('accepts only workspace-scoped paths', () => {
        expect(safeWorkspaceMediaPath('workspace/videos/demo clip.mp4')).toBe(
            'workspace/videos/demo clip.mp4',
        );
        expect(safeWorkspaceMediaPath('../secrets.mp4')).toBeNull();
        expect(safeWorkspaceMediaPath('workspace/../secrets.mp4')).toBeNull();
        expect(safeWorkspaceMediaPath('https://example.com/demo.mp4')).toBeNull();
    });

    it('accepts only workspace artifacts owned by the active agent', () => {
        expect(workspaceMediaPathFromArtifactRefs(
            ['workspace://agent-1/workspace/videos/demo.mp4'],
            'agent-1',
        )).toBe('workspace/videos/demo.mp4');
        expect(workspaceMediaPathFromArtifactRefs(
            ['workspace://agent-2/workspace/videos/demo.mp4'],
            'agent-1',
        )).toBeNull();
        expect(workspaceMediaPathFromArtifactRefs(
            ['workspace://agent-1/workspace/../secret.mp4'],
            'agent-1',
        )).toBeNull();
    });

    it('deduplicates realtime messages already loaded from history', () => {
        const existing = [{ id: 'message-1', content: 'ready' }];
        expect(appendUniqueById(existing, { id: 'message-1', content: 'ready' })).toBe(existing);
        expect(appendUniqueById(existing, { id: 'message-2', content: 'next' })).toHaveLength(2);
    });

    it.each([
        ['image', 'generate_image_minimax'],
        ['audio', 'generate_speech_minimax'],
        ['music', 'generate_music_minimax'],
        ['video', 'generate_video_minimax'],
    ])('maps %s completion events to the canonical tool', (modality, expected) => {
        expect(safeMediaCompletionTool(modality, expected)).toBe(expected);
        expect(safeMediaCompletionTool(modality, 'generate_video_minimax')).toBe(expected);
    });

    it('uses a generic activity for an unknown completion modality', () => {
        expect(safeMediaCompletionTool('document', 'generate_video_minimax')).toBe('media_generation');
    });
});
