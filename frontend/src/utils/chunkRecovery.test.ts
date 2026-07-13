import { describe, expect, it } from 'vitest';
import { shouldReloadAfterPreloadError } from './chunkRecovery';

function memoryStorage(): Storage {
    const values = new Map<string, string>();
    return {
        get length() { return values.size; },
        clear: () => values.clear(),
        getItem: (key) => values.get(key) ?? null,
        key: (index) => [...values.keys()][index] ?? null,
        removeItem: (key) => { values.delete(key); },
        setItem: (key, value) => { values.set(key, value); },
    };
}

describe('stale lazy chunk recovery', () => {
    it('reloads once and prevents a rapid reload loop', () => {
        const storage = memoryStorage();

        expect(shouldReloadAfterPreloadError(storage, 100_000)).toBe(true);
        expect(shouldReloadAfterPreloadError(storage, 101_000)).toBe(false);
        expect(shouldReloadAfterPreloadError(storage, 120_000)).toBe(true);
    });
});
