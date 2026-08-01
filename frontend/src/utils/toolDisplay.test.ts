import { describe, expect, it } from 'vitest';

import {
    customerSafeAssistantText,
    customerSafeToolArgs,
    customerSafeToolResult,
    toolDisplayName,
} from './toolDisplay';

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

    it('removes provider routing fields from managed media detail arguments', () => {
        expect(customerSafeToolArgs('generate_image_minimax', {
            prompt: 'commercial poster',
            save_path: 'workspace/poster.jpg',
            provider: 'volcengine_agent_plan',
            model: 'internal-image-model',
            nested: {
                credential_id: 'secret-row',
                providerTaskId: 'provider-job',
                trace_id: 'internal-trace',
                aspect_ratio: '3:4',
            },
        })).toEqual({
            prompt: 'commercial poster',
            save_path: 'workspace/poster.jpg',
            nested: { aspect_ratio: '3:4' },
        });
    });

    it('keeps the artifact receipt while hiding internal tool and task identifiers', () => {
        const result = customerSafeToolResult(
            'generate_image_minimax',
            '✅ Image generated: workspace/poster.jpg\nTask ID: 01234567-89ab-cdef-0123-456789abcdef',
        );
        expect(result).toContain('workspace/poster.jpg');
        expect(result).not.toMatch(/minimax|task id|01234567/i);
    });

    it('redacts routing metadata from JSON tool results and covers local composition', () => {
        const result = customerSafeToolResult('compose_video_audio', JSON.stringify({
            output_path: 'workspace/final.mp4',
            providerName: 'internal-compositor',
            model_id: 'internal-model',
            request_id: 'internal-request',
            nested: { task_id: 'internal-task', duration_seconds: 6 },
        }));
        expect(toolDisplayName('compose_video_audio')).toBe('Compose Video Audio');
        expect(JSON.parse(result)).toEqual({
            output_path: 'workspace/final.mp4',
            nested: { duration_seconds: 6 },
        });
    });

    it('redacts markdown routing lines in historical receipts', () => {
        const receipt = customerSafeAssistantText([
            '视频制作完成：workspace/final.mp4',
            '- **Provider Name**: volcengine_agent_plan',
            '- **Model ID**：seedance-internal',
            '- **Request ID**: internal-request',
        ].join('\n'));
        expect(receipt).toBe('视频制作完成：workspace/final.mp4');
    });

    it('sanitizes historical media assistant receipts without rewriting normal comparisons', () => {
        const receipt = customerSafeAssistantText([
            '语音文件已生成完成',
            '音频文件路径：workspace/voice.mp3',
            '提供方：volcengine_agent_plan',
            '模型：doubao-seed-tts-2.0',
            '工具：generate_speech_minimax',
        ].join('\n'));
        expect(receipt).toContain('workspace/voice.mp3');
        expect(receipt).toContain('Generate Speech');
        expect(receipt).not.toMatch(/volcengine|doubao-seed|minimax/i);

        const comparison = 'Provider: Volcengine\nModel: Seedance\n用于供应商比较。';
        expect(customerSafeAssistantText(comparison)).toBe(comparison);
    });
});
