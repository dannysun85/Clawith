export class AuthBootstrapTimeoutError extends Error {
    constructor() {
        super('Authentication bootstrap timed out');
        this.name = 'AuthBootstrapTimeoutError';
    }
}

export async function withAuthBootstrapTimeout<T>(
    action: (signal: AbortSignal) => Promise<T>,
    timeoutMs = 8_000,
): Promise<T> {
    const controller = new AbortController();
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
        timeoutId = setTimeout(() => {
            reject(new AuthBootstrapTimeoutError());
            controller.abort();
        }, timeoutMs);
    });

    try {
        return await Promise.race([action(controller.signal), timeout]);
    } finally {
        if (timeoutId !== undefined) clearTimeout(timeoutId);
    }
}

export function isDefinitiveAuthRejection(error: unknown): boolean {
    if (!error || typeof error !== 'object') return false;
    const status = 'status' in error ? Number(error.status) : undefined;
    return status === 401 || status === 403;
}

export function isTransientAuthBootstrapFailure(error: unknown): boolean {
    if (error instanceof AuthBootstrapTimeoutError) return true;
    if (error instanceof TypeError) return true;
    if (!error || typeof error !== 'object') return false;

    const candidate = error as {
        name?: unknown;
        message?: unknown;
        retryable?: unknown;
        status?: unknown;
    };
    if (candidate.name === 'AbortError') return true;
    if (candidate.retryable === true) return true;

    const status = Number(candidate.status);
    if (Number.isFinite(status) && status >= 500) return true;

    const message = typeof candidate.message === 'string' ? candidate.message : '';
    return /Browser session could not be established \(HTTP 5\d\d\)/.test(message);
}
