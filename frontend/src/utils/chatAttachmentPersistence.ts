const PERSISTED_FILE_PREFIX_RE = /^\[file:([^\]\r\n]+)\]\r?\n?/;
const DISPLAY_ATTACHMENT_LINE_RE = /^((?:\[Attachment: [^\]\r\n]+\][ \t]*)+)(?:\r?\n)?/;
const DISPLAY_ATTACHMENT_RE = /\[Attachment: ([^\]\r\n]+)\]/g;

const safeBasename = (value: string): string => (
    value.replace(/\\/g, '/').split('/').pop()?.trim() || ''
);

export type PersistedChatAttachments = {
    content: string;
    displayFileNames: string[];
    storageFileNames: string[];
};

/**
 * Split durable attachment metadata from a persisted chat message.
 *
 * The server stores one or more `[file:<stored basename>]` prefixes before the
 * display-only `[Attachment: <original name>]` markers. Keeping those values
 * separate lets history show the customer's filename while downloads continue
 * to address the collision-safe object that was actually written.
 */
export const parsePersistedChatAttachments = (content: string): PersistedChatAttachments => {
    let remainder = content;
    const storageFileNames: string[] = [];

    while (true) {
        const match = remainder.match(PERSISTED_FILE_PREFIX_RE);
        if (!match) break;
        const storageName = safeBasename(match[1]);
        if (storageName && !storageFileNames.includes(storageName)) {
            storageFileNames.push(storageName);
        }
        remainder = remainder.slice(match[0].length);
    }

    const displayFileNames: string[] = [];
    if (storageFileNames.length > 0) {
        const displayLine = remainder.match(DISPLAY_ATTACHMENT_LINE_RE);
        if (displayLine) {
            for (const match of displayLine[1].matchAll(DISPLAY_ATTACHMENT_RE)) {
                const displayName = match[1].trim();
                if (displayName) {
                    displayFileNames.push(displayName);
                }
            }
            remainder = remainder.slice(displayLine[0].length);
        }
    }

    return {
        content: remainder.trim(),
        displayFileNames: displayFileNames.length > 0 ? displayFileNames : storageFileNames,
        storageFileNames,
    };
};

export const attachmentStorageBasename = (path: string | undefined, fallback: string): string => (
    safeBasename(path || '') || safeBasename(fallback)
);
