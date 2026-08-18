import { describe, expect, it } from 'vitest';

import {
    clearWorkDraft,
    loadWorkDraft,
    saveWorkDraft,
    workDraftStorageKey,
    type PersistedWorkDraft,
} from './workDraftPersistence';


class MemoryStorage {
    values = new Map<string, string>();

    getItem(key: string) {
        return this.values.get(key) ?? null;
    }

    setItem(key: string, value: string) {
        this.values.set(key, value);
    }

    removeItem(key: string) {
        this.values.delete(key);
    }
}

const draft: PersistedWorkDraft = {
    version: 2,
    title: 'Prepare launch',
    intent: 'Prepare a launch brief',
    workType: 'presentation',
    priority: 'high',
    routingMode: 'manual',
    executorKind: 'group',
    agentId: '',
    expertRole: '',
    groupId: 'group-1',
    groupSessionId: 'session-1',
    groupAgentParticipantIds: ['participant-1', 'participant-2'],
    clientRequestId: 'c3f0fcd5-f85c-4b03-9bb7-275cd5f2f2b4',
};

describe('work draft refresh recovery', () => {
    it('round-trips the original client request id with the draft', () => {
        const storage = new MemoryStorage();
        const key = workDraftStorageKey('user-1', 'tenant-1')!;

        saveWorkDraft(storage, key, draft);

        expect(loadWorkDraft(storage, key)).toEqual(draft);
        clearWorkDraft(storage, key);
        expect(loadWorkDraft(storage, key)).toBeNull();
    });

    it('isolates drafts by both user and tenant', () => {
        expect(workDraftStorageKey('user-1', 'tenant-1')).not.toBe(
            workDraftStorageKey('user-1', 'tenant-2'),
        );
        expect(workDraftStorageKey('user-1', null)).toBeNull();
    });

    it('rejects corrupt or non-uuid request identities', () => {
        const storage = new MemoryStorage();
        const key = 'draft';
        storage.setItem(key, JSON.stringify({ ...draft, clientRequestId: 'retry-1' }));

        expect(loadWorkDraft(storage, key)).toBeNull();
        storage.setItem(key, '{not-json');
        expect(loadWorkDraft(storage, key)).toBeNull();
    });
});
