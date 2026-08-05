import { describe, expect, it } from 'vitest';

import { deriveRegistrationIdentity } from './registrationIdentity';

describe('deriveRegistrationIdentity', () => {
    it('keeps an ordinary email local-part as the internal username', () => {
        expect(deriveRegistrationIdentity(' Alice.Smith@example.com ')).toEqual({
            username: 'alice.smith',
            displayName: 'alice.smith',
        });
    });

    it('converts a numeric email local-part into a safe stable username', () => {
        const first = deriveRegistrationIdentity('1234567890@example.com');
        const second = deriveRegistrationIdentity('1234567890@example.com');

        expect(first).toEqual(second);
        expect(first.displayName).toBe('1234567890');
        expect(first.username).toMatch(/^user_1234567890_[a-z0-9]{7}$/);
        expect(first.username).not.toMatch(/^\d+$/);
    });

    it('uses the full email to avoid cross-domain collisions', () => {
        expect(deriveRegistrationIdentity('13800138000@qq.com').username)
            .not.toBe(deriveRegistrationIdentity('13800138000@example.com').username);
    });

    it('keeps generated usernames within the backend limit', () => {
        const localPart = '+'.padEnd(20, '1');
        const result = deriveRegistrationIdentity(`${localPart}@example.com`);

        expect(result.username.length).toBeLessThanOrEqual(100);
        expect(result.username.startsWith('user_')).toBe(true);
    });

    it('keeps the generated display name within the persisted user limit', () => {
        const result = deriveRegistrationIdentity(`${'a'.repeat(120)}@example.com`);

        expect(result.displayName).toHaveLength(100);
        expect(result.username).toHaveLength(100);
    });
});
