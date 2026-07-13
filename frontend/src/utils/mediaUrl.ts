/** Return a lowercase media extension from either a direct URL or `?path=` download URL. */
export function mediaUrlExtension(rawUrl: string): string {
    const value = String(rawUrl || '').trim();
    if (!value) return '';

    let candidate = value;
    try {
        const parsed = new URL(value, 'https://astra.local');
        candidate = parsed.searchParams.get('path') || parsed.pathname;
    } catch {
        candidate = value.split('#')[0].split('?')[0];
    }

    const clean = candidate.split('#')[0].split('?')[0];
    const name = clean.split('/').pop() || '';
    const dot = name.lastIndexOf('.');
    return dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
}
