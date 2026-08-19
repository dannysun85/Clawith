import { describe, expect, it } from 'vitest';

import type { DeliverableRequest } from '../services/api';
import {
    deliverableApprovalBlocked,
    deliverableApprovalStatusMessage,
    deliverableLaunchMessage,
    deliverableLaunchUsesIsolatedInputs,
    deliverableRouteTier,
    isDeliverableAwaitingContinuation,
    isDeliverableStageVisible,
    latestPendingDeliverable,
    latestTrackedDeliverables,
    nextDeliverableComposerText,
    requestCanLaunchFromComposer,
    workTaskDeliverableHandoff,
} from './deliverables';
import type { WorkItem } from '../services/api';


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

function workItem(overrides: Partial<WorkItem> = {}): WorkItem {
    return {
        id: 'task-1',
        kind: 'task',
        title: '制作商业海报 Brief',
        intent: '制作商业海报',
        origin_type: 'workbench',
        executor_kind: 'personal_assistant',
        executor_snapshot: {},
        work_statement: { work_type: 'image' },
        agent_id: 'agent-1',
        agent_name: '小丽',
        task_id: 'task-1',
        task_status: 'done',
        execution_status: 'completed',
        deliverable_id: null,
        work_type: 'image',
        delivery_status: 'not_requested',
        delivery_mode: 'task_only',
        user_stage: 'completed',
        artifacts: [],
        deep_link: '/agents/agent-1/chat?task_id=task-1',
        created_at: '2026-08-04T00:00:00Z',
        updated_at: '2026-08-04T00:00:00Z',
        ...overrides,
    };
}

