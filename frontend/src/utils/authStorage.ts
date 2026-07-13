const AUTH_SCOPED_KEYS = [
    'token',
    'user',
    'current_tenant_id',
    'pinned_agents',
] as const;

/** Remove identity and tenant-scoped browser state while preserving UI preferences. */
export function clearAuthStorage(storage: Pick<Storage, 'removeItem'> = localStorage): void {
    for (const key of AUTH_SCOPED_KEYS) {
        storage.removeItem(key);
    }
}
