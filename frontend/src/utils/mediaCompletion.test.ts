import { describe, expect, it } from 'vitest';

import { appendUniqueById, safeWorkspaceMediaPath } from './mediaCompletion';

describe('media completion helpers', () => {
    it('accepts only workspace-scoped paths', () => {
        expect(safeWorkspaceMediaPath('workspace/videos/demo clip.mp4')).toBe(
            'workspace/videos/demo clip.mp4',
        );
        expect(safeWorkspaceMediaPath('../secrets.mp4')).toBeNull();
        expect(safeWorkspaceMediaPath('workspace/../secrets.mp4')).toBeNull();
        expect(safeWorkspaceMediaPath('https://example.com/demo.mp4')).toBeNull();
    });

    it('deduplicates realtime messages already loaded from history', () => {
        const existing = [{ id: 'message-1', content: 'ready' }];
        expect(appendUniqueById(existing, { id: 'message-1', content: 'ready' })).toBe(existing);
        expect(appendUniqueById(existing, { id: 'message-2', content: 'next' })).toHaveLength(2);
    });
});
