import { describe, expect, it } from 'vitest';

import { partitionAgentRoles } from './productRoles';


describe('partitionAgentRoles', () => {
    const agents = [
        { id: 'assistant', name: 'Nova', role_description: 'Anything' },
        { id: 'employee', name: 'Researcher', role_description: 'Private Assistant' },
    ];

    it('uses the onboarding relation instead of role text', () => {
        const result = partitionAgentRoles(agents, 'assistant');

        expect(result.personalAssistant?.id).toBe('assistant');
        expect(result.employees.map((agent) => agent.id)).toEqual(['employee']);
    });

    it('does not guess an assistant when the relation is absent or stale', () => {
        expect(partitionAgentRoles(agents, null)).toEqual({
            personalAssistant: null,
            employees: agents,
        });
        expect(partitionAgentRoles(agents, 'missing')).toEqual({
            personalAssistant: null,
            employees: agents,
        });
    });
});
