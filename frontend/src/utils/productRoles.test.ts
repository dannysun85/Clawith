import { describe, expect, it } from 'vitest';

import { partitionAgentRoles } from './productRoles';


describe('partitionAgentRoles', () => {
    const agents = [
        { id: 'assistant', name: 'Nova', role_description: 'Anything', product_role: 'personal_assistant' as const },
        { id: 'employee', name: 'Researcher', role_description: 'Private Assistant', product_role: 'agent_employee' as const },
        { id: 'legacy', name: 'Old companion', role_description: 'Anything', product_role: 'legacy_personal_assistant' as const },
    ];

    it('uses the onboarding relation instead of role text', () => {
        const result = partitionAgentRoles(agents, 'assistant');

        expect(result.personalAssistant?.id).toBe('assistant');
        expect(result.employees.map((agent) => agent.id)).toEqual(['employee']);
        expect(result.legacyPersonalAssistants.map((agent) => agent.id)).toEqual(['legacy']);
    });

    it('uses the unique server role while the onboarding relation is absent or stale', () => {
        expect(partitionAgentRoles(agents, null)).toEqual({
            personalAssistant: agents[0],
            legacyPersonalAssistants: [agents[2]],
            employees: [agents[1]],
        });
        expect(partitionAgentRoles(agents, 'missing')).toEqual({
            personalAssistant: agents[0],
            legacyPersonalAssistants: [agents[2]],
            employees: [agents[1]],
        });
    });

    it('never classifies editable names or role text as product identity', () => {
        const result = partitionAgentRoles([
            { id: 'same-name', name: '私人助理', role_description: 'Private Assistant' },
        ], 'missing');

        expect(result.legacyPersonalAssistants).toEqual([]);
        expect(result.employees.map((agent) => agent.id)).toEqual(['same-name']);
    });

    it('fails closed when the server marks more than one current assistant', () => {
        const ambiguous = [
            { id: 'first', product_role: 'personal_assistant' as const },
            { id: 'second', product_role: 'personal_assistant' as const },
        ];
        const result = partitionAgentRoles(ambiguous, null);

        expect(result.personalAssistant).toBeNull();
        expect(result.employees).toEqual(ambiguous);
    });
});
