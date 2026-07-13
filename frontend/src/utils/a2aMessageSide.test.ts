import { describe, expect, it } from 'vitest';
import { isA2AMessageLeft } from './a2aMessageSide';

describe('isA2AMessageLeft', () => {
    it('uses the backend current-agent marker instead of message role', () => {
        expect(isA2AMessageLeft({ role: 'user', is_current_agent: true }, 'agent-2')).toBe(false);
        expect(isA2AMessageLeft({ role: 'assistant', is_current_agent: false }, 'agent-2')).toBe(true);
    });

    it('uses stable sender ids when the marker is unavailable', () => {
        expect(isA2AMessageLeft({ sender_agent_id: 'agent-2' }, 'agent-2')).toBe(false);
        expect(isA2AMessageLeft({ sender_agent_id: 'agent-1' }, 'agent-2')).toBe(true);
    });

    it('keeps a legacy fallback for old API responses', () => {
        expect(isA2AMessageLeft({ sender_name: 'Bob' }, undefined, 'Alice')).toBe(true);
        expect(isA2AMessageLeft({ role: 'assistant' })).toBe(true);
    });
});
