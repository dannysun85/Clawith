import { describe, expect, it } from 'vitest';

import { buildOpenClawInstruction } from './openClawInstruction';

describe('buildOpenClawInstruction', () => {
    it('uses Astra identity in the Chinese compatibility guide', () => {
        const instruction = buildOpenClawInstruction(
            'agent-key',
            true,
            'https://astra.customer.example',
        );

        expect(instruction).toContain('检查 Astra inbox');
        expect(instruction).toContain('Astra 平台');
        expect(instruction).toContain('clawith_sync');
        expect(instruction).not.toContain('Clawith platform');
        expect(instruction).not.toContain('Clawith inbox');
    });

    it('uses Astra identity in the English compatibility guide', () => {
        const instruction = buildOpenClawInstruction(
            'agent-key',
            false,
            'https://astra.customer.example',
        );

        expect(instruction).toContain('Sync with Astra platform');
        expect(instruction).toContain('Check Astra inbox');
        expect(instruction).toContain('legacy protocol skill identifier only');
        expect(instruction).not.toContain('Clawith platform');
        expect(instruction).not.toContain('Clawith inbox');
    });
});
