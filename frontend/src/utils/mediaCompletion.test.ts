import { describe, expect, it } from 'vitest';

import {
    appendUniqueById,
    latestCompletedWorkspaceMediaPath,
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

    it('recovers the latest successful media artifact from durable tool history', () => {
        expect(latestCompletedWorkspaceMediaPath([
            {
                role: 'tool_call',
                toolName: 'generate_image_minimax',
                toolStatus: 'done',
                toolResult: '✅ Saved to workspace/generated/hero image.png',
            },
            {
                role: 'tool_call',
                toolName: 'generate_video_minimax',
                toolStatus: 'done',
                toolResult: '视频已完成：workspace/deliverables/campaign/final.mp4\n可直接预览。',
            },
        ])).toBe('workspace/deliverables/campaign/final.mp4');
    });

    it('recovers an asynchronously reconciled image artifact', () => {
        expect(latestCompletedWorkspaceMediaPath([
            {
                role: 'tool_call',
                toolName: 'check_image_generation',
                toolStatus: 'done',
                toolResult: '✅ Image delivered: workspace/images/poster.png',
            },
        ])).toBe('workspace/images/poster.png');
    });

    it('ignores unfinished, non-media, and unsafe durable tool rows', () => {
        expect(latestCompletedWorkspaceMediaPath([
            {
                role: 'tool_call',
                toolName: 'generate_video_minimax',
                toolStatus: 'running',
                toolResult: 'workspace/videos/running.mp4',
            },
            {
                role: 'tool_call',
                toolName: 'write_file',
                toolStatus: 'done',
                toolResult: 'workspace/videos/not-media-tool.mp4',
            },
            {
                role: 'tool_call',
                toolName: 'generate_music_minimax',
                toolStatus: 'done',
                toolResult: 'workspace/../secret.mp3',
            },
        ])).toBeNull();
    });

    it('uses the final assistant receipt to choose the delivered artifact within one run', () => {
        expect(latestCompletedWorkspaceMediaPath([
            {
                role: 'tool_call',
                toolName: 'generate_video_minimax',
                toolStatus: 'done',
                toolResult: 'workspace/videos/commercial.mp4',
            },
            {
                role: 'tool_call',
                toolName: 'generate_image_minimax',
                toolStatus: 'done',
                toolResult: 'workspace/images/first-frame.png',
            },
            {
                role: 'assistant',
                content: '视频已完成：`workspace/videos/commercial.mp4`',
            },
        ])).toBe('workspace/videos/commercial.mp4');
    });

    it('does not trust an assistant-only media path without a successful tool receipt', () => {
        expect(latestCompletedWorkspaceMediaPath([
            {
                role: 'assistant',
                content: '已生成：workspace/videos/fabricated.mp4',
            },
        ])).toBeNull();
    });
});
