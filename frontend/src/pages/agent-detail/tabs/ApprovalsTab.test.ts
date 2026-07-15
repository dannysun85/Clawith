import { describe, expect, it } from 'vitest';

import { normalizeApprovals } from './ApprovalsTab';
import {
    approvalExecutionLabel,
    approvalExecutionErrorLabel,
    approvalNeedsPolling,
    isApprovalApprovable,
} from '../../../components/ApprovalPreview';

describe('normalizeApprovals', () => {
    it('preserves the current list response', () => {
        const approvals = [{ id: 'approval-1' }];
        expect(normalizeApprovals(approvals)).toBe(approvals);
    });

    it('accepts a paginated items response', () => {
        const approvals = [{ id: 'approval-1' }];
        expect(normalizeApprovals({ items: approvals })).toBe(approvals);
    });

    it.each([undefined, null, {}, { items: null }, 'unauthorized'])(
        'returns an empty list for malformed data: %s',
        (value) => {
            expect(normalizeApprovals(value)).toEqual([]);
        },
    );
});

describe('approval safety helpers', () => {
    it('only enables approval for a verified public payload', () => {
        expect(isApprovalApprovable({ payload_state: 'verified', approvable: true })).toBe(true);
        expect(isApprovalApprovable({ payload_state: 'invalid', approvable: true })).toBe(false);
        expect(isApprovalApprovable({ payload_state: 'verified', approvable: false })).toBe(false);
    });

    it('polls while either the decision or execution is active', () => {
        expect(approvalNeedsPolling([{ status: 'pending' }])).toBe(true);
        expect(approvalNeedsPolling([{ status: 'approved', execution_status: 'executing' }])).toBe(true);
        expect(approvalNeedsPolling([{ status: 'approved', execution_status: 'succeeded' }])).toBe(false);
    });

    it('does not present approval as execution success', () => {
        const approval = {
            id: 'approval-1',
            agent_id: 'agent-1',
            action_type: 'send_message',
            status: 'approved',
            execution_status: 'pending',
        };
        expect(approvalExecutionLabel(approval, true)).toContain('等待后台执行');
        expect(approvalExecutionLabel({ ...approval, execution_status: 'ambiguous' }, true)).toContain('人工核对');
    });

    it('does not present an intermediate Douyin phase as public publish success', () => {
        const approval = {
            id: 'approval-douyin',
            agent_id: 'agent-1',
            action_type: 'douyin_publish_job',
            status: 'approved',
            execution_status: 'succeeded',
            execution_result_summary: { outcome_code: 'DouyinAcceptedPendingReview' },
        };
        expect(approvalExecutionLabel(approval, true)).toContain('等待审核');
        expect(approvalExecutionLabel(approval, true)).toContain('不代表已公开发布');
    });

    it('shows only a bounded safe terminal error code', () => {
        const approval = {
            id: 'approval-1',
            agent_id: 'agent-1',
            action_type: 'write_file',
            status: 'approved',
            execution_status: 'failed',
            execution_error_code: 'WorkspaceMutationRejected',
        };
        expect(approvalExecutionErrorLabel(approval, true)).toContain('WorkspaceMutationRejected');
        expect(approvalExecutionErrorLabel({
            ...approval,
            execution_error_code: 'Bearer customer-secret with spaces',
        }, false)).toBe('Safe error code: ExecutionFailed');
        expect(approvalExecutionErrorLabel({
            ...approval,
            execution_status: 'succeeded',
        }, false)).toBeNull();
    });
});
