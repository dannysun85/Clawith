import type {
    DeliverableRequest,
    DeliverableWorkType,
    WorkItem,
} from '../services/api';

const COMPOSER_LAUNCHABLE_WORKFLOWS = new Set([
    'builtin.poster.v1@1.0.0',
    'builtin.presentation.v1@1.0.0',
    'builtin.video.v1@1.0.0',
]);

const WORK_TASK_TO_DELIVERABLE_TYPE: Partial<Record<string, DeliverableWorkType>> = {
    image: 'poster',
    video: 'video',
    presentation: 'presentation',
    document: 'report',
};

export type WorkTaskDeliverableHandoff = {
    taskId: string;
    workType: DeliverableWorkType;
    goal: string;
};

export function workTaskDeliverableHandoff(
    item: WorkItem | null | undefined,
): WorkTaskDeliverableHandoff | null {
    if (
        !item
        || item.kind !== 'task'
        || item.delivery_mode !== 'task_only'
        || item.user_stage !== 'completed'
        || item.deliverable_id
        || !item.task_id
    ) {
        return null;
    }
    const statementWorkType = item.work_statement?.work_type;
    const sourceWorkType = typeof statementWorkType === 'string'
        ? statementWorkType
        : item.work_type;
    const workType = sourceWorkType
        ? WORK_TASK_TO_DELIVERABLE_TYPE[sourceWorkType]
        : undefined;
    const statementObjective = item.work_statement?.objective;
    const goal = typeof statementObjective === 'string' && statementObjective.trim()
        ? statementObjective.trim()
        : item.intent.trim();
    if (!workType || goal.length < 3) return null;
    return {
        taskId: item.task_id,
        workType,
        goal,
    };
}

export function requestCanLaunchFromComposer(request: DeliverableRequest): boolean {
    return request.status === 'ready'
        && request.agent_run_id === null
        && COMPOSER_LAUNCHABLE_WORKFLOWS.has(
            `${request.workflow_id}@${request.workflow_version}`,
        );
}

export function deliverableRouteTier(
    request: DeliverableRequest | null | undefined,
    fallback: DeliverableRequest['tier'] | null | undefined,
): DeliverableRequest['tier'] | null | undefined {
    return request && requestCanLaunchFromComposer(request) ? request.tier : fallback;
}

export function latestPendingDeliverable(
    requests: DeliverableRequest[],
    dismissedRequestIds: ReadonlySet<string>,
): DeliverableRequest | null {
    return requests.find((request) => (
        request.status === 'ready'
        && request.agent_run_id === null
        && !dismissedRequestIds.has(request.id)
    )) || null;
}


export function latestTrackedDeliverables(
    requests: DeliverableRequest[],
): DeliverableRequest[] {
    const latestByWorkType = new Map<DeliverableRequest['work_type'], DeliverableRequest>();
    for (const request of requests) {
        if (
            request.agent_run_id === null
            || request.status === 'cancelled'
            || latestByWorkType.has(request.work_type)
        ) {
            continue;
        }
        latestByWorkType.set(request.work_type, request);
    }
    return [...latestByWorkType.values()];
}

export function deliverableLaunchMessage(request: DeliverableRequest, isChinese: boolean): string {
    return isChinese
        ? `请按照已确认的工作说明开始制作：${request.goal}`
        : `Start from the confirmed work brief: ${request.goal}`;
}

/**
 * Keep the composer synchronized with the latest confirmed brief without
 * overwriting text that the user has edited themselves.
 *
 * A newly saved brief replaces an empty composer, or the launch text that was
 * generated for the previous pending brief. Anything else is treated as user
 * authored content and is preserved.
 */
export function nextDeliverableComposerText(
    currentText: string,
    request: DeliverableRequest,
    previousRequest: DeliverableRequest | null | undefined,
    isChinese: boolean,
): string {
    const nextText = deliverableLaunchMessage(request, isChinese);
    const previousText = previousRequest
        ? deliverableLaunchMessage(previousRequest, isChinese)
        : null;
    const current = currentText.trim();
    if (!current || (previousText && current === previousText.trim())) {
        return nextText;
    }
    return currentText;
}

export function deliverableApprovalBlocked(request: DeliverableRequest): boolean {
    return request.status === 'waiting_approval'
        && request.current_stage === 'output_review'
        && request.approval_readiness?.approvable === false;
}

export function deliverableApprovalStatusMessage(
    request: DeliverableRequest,
    isChinese: boolean,
): string {
    if (!deliverableApprovalBlocked(request)) {
        return isChinese
            ? '文件已通过结构校验，请确认交付'
            : 'Files passed structural validation and await approval';
    }
    const status = request.approval_readiness?.quality_status;
    if (status === 'blocked') {
        return isChinese
            ? '质量评审发现明确问题，当前不能批准交付'
            : 'Quality review found a blocking issue; delivery cannot be approved';
    }
    if (status === 'invalid') {
        return isChinese
            ? '质量评审凭证无效或与当前文件不匹配'
            : 'Quality evidence is invalid or does not match the current files';
    }
    return isChinese
        ? '文件结构校验已完成，质量评审尚未通过'
        : 'Structural validation passed, but quality review is not complete';
}
