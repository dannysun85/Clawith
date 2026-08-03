import { describe, expect, it } from 'vitest';

import {
    type SessionActiveRun,
    isMediaDeliveryPending,
    waitingRunResumePayload,
} from './sessionRuntimeState';

const activeRun = (overrides: Partial<SessionActiveRun> = {}): SessionActiveRun => ({
    runId: 'run-1',
    threadId: 'session-1',
    sessionId: 'session-1',
    status: 'waiting_user',
    waitingType: 'user',
    waitingReason: 'Approve the outline',
    correlationId: 'correlation-1',
    modelStepCount: 3,
    canResume: true,
    canCancel: true,
    pendingToolReconciliations: [],
    ...overrides,
});

describe('waitingRunResumePayload', () => {
    it('includes the exact waiting run identity and correlation', () => {
        expect(waitingRunResumePayload(activeRun())).toEqual({
            resumeRunId: 'run-1',
            resumeCorrelationId: 'correlation-1',
        });
    });

    it('fails closed until the waiting run is resumable', () => {
        expect(waitingRunResumePayload(activeRun({ canResume: false }))).toEqual({});
        expect(waitingRunResumePayload(activeRun({ correlationId: null }))).toEqual({});
    });

    it('does not attach resume identity to a non-waiting send', () => {
        expect(waitingRunResumePayload(activeRun({ status: 'running' }))).toEqual({});
        expect(waitingRunResumePayload(null)).toEqual({});
    });
});

describe('isMediaDeliveryPending', () => {
    const reconciliation = (errorCode: string | null) => ({
        executionId: 'execution-1',
        toolCallId: 'tool-call-1',
        toolName: 'generate_image_minimax',
        errorCode,
        canReconcile: false,
    });

    it('classifies durable image delivery as background processing', () => {
        expect(isMediaDeliveryPending(reconciliation('media_image_delivery_pending'))).toBe(true);
        expect(isMediaDeliveryPending(reconciliation('media_image_recovery_pending'))).toBe(true);
        expect(isMediaDeliveryPending(reconciliation('media_video_recovery_pending'))).toBe(true);
    });

    it('does not hide a genuinely unknown tool reconciliation', () => {
        expect(isMediaDeliveryPending(reconciliation('tool_outcome_unknown'))).toBe(false);
        expect(isMediaDeliveryPending(reconciliation(null))).toBe(false);
    });
});
