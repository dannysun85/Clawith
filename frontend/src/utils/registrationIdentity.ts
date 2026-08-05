const MAX_USERNAME_LENGTH = 100;

function usernameLooksLikeContact(value: string): boolean {
    if (value.includes('@')) return true;
    const digits = value.replace(/[\s\-+()]/g, '');
    return digits.length >= 6
        && digits.length <= 20
        && /^\d+$/.test(digits)
        && /^[\d\s\-+()]+$/.test(value);
}

function stableEmailSuffix(email: string): string {
    let hash = 0x811c9dc5;
    for (let index = 0; index < email.length; index += 1) {
        hash ^= email.charCodeAt(index);
        hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(36).padStart(7, '0');
}

export interface RegistrationIdentity {
    username: string;
    displayName: string;
}

/**
 * Derive internal registration fields from an email without exposing username
 * policy to the customer.  Numeric email local-parts look like phone numbers
 * to the backend login namespace, so they receive a safe, stable prefix and a
 * short email-derived suffix to avoid common cross-domain collisions.
 */
export function deriveRegistrationIdentity(emailInput: string): RegistrationIdentity {
    const email = emailInput.trim().toLowerCase();
    const localPart = email.split('@', 1)[0]?.trim() || 'user';
    const displayName = localPart.slice(0, MAX_USERNAME_LENGTH);
    let username = localPart.slice(0, MAX_USERNAME_LENGTH);

    if (usernameLooksLikeContact(username)) {
        const suffix = stableEmailSuffix(email);
        const prefix = 'user_';
        const separator = '_';
        const availableLocalLength = MAX_USERNAME_LENGTH
            - prefix.length
            - separator.length
            - suffix.length;
        username = `${prefix}${localPart.slice(0, availableLocalLength)}${separator}${suffix}`;
    }

    return { username, displayName };
}
