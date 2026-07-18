import { describe, expect, it } from 'vitest';

import { markdownToHtml } from './MarkdownRenderer';


describe('MarkdownRenderer streaming artifact policy', () => {
    const agentId = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    const relativeVideo = `/api/agents/${agentId}/files/download?path=workspace%2Fvideos%2Fdemo.mp4`;
    const absoluteImage = `https://app.example/api/agents/${agentId}/files/download?path=workspace%2Fimages%2Fdemo.png`;

    it('does not create media elements or Agent download links while streaming', () => {
        const html = markdownToHtml(
            `![video](${relativeVideo})\n![image](${absoluteImage})\n[download](${relativeVideo})`,
            true,
        );

        expect(html).not.toContain('<video');
        expect(html).not.toContain('<audio');
        expect(html).not.toContain('<img');
        expect(html).not.toContain('<a ');
        expect(html).not.toContain('src=');
    });

    it('renders server-finalized Agent media after streaming ends', () => {
        const html = markdownToHtml(
            `![video](${relativeVideo})\n![image](${absoluteImage})`,
            false,
        );

        expect(html).toContain('<video');
        expect(html).toContain('<img');
        expect(html).toContain(relativeVideo);
        expect(html).toContain(absoluteImage);
    });

    it('does not issue external media requests while any response is streaming', () => {
        const html = markdownToHtml('![remote](https://cdn.example/unverified.png)', true);

        expect(html).not.toContain('<img');
        expect(html).not.toContain('src=');
    });
});
