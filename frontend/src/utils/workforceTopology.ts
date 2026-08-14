import type {
    WorkItem,
    WorkforceTopologyNode,
} from '../services/api';

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

export type AgentWorkSummary = {
    item: WorkItem;
    group: TopologyWorkGroup;
    activeCount: number;
};

const STAGE_PRIORITY: Record<string, number> = {
    blocked: 0,
    approval: 1,
    review: 2,
    artifact: 3,
    execution: 4,
    task: 5,
    delivery: 6,
    completed: 7,
    cancelled: 8,
};

export function workStageGroup(stage: string | null | undefined): TopologyWorkGroup {
    switch (stage) {
        case 'task':
        case 'execution':
            return 'executing';
        case 'artifact':
        case 'review':
            return 'review';
        case 'approval':
            return 'approval';
        case 'blocked':
        case 'cancelled':
            return 'blocked';
        case 'delivery':
        case 'completed':
            return 'completed';
        default:
            return 'no_work';
    }
}

export function buildAgentWorkMap(workItems: WorkItem[]): Map<string, AgentWorkSummary> {
    const grouped = new Map<string, WorkItem[]>();
    for (const item of workItems) {
        const current = grouped.get(item.agent_id);
        if (current) current.push(item);
        else grouped.set(item.agent_id, [item]);
    }

    const result = new Map<string, AgentWorkSummary>();
    for (const [agentId, items] of grouped) {
        const ordered = items.slice().sort((first: WorkItem, second: WorkItem) => {
            const stageDelta = (STAGE_PRIORITY[first.user_stage] ?? 99)
                - (STAGE_PRIORITY[second.user_stage] ?? 99);
            if (stageDelta !== 0) return stageDelta;
            return new Date(second.updated_at).getTime() - new Date(first.updated_at).getTime();
        });
        const item = ordered[0];
        if (!item) continue;
        const activeCount = items.filter((candidate) => (
            !['completed', 'delivery', 'cancelled'].includes(candidate.user_stage)
        )).length;
        result.set(agentId, {
            item,
            group: workStageGroup(item.user_stage),
            activeCount,
        });
    }
    return result;
}

export function filterTopologyNodes(
    nodes: WorkforceTopologyNode[],
    options: {
        query: string;
        health: TopologyHealthFilter;
        work: TopologyWorkFilter;
        workByAgent: Map<string, AgentWorkSummary>;
    },
): WorkforceTopologyNode[] {
    const normalizedQuery = options.query.trim().toLocaleLowerCase();
    return nodes.filter((node) => {
        if (options.health !== 'all' && node.status !== options.health) return false;
        const workGroup = options.workByAgent.get(node.id)?.group ?? 'no_work';
        if (options.work !== 'all' && workGroup !== options.work) return false;
        if (!normalizedQuery) return true;
        return `${node.name} ${node.role_description}`
            .toLocaleLowerCase()
            .includes(normalizedQuery);
    });
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
    const gridWidth = (columns - 1) * horizontalGap;
    const gridHeight = (rows - 1) * verticalGap;
    const startX = -gridWidth / 2;
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
        ? (nodeIds.length >= 20 ? 'grid' : 'ring')
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
