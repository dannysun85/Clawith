import { describe, expect, it } from 'vitest';

import type { WorkforceTopologyNode } from '../services/api';
import {
    computeTopologyLayout,
    filterTopologyNodes,
    topologyNodeIdKey,
    topologyRectsOverlap,
} from './workforceTopology';

const node = (id: string, overrides: Partial<WorkforceTopologyNode> = {}): WorkforceTopologyNode => ({
    id,
    name: `Agent ${id}`,
    role_description: 'Research',
    status: 'running',
    tokens_used_today: 0,
    cache_read_tokens_today: 0,
    is_expired: false,
    is_system: false,
    visibility: 'company',
    can_manage: false,
    ...overrides,
});

describe('workforce topology layout', () => {
    for (const count of [5, 20, 100]) {
        it(`lays out ${count} employees without overlapping cards`, () => {
            const ids = Array.from({ length: count }, (_, index) => String(index));
            const layout = computeTopologyLayout(ids, 'auto');
            const positions = [...layout.positions.values()];

            expect(positions).toHaveLength(count);
            for (let first = 0; first < positions.length; first += 1) {
                for (let second = first + 1; second < positions.length; second += 1) {
                    expect(topologyRectsOverlap(positions[first]!, positions[second]!)).toBe(false);
                }
            }
        });
    }

    it('uses a ring for small teams and a grid for dense teams', () => {
        expect(computeTopologyLayout(
            Array.from({ length: 12 }, (_, index) => String(index)),
            'auto',
        ).mode).toBe('ring');
        expect(computeTopologyLayout(
            Array.from({ length: 13 }, (_, index) => String(index)),
            'auto',
        ).mode).toBe('grid');
    });

    it('keeps layout identity stable when refreshed node objects keep the same order', () => {
        const firstResponse = [node('one'), node('two')];
        const refreshedResponse = [
            node('one', { status: 'idle' }),
            node('two', { role_description: 'Updated role' }),
        ];

        expect(topologyNodeIdKey(firstResponse)).toBe(topologyNodeIdKey(refreshedResponse));
        expect(topologyNodeIdKey(refreshedResponse.slice().reverse()))
            .not.toBe(topologyNodeIdKey(refreshedResponse));
    });
});

describe('workforce topology projection', () => {
    it('filters by health, work state and role text together', () => {
        const nodes = [
            node('one', {
                name: 'Research Lead',
                work: {
                    id: 'work-one',
                    title: 'Approve research plan',
                    summary: 'Approval is required',
                    stage: 'approval',
                    active_count: 1,
                    recently_completed_count: 0,
                    deep_link: '/agents/one/chat?task_id=work-one',
                    updated_at: '2026-08-14T10:00:00Z',
                },
            }),
            node('two', { name: 'Writer', role_description: 'Content', status: 'idle' }),
        ];

        expect(filterTopologyNodes(nodes, {
            query: 'research',
            health: 'running',
            work: 'approval',
        }).map((candidate) => candidate.id)).toEqual(['one']);
        expect(filterTopologyNodes(nodes, {
            query: '',
            health: 'idle',
            work: 'no_work',
        }).map((candidate) => candidate.id)).toEqual(['two']);
    });
});
