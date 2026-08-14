import { describe, expect, it } from 'vitest';

import type { WorkItem, WorkforceTopologyNode } from '../services/api';
import {
    buildAgentWorkMap,
    computeTopologyLayout,
    filterTopologyNodes,
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
    ...overrides,
});

const workItem = (agentId: string, stage: string, updatedAt: string): WorkItem => ({
    id: `${agentId}-${stage}`,
    kind: 'task',
    title: `${stage} work`,
    intent: 'Complete the work',
    origin_type: 'web',
    executor_kind: 'agent_employee',
    executor_snapshot: {},
    work_statement: {},
    agent_id: agentId,
    agent_name: agentId,
    execution_status: 'running',
    delivery_status: 'pending',
    delivery_mode: 'task_only',
    user_stage: stage,
    artifacts: [],
    deep_link: `/agents/${agentId}/chat`,
    created_at: updatedAt,
    updated_at: updatedAt,
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
        expect(computeTopologyLayout(['1', '2'], 'auto').mode).toBe('ring');
        expect(computeTopologyLayout(
            Array.from({ length: 21 }, (_, index) => String(index)),
            'auto',
        ).mode).toBe('grid');
    });
});

describe('workforce topology projection', () => {
    it('keeps a blocked item visible ahead of newer routine work', () => {
        const work = buildAgentWorkMap([
            workItem('one', 'blocked', '2026-08-14T10:00:00Z'),
            workItem('one', 'execution', '2026-08-14T11:00:00Z'),
        ]);

        expect(work.get('one')?.group).toBe('blocked');
        expect(work.get('one')?.item.title).toBe('blocked work');
        expect(work.get('one')?.activeCount).toBe(2);
    });

    it('filters by health, work state and role text together', () => {
        const nodes = [
            node('one', { name: 'Research Lead' }),
            node('two', { name: 'Writer', role_description: 'Content', status: 'idle' }),
        ];
        const work = buildAgentWorkMap([
            workItem('one', 'approval', '2026-08-14T10:00:00Z'),
        ]);

        expect(filterTopologyNodes(nodes, {
            query: 'research',
            health: 'running',
            work: 'approval',
            workByAgent: work,
        }).map((candidate) => candidate.id)).toEqual(['one']);
        expect(filterTopologyNodes(nodes, {
            query: '',
            health: 'idle',
            work: 'no_work',
            workByAgent: work,
        }).map((candidate) => candidate.id)).toEqual(['two']);
    });
});
