export const MODALITIES = ['text', 'image', 'audio', 'music', 'video', 'multimodal'] as const;

const MODALITY_ALIASES: Record<string, string> = {
    vision: 'image',
    voice: 'audio',
    tts: 'audio',
};

export function canonicalizeModality(value?: string | null): string {
    const normalized = (value || 'text').trim().toLowerCase();
    return MODALITY_ALIASES[normalized] || normalized;
}

export function canonicalizeModalities(values?: string[] | null): string[] {
    if (!values?.length) return [];
    const seen = new Set<string>();
    const out: string[] = [];
    for (const value of values) {
        const canonical = canonicalizeModality(value);
        if (!seen.has(canonical)) {
            seen.add(canonical);
            out.push(canonical);
        }
    }
    return out;
}
