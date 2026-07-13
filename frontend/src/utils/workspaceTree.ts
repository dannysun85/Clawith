export interface WorkspaceFileNode {
    name: string;
    path: string;
    is_dir: boolean;
    children?: WorkspaceFileNode[];
}

type ListWorkspaceFiles = (path: string) => Promise<WorkspaceFileNode[]>;

/**
 * Load only the visible branches of the workspace tree.
 *
 * Expanded paths are fetched lazily, so directory depth is limited by the
 * actual workspace rather than an arbitrary UI cutoff. The ancestry guard
 * prevents a malformed listing from recursing forever when it points back to
 * one of its parents.
 */
export async function loadExpandedFileTree(
    listFiles: ListWorkspaceFiles,
    rootPath: string,
    expandedDirs: ReadonlySet<string>,
): Promise<WorkspaceFileNode[]> {
    const loadDir = async (
        path: string,
        ancestors: ReadonlySet<string>,
    ): Promise<WorkspaceFileNode[]> => {
        if (ancestors.has(path)) return [];

        const isRoot = path === rootPath;
        if (!isRoot && !expandedDirs.has(path)) return [];

        const nextAncestors = new Set(ancestors);
        nextAncestors.add(path);
        const items = await listFiles(path).catch(() => []);

        return Promise.all(items.map(async (item) => {
            if (!item.is_dir) return item;
            const children = expandedDirs.has(item.path)
                ? await loadDir(item.path, nextAncestors)
                : [];
            return { ...item, children };
        }));
    };

    return loadDir(rootPath, new Set());
}
