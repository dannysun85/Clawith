import { describe, expect, it } from 'vitest';

import type { DeliverableRequest } from '../services/api';
import {
    deliverableApprovalBlocked,
    deliverableApprovalStatusMessage,
    deliverableLaunchMessage,
    deliverableRouteTier,
    latestPendingDeliverable,
    latestTrackedDeliverables,
    nextDeliverableComposerText,
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
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.video.v1',
            work_type: 'video',
            output_contract: ['mp4'],
        }))).toBe(true);
        expect(requestCanLaunchFromComposer(request({ workflow_id: 'builtin.poster.v1', work_type: 'poster' }))).toBe(true);
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

    it('shows the latest launched non-cancelled work for each deliverable type', () => {
        const ready = request({ id: 'ready' });
        const cancelled = request({ id: 'cancelled', status: 'cancelled', agent_run_id: 'run-cancelled' });
        const newestPresentation = request({ id: 'new-ppt', status: 'succeeded', agent_run_id: 'run-new-ppt' });
        const olderPresentation = request({ id: 'old-ppt', status: 'succeeded', agent_run_id: 'run-old-ppt' });
        const video = request({
            id: 'video',
            work_type: 'video',
            workflow_id: 'builtin.video.v1',
            status: 'waiting_approval',
            agent_run_id: 'run-video',
        });

        expect(latestTrackedDeliverables([
            ready,
            cancelled,
            newestPresentation,
            olderPresentation,
            video,
        ]).map((item) => item.id)).toEqual(['new-ppt', 'video']);
        expect(latestTrackedDeliverables([ready, cancelled])).toEqual([]);
    });

    it('builds equivalent Chinese and English launch copy from the persisted goal', () => {
        expect(deliverableLaunchMessage(request(), true)).toContain('制作融资汇报');
        expect(deliverableLaunchMessage(request(), false)).toBe(
            'Start from the confirmed work brief: 制作融资汇报',
        );
    });

    it('replaces stale generated copy when a newer brief is saved', () => {
        const previous = request({ id: 'poster-1', work_type: 'poster', workflow_id: 'builtin.poster.v1', goal: '制作一张商品海报' });
        const next = request({ id: 'ppt-1', goal: '制作一份八页商业提案' });
        const previousText = deliverableLaunchMessage(previous, true);

        expect(nextDeliverableComposerText('', next, previous, true)).toBe(
            deliverableLaunchMessage(next, true),
        );
        expect(nextDeliverableComposerText(previousText, next, previous, true)).toBe(
            deliverableLaunchMessage(next, true),
        );
        expect(nextDeliverableComposerText('我已经手动补充的要求', next, previous, true)).toBe(
            '我已经手动补充的要求',
        );
    });

    it('keeps the persisted request tier authoritative at launch time', () => {
        expect(deliverableRouteTier(request({ tier: 'ultra' }), 'lite')).toBe('ultra');
        expect(deliverableRouteTier(request({ status: 'running', tier: 'ultra' }), 'lite')).toBe('lite');
        expect(deliverableRouteTier(null, 'pro')).toBe('pro');
    });

    it('keeps legacy output review approvable when readiness is absent', () => {
        const legacy = request({
            status: 'waiting_approval',
            current_stage: 'output_review',
        });

        expect(deliverableApprovalBlocked(legacy)).toBe(false);
        expect(deliverableApprovalStatusMessage(legacy, true)).toContain('结构校验');
    });

    it('blocks approval copy when the hash-bound quality receipt failed', () => {
        const blocked = request({
            status: 'waiting_approval',
            current_stage: 'output_review',
            approval_readiness: {
                approvable: false,
                quality_gate_required: false,
                quality_status: 'blocked',
                blockers: ['deliverable_creative_quality_blocked'],
                receipt_ref: 'receipt-1',
            },
        });

        expect(deliverableApprovalBlocked(blocked)).toBe(true);
        expect(deliverableApprovalStatusMessage(blocked, true)).toContain('明确问题');
        expect(deliverableApprovalStatusMessage(blocked, false)).toContain('cannot be approved');
    });
});
