import type { WorkTaskDraft } from '../services/api';


const VERSION = 2;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const WORK_TYPES = new Set<WorkTaskDraft['work_type']>([
    'general',
    'image',
    'video',
    'presentation',
    'document',
]);
const PRIORITIES = new Set(['low', 'medium', 'high', 'urgent']);
const EXECUTOR_KINDS = new Set<NonNullable<WorkTaskDraft['executor_kind']>>([
    'personal_assistant',
    'agent_employee',
    'temporary_expert',
    'group',
]);

export interface PersistedWorkDraft {
    version: typeof VERSION;
    title: string;
    intent: string;
    workType: WorkTaskDraft['work_type'];
    priority: 'low' | 'medium' | 'high' | 'urgent';
    routingMode: 'auto' | 'manual';
    executorKind: NonNullable<WorkTaskDraft['executor_kind']>;
    agentId: string;
    expertRole: string;
    groupId: string;
    groupSessionId: string;
    groupAgentParticipantIds: string[];
    acceptanceCriteria: string;
    requiredSections: string;
    forbiddenTerms: string;
    minimumLength: string;
    maximumLength: string;
    lengthUnit: 'characters' | 'cjk_characters' | 'words';
    evidenceRequired: boolean;
    clientRequestId: string;
}

export function workDraftStorageKey(
    userId: string | null | undefined,
    tenantId: string | null | undefined,
): string | null {
    if (!userId || !tenantId) return null;
    return `astra:work-draft:v${VERSION}:${tenantId}:${userId}`;
}

export function loadWorkDraft(
    storage: Pick<Storage, 'getItem'>,
    key: string,
): PersistedWorkDraft | null {
    try {
        const raw = storage.getItem(key);
        if (!raw) return null;
        const value = JSON.parse(raw) as Partial<PersistedWorkDraft>;
        if (
            value.version !== VERSION
            || typeof value.title !== 'string'
            || typeof value.intent !== 'string'
            || !WORK_TYPES.has(value.workType as WorkTaskDraft['work_type'])
            || !PRIORITIES.has(String(value.priority))
            || !['auto', 'manual'].includes(String(value.routingMode))
            || !EXECUTOR_KINDS.has(value.executorKind as NonNullable<WorkTaskDraft['executor_kind']>)
            || typeof value.agentId !== 'string'
            || typeof value.expertRole !== 'string'
            || typeof value.groupId !== 'string'
            || typeof value.groupSessionId !== 'string'
            || !Array.isArray(value.groupAgentParticipantIds)
            || !value.groupAgentParticipantIds.every((id) => typeof id === 'string')
            || typeof value.clientRequestId !== 'string'
            || !UUID_PATTERN.test(value.clientRequestId)
        ) {
            return null;
        }
        return {
            ...(value as PersistedWorkDraft),
            acceptanceCriteria: typeof value.acceptanceCriteria === 'string'
                ? value.acceptanceCriteria
                : '',
            requiredSections: typeof value.requiredSections === 'string'
                ? value.requiredSections
                : '',
            forbiddenTerms: typeof value.forbiddenTerms === 'string'
                ? value.forbiddenTerms
                : '',
            minimumLength: typeof value.minimumLength === 'string'
                ? value.minimumLength
                : '',
            maximumLength: typeof value.maximumLength === 'string'
                ? value.maximumLength
                : '',
            lengthUnit: ['characters', 'cjk_characters', 'words'].includes(
                String(value.lengthUnit),
            )
                ? value.lengthUnit as PersistedWorkDraft['lengthUnit']
                : 'characters',
            evidenceRequired: value.evidenceRequired === true,
        };
    } catch {
        return null;
    }
}

export function saveWorkDraft(
    storage: Pick<Storage, 'setItem'>,
    key: string,
    draft: PersistedWorkDraft,
): void {
    try {
        storage.setItem(key, JSON.stringify(draft));
    } catch {
        // Storage may be disabled or full. Backend idempotency remains the
        // source of truth; the page simply cannot offer refresh recovery.
    }
}

export function clearWorkDraft(
    storage: Pick<Storage, 'removeItem'>,
    key: string,
): void {
    try {
        storage.removeItem(key);
    } catch {
        // Keep task completion usable even when browser storage is blocked.
    }
}
