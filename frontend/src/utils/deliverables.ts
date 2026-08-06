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
    specOverrides: Record<string, string | number>;
};

const DELIVERABLE_ASPECT_RATIOS = new Set(['1:1', '3:4', '9:16', '16:9']);
const POSTER_COPY_LABELS = ['主标题', '副标题', '标语', '落款', 'CTA'] as const;
const POSTER_COPY_LABEL_ALIASES: Record<string, (typeof POSTER_COPY_LABELS)[number]> = {
    '署名': '落款',
    '品牌落款': '落款',
    '按钮': 'CTA',
};
const POSTER_COPY_WRAPPERS: Record<string, string> = {
    '【': '】',
    '「': '」',
    '“': '”',
    '"': '"',
};
const INLINE_POSTER_COPY_PATTERNS: Array<[
    (typeof POSTER_COPY_LABELS)[number],
    RegExp,
]> = [
    ['主标题', /(?:主标题|大标题)[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|"([^"\n]+)")/gi],
    ['副标题', /副标题[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|"([^"\n]+)")/gi],
    ['标语', /(?:标语|口号)[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|"([^"\n]+)")/gi],
    ['落款', /(?:落款|署名|品牌落款)[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|"([^"\n]+)")/gi],
    ['CTA', /(?:(?:CTA|按钮(?:内)?(?:白色)?(?:文字|文案))[^，；。\n]{0,48}?|按钮\s*[:：]?\s*)(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|"([^"\n]+)")/gi],
];

function explicitAspectRatio(goal: string): string | undefined {
    const matches = [...goal.matchAll(/(?:^|[^0-9])((?:1|3|9|16)\s*[:：]\s*(?:1|4|9|16))(?![0-9])/g)]
        .map((match) => match[1].replace(/\s+/g, '').replace('：', ':'))
        .filter((value) => DELIVERABLE_ASPECT_RATIOS.has(value));
    const unique = [...new Set(matches)];
    return unique.length === 1 ? unique[0] : undefined;
}

function explicitPosterCopy(goal: string): string | undefined {
    const values = new Map<(typeof POSTER_COPY_LABELS)[number], string>();

    const record = (
        label: (typeof POSTER_COPY_LABELS)[number],
        rawValue: string,
    ): boolean => {
        let value = rawValue.trim();
        if (value.length >= 2 && POSTER_COPY_WRAPPERS[value[0]] === value[value.length - 1]) {
            value = value.slice(1, -1).trim();
        }
        const previous = values.get(label);
        if (!value || (previous && previous !== value)) return false;
        values.set(label, value);
        return true;
    };

    for (const line of goal.split(/\r?\n/)) {
        const match = line.trim().match(/^(?:[-*•]\s*)?(主标题|副标题|标语|落款|署名|品牌落款|CTA|按钮)\s*[:：]\s*(.+?)\s*$/i);
        if (!match) continue;
        const label = match[1].toUpperCase() === 'CTA'
            ? 'CTA'
            : POSTER_COPY_LABEL_ALIASES[match[1]]
                || match[1] as (typeof POSTER_COPY_LABELS)[number];
        if (!record(label, match[2])) return undefined;
    }
    for (const [label, pattern] of INLINE_POSTER_COPY_PATTERNS) {
        for (const match of goal.matchAll(pattern)) {
            const value = match.slice(1).find((candidate) => candidate !== undefined) || '';
            if (!record(label, value)) return undefined;
        }
    }
    const ordered = POSTER_COPY_LABELS
        .map((label) => values.get(label))
        .filter((value): value is string => Boolean(value));
    if (!values.has('主标题') || ordered.length < 3) return undefined;
    if (values.has('CTA') && ordered.length < 4) return undefined;
    return ordered.join('\n');
}

function workTaskSpecOverrides(
    workType: DeliverableWorkType,
    goal: string,
): Record<string, string | number> {
    const overrides: Record<string, string | number> = {};
    if (workType === 'poster' || workType === 'video') {
        const aspectRatio = explicitAspectRatio(goal);
        if (aspectRatio) overrides.aspect_ratio = aspectRatio;
    }
    if (workType === 'poster') {
        const exactCopy = explicitPosterCopy(goal);
        if (exactCopy) overrides.exact_copy = exactCopy;
    }
    return overrides;
}

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
        specOverrides: item.formal_delivery_spec && Object.keys(item.formal_delivery_spec).length > 0
            ? item.formal_delivery_spec
            : workTaskSpecOverrides(workType, goal),
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

/**
 * A completed Work task is an immutable, server-owned handoff. Until Work
 * persists task-owned file references, the launch message must not borrow
 * files that happen to be attached to the current chat composer.
 */
export function deliverableLaunchUsesIsolatedInputs(
    request: DeliverableRequest | null | undefined,
): boolean {
    return Boolean(request?.task_id);
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
