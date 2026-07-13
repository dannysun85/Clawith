import { describe, expect, it, vi } from 'vitest';

import { loadExpandedFileTree, type WorkspaceFileNode } from './workspaceTree';

describe('loadExpandedFileTree', () => {
    it('loads expanded directories beyond the former depth limit', async () => {
        const depth = 12;
        const paths = Array.from({ length: depth + 1 }, (_, index) => (
            index === 0 ? 'workspace' : `workspace/${Array.from({ length: index }, (_value, part) => `d${part + 1}`).join('/')}`
        ));
        const expanded = new Set(paths);
        const listFiles = vi.fn(async (path: string): Promise<WorkspaceFileNode[]> => {
            const index = paths.indexOf(path);
            if (index < depth) {
                return [{
                    name: `d${index + 1}`,
                    path: paths[index + 1],
                    is_dir: true,
                }];
            }
            return [{ name: 'deep.txt', path: `${path}/deep.txt`, is_dir: false }];
        });

        const tree = await loadExpandedFileTree(listFiles, 'workspace', expanded);

        let nodes = tree;
        for (let index = 0; index < depth; index += 1) {
            expect(nodes[0]?.is_dir).toBe(true);
            nodes = nodes[0]?.children || [];
        }
        expect(nodes).toEqual([{
            name: 'deep.txt',
            path: `${paths[depth]}/deep.txt`,
            is_dir: false,
        }]);
        expect(listFiles).toHaveBeenCalledTimes(depth + 1);
    });

    it('stops malformed directory cycles without imposing a depth cap', async () => {
        const listFiles = vi.fn(async (): Promise<WorkspaceFileNode[]> => ([{
            name: 'workspace',
            path: 'workspace',
            is_dir: true,
        }]));

        const tree = await loadExpandedFileTree(listFiles, 'workspace', new Set(['workspace']));

        expect(tree[0]?.children).toEqual([]);
        expect(listFiles).toHaveBeenCalledTimes(1);
    });
});
