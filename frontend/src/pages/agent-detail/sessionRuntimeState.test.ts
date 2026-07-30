import { describe, expect, it } from 'vitest';

import {
    type SessionActiveRun,
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
