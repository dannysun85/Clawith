const RELOAD_MARKER = 'astra_vite_preload_reload_at';
const RELOAD_GUARD_MS = 15_000;

export function shouldReloadAfterPreloadError(storage: Storage, now = Date.now()): boolean {
    const previous = Number(storage.getItem(RELOAD_MARKER) || 0);
    if (previous > 0 && now - previous < RELOAD_GUARD_MS) return false;
    storage.setItem(RELOAD_MARKER, String(now));
    return true;
}

export function installChunkRecovery(): void {
    window.addEventListener('vite:preloadError', (event) => {
        event.preventDefault();
        if (shouldReloadAfterPreloadError(window.sessionStorage)) {
            window.location.reload();
        }
    });
}
