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
    storageFilePaths: string[];
};

const safeWorkspaceAttachmentPath = (
    storageFileName: string,
    displayFileName: string,
): string | null => {
    const storageName = safeBasename(storageFileName);
    if (!storageName) return null;

    const normalizedDisplay = displayFileName.replace(/\\/g, '/').trim();
    const relativePath = normalizedDisplay.replace(/^workspace\//, '');
    const segments = relativePath.split('/');
    if (
        segments.length < 2
        || segments.some((segment) => (
            !segment
            || segment === '.'
            || segment === '..'
            || /[\x00-\x1f\x7f:\[\]]/.test(segment)
        ))
        || safeBasename(relativePath) !== storageName
    ) {
        return null;
    }
    return `workspace/${segments.join('/')}`;
};

/**
 * Resolve the server path used to preview a persisted attachment.
 *
 * Browser uploads keep their collision-safe basename under
 * `workspace/uploads/`. Workspace-generated files retain their relative path
 * in the display marker (for example `posters/example.jpg`); accept that path
 * only when every segment is safe and its basename matches the durable marker.
 */
export const attachmentStoragePath = (
    storageFileName: string,
    displayFileName: string,
): string => {
    const storageName = safeBasename(storageFileName);
    return safeWorkspaceAttachmentPath(storageName, displayFileName)
        || `workspace/uploads/${storageName}`;
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

    const resolvedDisplayFileNames = displayFileNames.length > 0
        ? displayFileNames
        : storageFileNames;

    return {
        content: remainder.trim(),
        displayFileNames: resolvedDisplayFileNames,
        storageFileNames,
        storageFilePaths: storageFileNames.map((storageName, index) => (
            attachmentStoragePath(storageName, resolvedDisplayFileNames[index] || storageName)
        )),
    };
};

export const attachmentStorageBasename = (path: string | undefined, fallback: string): string => (
    safeBasename(path || '') || safeBasename(fallback)
);
