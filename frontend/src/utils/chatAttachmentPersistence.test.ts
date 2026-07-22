import { describe, expect, it } from 'vitest';

import {
    attachmentStoragePath,
    attachmentStorageBasename,
    parsePersistedChatAttachments,
} from './chatAttachmentPersistence';

describe('parsePersistedChatAttachments', () => {
    it('keeps the display name separate from the collision-safe stored name', () => {
        expect(parsePersistedChatAttachments(
            '[file:slogan_4875d85abdb4.png]\n[Attachment: slogan.png]\n请描述图片',
        )).toEqual({
            content: '请描述图片',
            displayFileNames: ['slogan.png'],
            storageFileNames: ['slogan_4875d85abdb4.png'],
            storageFilePaths: ['workspace/uploads/slogan_4875d85abdb4.png'],
        });
    });

    it('parses multiple durable prefixes and display markers without leaking either into the prompt', () => {
        expect(parsePersistedChatAttachments(
            '[file:first_111111111111.png]\n[file:demo_222222222222.mp4]\n'
            + '[Attachment: first.png] [Attachment: demo.mp4]\n分析附件',
        )).toEqual({
            content: '分析附件',
            displayFileNames: ['first.png', 'demo.mp4'],
            storageFileNames: ['first_111111111111.png', 'demo_222222222222.mp4'],
            storageFilePaths: [
                'workspace/uploads/first_111111111111.png',
                'workspace/uploads/demo_222222222222.mp4',
            ],
        });
    });

    it('preserves repeated display names for distinct stored attachments', () => {
        expect(parsePersistedChatAttachments(
            '[file:image_111111111111.png]\n[file:image_222222222222.png]\n'
            + '[Attachment: image.png] [Attachment: image.png]\n比较两张图片',
        )).toEqual({
            content: '比较两张图片',
            displayFileNames: ['image.png', 'image.png'],
            storageFileNames: ['image_111111111111.png', 'image_222222222222.png'],
            storageFilePaths: [
                'workspace/uploads/image_111111111111.png',
                'workspace/uploads/image_222222222222.png',
            ],
        });
    });

    it('preserves legacy file-only messages and ordinary chat content', () => {
        expect(parsePersistedChatAttachments('[file:legacy.png]\nhello')).toEqual({
            content: 'hello',
            displayFileNames: ['legacy.png'],
            storageFileNames: ['legacy.png'],
            storageFilePaths: ['workspace/uploads/legacy.png'],
        });
        expect(parsePersistedChatAttachments('hello')).toEqual({
            content: 'hello',
            displayFileNames: [],
            storageFileNames: [],
            storageFilePaths: [],
        });
    });

    it('restores a generated Workspace image to its real directory', () => {
        expect(parsePersistedChatAttachments(
            '[file:icepoint_poster_v1_702b48653e96.jpg]\n'
            + '[Attachment: posters/icepoint_poster_v1_702b48653e96.jpg]\n直接给我展示出来',
        )).toEqual({
            content: '直接给我展示出来',
            displayFileNames: ['posters/icepoint_poster_v1_702b48653e96.jpg'],
            storageFileNames: ['icepoint_poster_v1_702b48653e96.jpg'],
            storageFilePaths: ['workspace/posters/icepoint_poster_v1_702b48653e96.jpg'],
        });
    });
});

describe('attachmentStorageBasename', () => {
    it('uses the authoritative workspace object basename and falls back safely', () => {
        expect(attachmentStorageBasename(
            'workspace/uploads/slogan_4875d85abdb4.png',
            'slogan.png',
        )).toBe('slogan_4875d85abdb4.png');
        expect(attachmentStorageBasename(undefined, 'folder\\demo.mp4')).toBe('demo.mp4');
        expect(attachmentStorageBasename(
            'workspace/uploads/report,final_4875d85abdb4.png',
            'report,final.png',
        )).toBe('report,final_4875d85abdb4.png');
    });
});

describe('attachmentStoragePath', () => {
    it('keeps normal uploads under the uploads directory', () => {
        expect(attachmentStoragePath('stored.png', 'customer.png')).toBe(
            'workspace/uploads/stored.png',
        );
    });

    it('accepts only a matching safe Workspace-relative path', () => {
        expect(attachmentStoragePath('poster.jpg', 'posters/poster.jpg')).toBe(
            'workspace/posters/poster.jpg',
        );
        expect(attachmentStoragePath('poster.jpg', 'workspace/posters/poster.jpg')).toBe(
            'workspace/posters/poster.jpg',
        );
        expect(attachmentStoragePath('poster.jpg', '../secrets/poster.jpg')).toBe(
            'workspace/uploads/poster.jpg',
        );
        expect(attachmentStoragePath('poster.jpg', 'posters/other.jpg')).toBe(
            'workspace/uploads/poster.jpg',
        );
        expect(attachmentStoragePath('poster.jpg', 'https://evil.invalid/poster.jpg')).toBe(
            'workspace/uploads/poster.jpg',
        );
    });
});