describe('deliverable composer selection', () => {
    it('restores a completed image Brief as a poster handoff with exact objective', () => {
        const item = workItem({
            intent: 'fallback goal',
            work_statement: {
                work_type: 'image',
                objective: [
                    '竖版 9:16 量化交易商业海报，保留完整标题和 CTA',
                    '精确文案（必须逐字一致）：',
                    '主标题：量化交易平台',
                    '副标题：智能策略・实时信号・数据驱动决策',
                    '标语：从复杂市场中，捕捉更清晰的交易方向',
                    'CTA：立即体验',
                ].join('\n'),
            },
        });

        expect(workTaskDeliverableHandoff(item)).toEqual({
            taskId: 'task-1',
            workType: 'poster',
            goal: [
                '竖版 9:16 量化交易商业海报，保留完整标题和 CTA',
                '精确文案（必须逐字一致）：',
                '主标题：量化交易平台',
                '副标题：智能策略・实时信号・数据驱动决策',
                '标语：从复杂市场中，捕捉更清晰的交易方向',
                'CTA：立即体验',
            ].join('\n'),
            specOverrides: {
                aspect_ratio: '9:16',
                exact_copy: [
                    '量化交易平台',
                    '智能策略・实时信号・数据驱动决策',
                    '从复杂市场中，捕捉更清晰的交易方向',
                    '立即体验',
                ].join('\n'),
            },
        });
    });

    it('does not guess a structured ratio or copy from ambiguous prose', () => {
        const item = workItem({
            work_statement: {
                work_type: 'image',
                objective: '同时准备 9:16 与 1:1 两个方向，标题和 CTA 后续再定',
            },
        });

        expect(workTaskDeliverableHandoff(item)?.specOverrides).toEqual({});
    });

    it('prefers the server-owned formal delivery spec over prose fallback', () => {
        const item = workItem({
            work_statement: {
                work_type: 'image',
                objective: '同时准备 9:16 与 1:1 两个方向，文案以后再定',
            },
            formal_delivery_spec: {
                aspect_ratio: '9:16',
                exact_copy: '量化交易平台\n立即体验',
            },
        });

        expect(workTaskDeliverableHandoff(item)?.specOverrides).toEqual({
            aspect_ratio: '9:16',
            exact_copy: '量化交易平台\n立即体验',
        });
    });

    it('extracts explicitly quoted poster copy from the original prose format', () => {
        const item = workItem({
            work_statement: {
                work_type: 'image',
                objective: [
                    '竖版 9:16 商业宣传海报，画面中部居中放置发光渐变立体白色大标题【量化交易平台】，',
                    '标题下方小字副标题「智能策略・实时信号・数据驱动决策」，再下方一行浅紫色小字标语「从复杂市场中，捕捉更清晰的交易方向」；',
                    '画面右下角有渐变粉紫发光圆角按钮，按钮内白色文字 “立即体验”。',
                ].join(''),
            },
        });

        expect(workTaskDeliverableHandoff(item)?.specOverrides).toEqual({
            aspect_ratio: '9:16',
            exact_copy: [
                '量化交易平台',
                '智能策略・实时信号・数据驱动决策',
                '从复杂市场中，捕捉更清晰的交易方向',
                '立即体验',
            ].join('\n'),
        });
    });

    it('preserves footer and button copy from inline poster prose', () => {
        const goal = [
            '竖版 9:16 商业宣传海报，主标题【把 AI 公司真正运行起来】；',
            '副标题【数字员工・任务协作・WorkProduct 审核】；',
            '标语【从任务到成果，企业运营真正闭环】；',
            '落款【ReefTotem｜深圳前海瑞孚图腾科技有限公司】；按钮【立即体验】。',
        ].join('');
        const item = workItem({
            work_statement: { work_type: 'image', objective: goal },
        });

        expect(workTaskDeliverableHandoff(item)?.specOverrides).toEqual({
            aspect_ratio: '9:16',
            exact_copy: [
                '把 AI 公司真正运行起来',
                '数字员工・任务协作・WorkProduct 审核',
                '从任务到成果，企业运营真正闭环',
                'ReefTotem｜深圳前海瑞孚图腾科技有限公司',
                '立即体验',
            ].join('\n'),
        });
    });

    it('does not treat button style as CTA copy', () => {
        const goal = (
            '竖版 9:16 海报，主标题【A】；副标题【B】；标语【C】；'
            + '按钮样式【渐变粉紫发光圆角】，按钮内白色文字【立即体验】。'
        );
        const item = workItem({
            work_statement: { work_type: 'image', objective: goal },
        });

        expect(workTaskDeliverableHandoff(item)?.specOverrides).toEqual({
            aspect_ratio: '9:16',
            exact_copy: 'A\nB\nC\n立即体验',
        });

        const styleOnly = workItem({
            work_statement: {
                work_type: 'image',
                objective: (
                    '竖版 9:16 海报，主标题【A】；副标题【B】；'
                    + '标语【C】；按钮样式【渐变粉紫发光圆角】。'
                ),
            },
        });

        expect(workTaskDeliverableHandoff(styleOnly)?.specOverrides.exact_copy)
            .toBe('A\nB\nC');
    });

    it('does not open formal delivery for unfinished or already-linked work', () => {
        const base = workItem();

        expect(workTaskDeliverableHandoff({ ...base, user_stage: 'execution' })).toBeNull();
        expect(workTaskDeliverableHandoff({ ...base, deliverable_id: 'delivery-1' })).toBeNull();
    });

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

    it('launches the v2 poster pipeline only with its exact version pair', () => {
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.poster.v2',
            workflow_version: '2.0.0',
            work_type: 'poster',
        }))).toBe(true);
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.poster.v2',
            workflow_version: '1.0.0',
            work_type: 'poster',
        }))).toBe(false);
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.poster.v2',
            workflow_version: '2.0.0',
            work_type: 'poster',
            status: 'running',
        }))).toBe(false);
    });

    it('launches the v2 video pipeline only with its exact version pair', () => {
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.video.v2',
            workflow_version: '2.0.0',
            work_type: 'video',
            output_contract: ['mp4'],
        }))).toBe(true);
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.video.v2',
            workflow_version: '1.0.0',
            work_type: 'video',
            output_contract: ['mp4'],
        }))).toBe(false);
    });

    it('launches the v2 video pipeline only with its exact version pair', () => {
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.video.v2',
            workflow_version: '2.0.0',
            work_type: 'video',
            output_contract: ['mp4'],
        }))).toBe(true);
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.video.v2',
            workflow_version: '1.0.0',
            work_type: 'video',
        }))).toBe(false);
        expect(requestCanLaunchFromComposer(request({
            workflow_id: 'builtin.video.v2',
            workflow_version: '2.0.0',
            work_type: 'video',
            status: 'running',
        }))).toBe(false);
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

    it('keeps a parked v2 video stage visible and re-arms the composer after the first send', () => {
        const dismissed = new Set(['video-v2']);
        const approved = request({
            id: 'video-v2',
            work_type: 'video',
            workflow_id: 'builtin.video.v2',
            workflow_version: '2.0.0',
            status: 'ready',
            current_stage: 'storyboard_approved',
            agent_run_id: null,
            output_contract: ['mp4'],
        });
        const shotReview = request({
            ...approved,
            current_stage: 'shot_review',
        });
        const composeReady = request({
            ...approved,
            current_stage: 'compose_ready',
        });

        expect(isDeliverableAwaitingContinuation(approved)).toBe(true);
        expect(isDeliverableAwaitingContinuation(shotReview)).toBe(false);
        expect(isDeliverableStageVisible(shotReview)).toBe(true);
        expect(latestPendingDeliverable([approved], dismissed)?.id).toBe('video-v2');
        expect(latestTrackedDeliverables([approved]).map((item) => item.id)).toEqual(['video-v2']);
        expect(latestTrackedDeliverables([shotReview]).map((item) => item.id)).toEqual(['video-v2']);
        expect(deliverableLaunchMessage(approved, true)).toContain('逐镜头制作');
        expect(deliverableLaunchMessage(composeReady, false)).toContain('Assemble the completed shots');
        expect(requestCanLaunchFromComposer(approved)).toBe(true);
    });

    it('drives the v2 presentation outline gate from the composer', () => {
        const approved = request({
            id: 'ppt-v2',
            work_type: 'presentation',
            workflow_id: 'builtin.presentation.v2',
            workflow_version: '2.0.0',
            status: 'ready',
            current_stage: 'outline_approved',
            agent_run_id: null,
            output_contract: ['pptx'],
        });

        expect(requestCanLaunchFromComposer(approved)).toBe(true);
        expect(requestCanLaunchFromComposer(request({
            ...approved,
            workflow_version: '1.0.0',
        }))).toBe(false);
        expect(isDeliverableAwaitingContinuation(approved)).toBe(true);
        expect(isDeliverableAwaitingContinuation(request({
            ...approved,
            current_stage: 'outline_review',
            status: 'waiting_approval',
        }))).toBe(false);
        expect(deliverableLaunchMessage(approved, true)).toContain('已批准的大纲');
        expect(deliverableLaunchMessage(approved, false)).toContain('approved outline');
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

    it('isolates a task-bound launch from unrelated chat attachments', () => {
        expect(deliverableLaunchUsesIsolatedInputs(request({
            task_id: 'task-1',
            work_type: 'poster',
            workflow_id: 'builtin.poster.v1',
        }))).toBe(true);
        expect(deliverableLaunchUsesIsolatedInputs(request({ task_id: null }))).toBe(false);
        expect(deliverableLaunchUsesIsolatedInputs(null)).toBe(false);
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
