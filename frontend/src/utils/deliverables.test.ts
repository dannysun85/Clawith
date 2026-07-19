import { describe, expect, it } from 'vitest';

import type { DeliverableRequest } from '../services/api';
import {
    deliverableLaunchMessage,
    deliverableRouteTier,
    latestPendingDeliverable,
    requestCanLaunchFromComposer,
} from './deliverables';


function request(overrides: Partial<DeliverableRequest> = {}): DeliverableRequest {
    return {
        id: 'request-1',
        tenant_id: 'tenant-1',
        created_by_user_id: 'user-1',
        agent_id: 'agent-1',
        session_id: 'session-1',
        agent_run_id: null,
        client_request_id: 'client-1',
        work_type: 'presentation',
        workflow_id: 'builtin.presentation.v1',
        workflow_version: '1.0.0',
        goal: '制作融资汇报',
        inputs: [],
        spec: {},
        tier: 'pro',
        approval_policy: ['outline', 'final'],
        output_contract: ['pptx', 'pdf'],
        status: 'ready',
        current_stage: 'brief_confirmed',
        version: 1,
        last_error_code: null,
        launched_at: null,
        completed_at: null,
        created_at: '2026-07-20T00:00:00Z',
        updated_at: '2026-07-20T00:00:00Z',
        artifacts: [],
        ...overrides,
    };
}

describe('deliverable composer selection', () => {
    it('fails closed for dry-run and unknown workflow versions', () => {
        expect(requestCanLaunchFromComposer(request())).toBe(true);
        expect(requestCanLaunchFromComposer(request({ workflow_id: 'builtin.poster.v1', work_type: 'poster' }))).toBe(false);
        expect(requestCanLaunchFromComposer(request({ workflow_version: '2.0.0' }))).toBe(false);
        expect(requestCanLaunchFromComposer(request({ status: 'running' }))).toBe(false);
    });

    it('restores only the newest non-dismissed ready request', () => {
        const running = request({ id: 'running', status: 'running', agent_run_id: 'run-1' });
        const dismissed = request({ id: 'dismissed' });
        const ready = request({ id: 'ready' });

        expect(latestPendingDeliverable([running, dismissed, ready], new Set(['dismissed']))?.id).toBe('ready');
        expect(latestPendingDeliverable([running], new Set())).toBeNull();
    });

    it('builds equivalent Chinese and English launch copy from the persisted goal', () => {
        expect(deliverableLaunchMessage(request(), true)).toContain('制作融资汇报');
        expect(deliverableLaunchMessage(request(), false)).toBe(
            'Start from the confirmed work brief: 制作融资汇报',
        );
    });

    it('keeps the persisted request tier authoritative at launch time', () => {
        expect(deliverableRouteTier(request({ tier: 'ultra' }), 'lite')).toBe('ultra');
        expect(deliverableRouteTier(request({ status: 'running', tier: 'ultra' }), 'lite')).toBe('lite');
        expect(deliverableRouteTier(null, 'pro')).toBe('pro');
    });
});
