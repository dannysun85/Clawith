import type { WorkforceTopologyNode } from '../services/api';

export const TOPOLOGY_NODE_WIDTH = 156;
export const TOPOLOGY_NODE_HEIGHT = 72;
export const TOPOLOGY_CENTER_WIDTH = 184;
export const TOPOLOGY_CENTER_HEIGHT = 88;

export type TopologyLayoutPreference = 'auto' | 'ring' | 'grid';
export type TopologyLayoutMode = 'ring' | 'grid';
export type TopologyHealthFilter =
    | 'all'
    | 'running'
    | 'idle'
    | 'creating'
    | 'stopped'
    | 'error';
export type TopologyWorkFilter =
    | 'all'
    | 'executing'
    | 'waiting'
    | 'review'
    | 'approval'
    | 'blocked'
    | 'completed'
    | 'no_work';
export type TopologyWorkGroup = Exclude<TopologyWorkFilter, 'all'>;

export type TopologyPoint = { x: number; y: number };

export type TopologyLayout = {
    mode: TopologyLayoutMode;
    center: TopologyPoint;
    positions: Map<string, TopologyPoint>;
    bounds: { minX: number; minY: number; maxX: number; maxY: number };
};

export function topologyExecutionWorkGroup(
    node: WorkforceTopologyNode,
): TopologyWorkGroup {
    const status = node.execution?.status;
    if (status === 'queued' || status === 'running') return 'executing';
    if (status === 'waiting_user' || status === 'waiting_agent' || status === 'waiting_external') {
        return 'waiting';
    }
    if (status === 'failed') return 'blocked';
    if (status === 'completed' || status === 'cancelled') return 'completed';
    return node.work?.stage ?? 'no_work';
}

export function filterTopologyNodes(
    nodes: WorkforceTopologyNode[],
    options: {
        query: string;
        health: TopologyHealthFilter;
        work: TopologyWorkFilter;
    },
): WorkforceTopologyNode[] {
    const normalizedQuery = options.query.trim().toLocaleLowerCase();
    return nodes.filter((node) => {
        if (options.health !== 'all' && node.status !== options.health) return false;
        const workGroup = topologyExecutionWorkGroup(node);
        if (options.work !== 'all' && workGroup !== options.work) return false;
        if (!normalizedQuery) return true;
        return `${node.name} ${node.role_description}`
            .toLocaleLowerCase()
            .includes(normalizedQuery);
    });
}

export function topologyNodeIdKey(nodes: WorkforceTopologyNode[]): string {
    return nodes.map((node) => node.id).join('|');
}

function ringPositions(nodeIds: string[]): Map<string, TopologyPoint> {
    const positions = new Map<string, TopologyPoint>();
    let offset = 0;
    let radius = 220;
    let ringIndex = 0;

    while (offset < nodeIds.length) {
        const circumferenceCapacity = Math.floor(
            (2 * Math.PI * radius) / (TOPOLOGY_NODE_WIDTH + 28),
        );
        const capacity = Math.max(8, circumferenceCapacity);
        const count = Math.min(capacity, nodeIds.length - offset);
        const angleOffset = -Math.PI / 2 + (ringIndex % 2 === 0 ? 0 : Math.PI / count);
        for (let index = 0; index < count; index += 1) {
            const angle = angleOffset + (index / count) * Math.PI * 2;
            const nodeId = nodeIds[offset + index];
            if (!nodeId) continue;
            positions.set(nodeId, {
                x: Math.cos(angle) * radius,
                y: Math.sin(angle) * radius,
            });
        }
        offset += count;
        radius += 142;
        ringIndex += 1;
    }
    return positions;
}

function gridPositions(nodeIds: string[]): Map<string, TopologyPoint> {
    const positions = new Map<string, TopologyPoint>();
    const columns = Math.max(1, Math.ceil(Math.sqrt(nodeIds.length * 1.6)));
    const horizontalGap = 184;
    const verticalGap = 108;
    const rows = Math.ceil(nodeIds.length / columns);
    const gridHeight = (rows - 1) * verticalGap;
    const startY = 132;

    nodeIds.forEach((nodeId, index) => {
        const row = Math.floor(index / columns);
        const itemsInRow = Math.min(columns, nodeIds.length - row * columns);
        const rowWidth = (itemsInRow - 1) * horizontalGap;
        const rowStartX = -rowWidth / 2;
        positions.set(nodeId, {
            x: rowStartX + (index % columns) * horizontalGap,
            y: startY + row * verticalGap - gridHeight / 2,
        });
    });
    return positions;
}

function layoutBounds(
    positions: Map<string, TopologyPoint>,
    center: TopologyPoint,
): TopologyLayout['bounds'] {
    const points = [...positions.values()];
    const minX = Math.min(
        center.x - TOPOLOGY_CENTER_WIDTH / 2,
        ...points.map((point) => point.x - TOPOLOGY_NODE_WIDTH / 2),
    );
    const maxX = Math.max(
        center.x + TOPOLOGY_CENTER_WIDTH / 2,
        ...points.map((point) => point.x + TOPOLOGY_NODE_WIDTH / 2),
    );
    const minY = Math.min(
        center.y - TOPOLOGY_CENTER_HEIGHT / 2,
        ...points.map((point) => point.y - TOPOLOGY_NODE_HEIGHT / 2),
    );
    const maxY = Math.max(
        center.y + TOPOLOGY_CENTER_HEIGHT / 2,
        ...points.map((point) => point.y + TOPOLOGY_NODE_HEIGHT / 2),
    );
    return { minX, minY, maxX, maxY };
}

export function computeTopologyLayout(
    nodeIds: string[],
    preference: TopologyLayoutPreference,
): TopologyLayout {
    const mode: TopologyLayoutMode = preference === 'auto'
        ? (nodeIds.length > 12 ? 'grid' : 'ring')
        : preference;
    const center = mode === 'grid' ? { x: 0, y: -120 } : { x: 0, y: 0 };
    const positions = mode === 'grid'
        ? gridPositions(nodeIds)
        : ringPositions(nodeIds);
    return {
        mode,
        center,
        positions,
        bounds: layoutBounds(positions, center),
    };
}

export function topologyRectsOverlap(
    first: TopologyPoint,
    second: TopologyPoint,
    padding = 8,
): boolean {
    return Math.abs(first.x - second.x) < TOPOLOGY_NODE_WIDTH + padding
        && Math.abs(first.y - second.y) < TOPOLOGY_NODE_HEIGHT + padding;
}
