const MANAGED_MEDIA_TOOL_TITLES: Readonly<Record<string, string>> = {
    generate_image_minimax: 'Generate Image',
    generate_speech_minimax: 'Generate Speech',
    generate_music_minimax: 'Generate Music',
    generate_video_minimax: 'Generate Video',
    check_video_minimax: 'Check Video',
    compose_video_audio_minimax: 'Compose Video Audio',
};

/**
 * Return a customer-safe title for a runtime tool identifier.
 *
 * The managed media identifiers retain their historical provider suffixes for
 * protocol compatibility. Provider selection is an internal routing concern,
 * so those suffixes must not leak into the customer-facing execution trace.
 */
export function toolDisplayName(name: string): string {
    const normalized = (name || 'tool').trim();
    const managedTitle = MANAGED_MEDIA_TOOL_TITLES[normalized.toLowerCase()];
    if (managedTitle) return managedTitle;

    return normalized
        .replace(/^mcp[_:-]/i, '')
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/\b\w/g, ch => ch.toUpperCase());
}
