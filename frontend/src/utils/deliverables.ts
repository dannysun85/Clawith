import type { DeliverableRequest } from '../services/api';


export function requestCanLaunchFromComposer(request: DeliverableRequest): boolean {
    return request.status === 'ready'
        && request.agent_run_id === null
        && request.workflow_id === 'builtin.presentation.v1'
        && request.workflow_version === '1.0.0';
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

export function deliverableLaunchMessage(request: DeliverableRequest, isChinese: boolean): string {
    return isChinese
        ? `请按照已确认的工作说明开始制作：${request.goal}`
        : `Start from the confirmed work brief: ${request.goal}`;
}
