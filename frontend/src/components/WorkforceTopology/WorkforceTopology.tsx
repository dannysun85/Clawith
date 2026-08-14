import {
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
} from 'react';
import { createPortal } from 'react-dom';
import {
    IconActivity,
    IconArrowsMaximize,
    IconCirclesRelation,
    IconLayoutGrid,
    IconMessage,
    IconMinus,
    IconPlus,
    IconSearch,
    IconSettings,
    IconTopologyRing,
    IconX,
} from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';

import type {
    WorkItem,
    WorkforceTopology as WorkforceTopologyData,
    WorkforceTopologyActivity,
    WorkforceTopologyNode,
} from '../../services/api';
import {
    buildAgentWorkMap,
    computeTopologyLayout,
    filterTopologyNodes,
    TOPOLOGY_CENTER_HEIGHT,
    TOPOLOGY_CENTER_WIDTH,
    TOPOLOGY_NODE_HEIGHT,
    TOPOLOGY_NODE_WIDTH,
    type AgentWorkSummary,
    type TopologyHealthFilter,
    type TopologyLayoutPreference,
    type TopologyWorkFilter,
    type TopologyWorkGroup,
} from '../../utils/workforceTopology';
import './workforceTopology.css';


type WorkforceTopologyProps = {
    topology: WorkforceTopologyData;
    workItems: WorkItem[];
};

type Viewport = { width: number; height: number };
type ViewTransform = { x: number; y: number; scale: number };

const MIN_SCALE = 0.28;
const MAX_SCALE = 2;

function clampScale(scale: number): number {
    return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

function formatRelativeTime(
    value: string | null | undefined,
    t: ReturnType<typeof useTranslation>['t'],
): string {
    if (!value) return t('dashboard.topology.never');
    const elapsed = Date.now() - new Date(value).getTime();
    const minutes = Math.max(0, Math.floor(elapsed / 60_000));
    if (minutes < 1) return t('dashboard.justNow');
    if (minutes < 60) return t('dashboard.minutesAgo', { count: minutes });
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t('dashboard.hoursAgo', { count: hours });
    return t('dashboard.daysAgo', { count: Math.floor(hours / 24) });
}

function statusLabel(status: string, t: ReturnType<typeof useTranslation>['t']): string {
    return t(`dashboard.status.${status}`, status);
}

function workGroupLabel(
    group: TopologyWorkGroup,
    t: ReturnType<typeof useTranslation>['t'],
): string {
    return t(`dashboard.topology.work.${group}`);
}

function AgentAvatar({ node, size = 30 }: { node: WorkforceTopologyNode; size?: number }) {
    const initial = Array.from(node.name.trim())[0]?.toUpperCase() || 'A';
    return (
        <span className="workforce-node__avatar" style={{ width: size, height: size }} aria-hidden="true">
            {node.avatar_url ? <img src={node.avatar_url} alt="" /> : initial}
        </span>
    );
}

function NodeCard({
    node,
    work,
    selected,
    onSelect,
}: {
    node: WorkforceTopologyNode;
    work?: AgentWorkSummary;
    selected: boolean;
    onSelect: () => void;
}) {
    const { t } = useTranslation();
    const workGroup = work?.group ?? 'no_work';
    return (
        <button
            type="button"
            className={`workforce-node${selected ? ' is-selected' : ''}`}
            data-status={node.status}
            onClick={onSelect}
            aria-label={t('dashboard.topology.openAgent', {
                name: node.name,
                status: statusLabel(node.status, t),
            })}
        >
            <span className="workforce-node__identity">
                <AgentAvatar node={node} />
                <span className="workforce-node__copy">
                    <strong>{node.name}</strong>
                    <span>{node.role_description || t('dashboard.topology.noRole')}</span>
                </span>
                <span
                    className="workforce-node__status"
                    data-status={node.status}
                    title={statusLabel(node.status, t)}
                />
            </span>
            <span className="workforce-node__work">
                <span className="workforce-work-chip" data-work={workGroup}>
                    {workGroupLabel(workGroup, t)}
                </span>
                <span className="workforce-node__work-title">
                    {work?.item.title || t('dashboard.topology.noCurrentWork')}
                </span>
            </span>
        </button>
    );
}

function WorkforceNodeDrawer({
    node,
    work,
    activity,
    relationshipCount,
    interactionCount,
    onClose,
}: {
    node: WorkforceTopologyNode;
    work?: AgentWorkSummary;
    activity?: WorkforceTopologyActivity;
    relationshipCount: number;
    interactionCount: number;
    onClose: () => void;
}) {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const drawerRef = useRef<HTMLElement>(null);
    const closeButtonRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        const returnFocus = document.activeElement as HTMLElement | null;
        closeButtonRef.current?.focus();
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                onClose();
                return;
            }
            if (event.key !== 'Tab' || !drawerRef.current) return;
            const focusable = Array.from(
                drawerRef.current.querySelectorAll<HTMLElement>(
                    'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
                ),
            );
            if (focusable.length === 0) return;
            const first = focusable[0]!;
            const last = focusable[focusable.length - 1]!;
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };
        document.addEventListener('keydown', onKeyDown);
        return () => {
            document.removeEventListener('keydown', onKeyDown);
            returnFocus?.focus();
        };
    }, [onClose]);

    const workGroup = work?.group ?? 'no_work';
    return createPortal(
        <div className="workforce-drawer-layer">
            <button
                type="button"
                className="workforce-drawer__backdrop"
                onClick={onClose}
                aria-label={t('common.close')}
            />
            <aside
                ref={drawerRef}
                className="workforce-drawer"
                role="dialog"
                aria-modal="true"
                aria-labelledby="workforce-drawer-title"
            >
                <header className="workforce-drawer__header">
                    <div className="workforce-drawer__identity">
                        <AgentAvatar node={node} size={44} />
                        <div>
                            <h2 id="workforce-drawer-title">{node.name}</h2>
                            <p>{node.role_description || t('dashboard.topology.noRole')}</p>
                        </div>
                    </div>
                    <button
                        ref={closeButtonRef}
                        type="button"
                        className="workforce-icon-button"
                        onClick={onClose}
                        aria-label={t('common.close')}
                    >
                        <IconX size={18} />
                    </button>
                </header>

                <div className="workforce-drawer__body">
                    <section className="workforce-drawer__status-grid" aria-label={t('dashboard.topology.statusOverview')}>
                        <div>
                            <span>{t('dashboard.topology.health')}</span>
                            <strong className="workforce-drawer__health">
                                <i data-status={node.status} />
                                {statusLabel(node.status, t)}
                            </strong>
                        </div>
                        <div>
                            <span>{t('dashboard.topology.workStage')}</span>
                            <strong>
                                <span className="workforce-work-chip" data-work={workGroup}>
                                    {workGroupLabel(workGroup, t)}
                                </span>
                            </strong>
                        </div>
                        <div>
                            <span>{t('dashboard.topology.relationships')}</span>
                            <strong>{relationshipCount}</strong>
                        </div>
                        <div>
                            <span>{t('dashboard.topology.interactions24h')}</span>
                            <strong>{interactionCount}</strong>
                        </div>
                    </section>

                    <section className="workforce-drawer__section">
                        <h3>{t('dashboard.topology.currentWork')}</h3>
                        {work ? (
                            <button
                                type="button"
                                className="workforce-drawer__work-card"
                                onClick={() => navigate(work.item.deep_link)}
                            >
                                <span className="workforce-work-chip" data-work={work.group}>
                                    {workGroupLabel(work.group, t)}
                                </span>
                                <strong>{work.item.title}</strong>
                                <span>{work.item.latest_update || work.item.intent}</span>
                                {work.activeCount > 1 && (
                                    <small>{t('dashboard.topology.moreActiveWork', { count: work.activeCount - 1 })}</small>
                                )}
                            </button>
                        ) : (
                            <p className="workforce-drawer__empty">{t('dashboard.topology.noCurrentWork')}</p>
                        )}
                    </section>

                    <section className="workforce-drawer__section">
                        <h3>{t('dashboard.topology.latestActivity')}</h3>
                        {activity ? (
                            <div className="workforce-drawer__activity">
                                <IconActivity size={17} />
                                <div>
                                    <strong>{activity.summary}</strong>
                                    <span>{formatRelativeTime(activity.created_at, t)}</span>
                                </div>
                            </div>
                        ) : (
                            <p className="workforce-drawer__empty">{t('dashboard.noActivity')}</p>
                        )}
                    </section>
                </div>

                <footer className="workforce-drawer__footer">
                    <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => navigate(`/agents/${node.id}/chat`)}
                    >
                        <IconMessage size={17} />
                        {t('dashboard.topology.sendMessage')}
                    </button>
                    <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => navigate(`/agents/${node.id}/settings`)}
                    >
                        <IconSettings size={17} />
                        {t('dashboard.topology.viewAgent')}
                    </button>
                </footer>
            </aside>
        </div>,
        document.body,
    );
}

export default function WorkforceTopology({ topology, workItems }: WorkforceTopologyProps) {
    const { t } = useTranslation();
    const canvasRef = useRef<HTMLDivElement>(null);
    const svgRef = useRef<SVGSVGElement>(null);
    const panRef = useRef<{
        pointerId: number;
        startX: number;
        startY: number;
        originX: number;
        originY: number;
    } | null>(null);
    const [query, setQuery] = useState('');
    const [healthFilter, setHealthFilter] = useState<TopologyHealthFilter>('all');
    const [workFilter, setWorkFilter] = useState<TopologyWorkFilter>('all');
    const [layoutPreference, setLayoutPreference] = useState<TopologyLayoutPreference>('auto');
    const [showRelationships, setShowRelationships] = useState(true);
    const [showActivity, setShowActivity] = useState(true);
    const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
    const [viewport, setViewport] = useState<Viewport>({ width: 960, height: 540 });
    const [view, setView] = useState<ViewTransform>({ x: 480, y: 270, scale: 1 });

    const workByAgent = useMemo(() => buildAgentWorkMap(workItems), [workItems]);
    const visibleNodes = useMemo(() => filterTopologyNodes(topology.nodes, {
        query,
        health: healthFilter,
        work: workFilter,
        workByAgent,
    }), [healthFilter, query, topology.nodes, workByAgent, workFilter]);
    const nodeIds = useMemo(() => visibleNodes.map((node) => node.id), [visibleNodes]);
    const layout = useMemo(
        () => computeTopologyLayout(nodeIds, layoutPreference),
        [layoutPreference, nodeIds],
    );
    const visibleIds = useMemo(() => new Set(nodeIds), [nodeIds]);
    const nodeById = useMemo(
        () => new Map(topology.nodes.map((node) => [node.id, node])),
        [topology.nodes],
    );
    const activityByAgent = useMemo(() => {
        const map = new Map<string, WorkforceTopologyActivity>();
        for (const activity of topology.recent_activities) {
            if (!map.has(activity.agent_id)) map.set(activity.agent_id, activity);
        }
        return map;
    }, [topology.recent_activities]);

    const visibleRelationshipEdges = useMemo(
        () => topology.relationship_edges.filter((edge) => (
            visibleIds.has(edge.source_agent_id) && visibleIds.has(edge.target_agent_id)
        )),
        [topology.relationship_edges, visibleIds],
    );
    const visibleActivityEdges = useMemo(
        () => topology.activity_edges.filter((edge) => (
            visibleIds.has(edge.agent_a_id) && visibleIds.has(edge.agent_b_id)
        )),
        [topology.activity_edges, visibleIds],
    );

    const selectedNode = selectedNodeId ? nodeById.get(selectedNodeId) : undefined;
    useEffect(() => {
        if (selectedNodeId && !nodeById.has(selectedNodeId)) setSelectedNodeId(null);
    }, [nodeById, selectedNodeId]);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const observer = new ResizeObserver(([entry]) => {
            if (!entry) return;
            const { width, height } = entry.contentRect;
            if (width > 0 && height > 0) setViewport({ width, height });
        });
        observer.observe(canvas);
        return () => observer.disconnect();
    }, []);

    const fitToView = useCallback(() => {
        const { minX, minY, maxX, maxY } = layout.bounds;
        const contentWidth = Math.max(1, maxX - minX);
        const contentHeight = Math.max(1, maxY - minY);
        const scale = clampScale(Math.min(
            (viewport.width - 72) / contentWidth,
            (viewport.height - 72) / contentHeight,
            1.35,
        ));
        setView({
            scale,
            x: viewport.width / 2 - ((minX + maxX) / 2) * scale,
            y: viewport.height / 2 - ((minY + maxY) / 2) * scale,
        });
    }, [layout.bounds, viewport.height, viewport.width]);

    useEffect(() => {
        fitToView();
    }, [fitToView]);

    const zoom = (factor: number) => {
        setView((current) => {
            const nextScale = clampScale(current.scale * factor);
            const worldCenterX = (viewport.width / 2 - current.x) / current.scale;
            const worldCenterY = (viewport.height / 2 - current.y) / current.scale;
            return {
                scale: nextScale,
                x: viewport.width / 2 - worldCenterX * nextScale,
                y: viewport.height / 2 - worldCenterY * nextScale,
            };
        });
    };

    const onPointerDown = (event: React.PointerEvent<SVGSVGElement>) => {
        if (event.target !== event.currentTarget) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        panRef.current = {
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            originX: view.x,
            originY: view.y,
        };
    };
    const onPointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
        const pan = panRef.current;
        if (!pan || pan.pointerId !== event.pointerId) return;
        setView((current) => ({
            ...current,
            x: pan.originX + event.clientX - pan.startX,
            y: pan.originY + event.clientY - pan.startY,
        }));
    };
    const finishPan = (event: React.PointerEvent<SVGSVGElement>) => {
        if (panRef.current?.pointerId !== event.pointerId) return;
        panRef.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
        }
    };

    const relationshipCountFor = (agentId: string) => topology.relationship_edges.filter(
        (edge) => edge.source_agent_id === agentId || edge.target_agent_id === agentId,
    ).length;
    const interactionCountFor = (agentId: string) => topology.activity_edges.reduce(
        (total, edge) => (
            edge.agent_a_id === agentId || edge.agent_b_id === agentId
                ? total + edge.interaction_count
                : total
        ),
        0,
    );

    return (
        <section className="workforce-topology" aria-labelledby="workforce-topology-title">
            <header className="workforce-topology__header">
                <div>
                    <h2 id="workforce-topology-title">{t('dashboard.topology.title')}</h2>
                    <p>
                        {t('dashboard.topology.subtitle', { hours: topology.window_hours })}
                        <span aria-hidden="true"> · </span>
                        {t('dashboard.topology.visibleCount', {
                            visible: visibleNodes.length,
                            total: topology.nodes.length,
                        })}
                    </p>
                </div>
                <div className="workforce-topology__freshness">
                    {t('dashboard.topology.updated', {
                        time: formatRelativeTime(topology.generated_at, t),
                    })}
                </div>
            </header>

            <div className="workforce-topology__toolbar">
                <label className="workforce-topology__search">
                    <IconSearch size={17} aria-hidden="true" />
                    <input
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        placeholder={t('dashboard.topology.searchPlaceholder')}
                        aria-label={t('dashboard.topology.search')}
                    />
                    {query && (
                        <button
                            type="button"
                            onClick={() => setQuery('')}
                            aria-label={t('dashboard.topology.clearSearch')}
                        >
                            <IconX size={15} />
                        </button>
                    )}
                </label>
                <select
                    value={healthFilter}
                    onChange={(event) => setHealthFilter(event.target.value as TopologyHealthFilter)}
                    aria-label={t('dashboard.topology.healthFilter')}
                >
                    <option value="all">{t('dashboard.topology.allHealth')}</option>
                    <option value="running">{t('dashboard.status.running')}</option>
                    <option value="idle">{t('dashboard.status.idle')}</option>
                    <option value="creating">{t('dashboard.status.creating')}</option>
                    <option value="stopped">{t('dashboard.status.stopped')}</option>
                    <option value="error">{t('dashboard.status.error')}</option>
                </select>
                <select
                    value={workFilter}
                    onChange={(event) => setWorkFilter(event.target.value as TopologyWorkFilter)}
                    aria-label={t('dashboard.topology.workFilter')}
                >
                    <option value="all">{t('dashboard.topology.allWork')}</option>
                    <option value="executing">{t('dashboard.topology.work.executing')}</option>
                    <option value="review">{t('dashboard.topology.work.review')}</option>
                    <option value="approval">{t('dashboard.topology.work.approval')}</option>
                    <option value="blocked">{t('dashboard.topology.work.blocked')}</option>
                    <option value="completed">{t('dashboard.topology.work.completed')}</option>
                    <option value="no_work">{t('dashboard.topology.work.no_work')}</option>
                </select>
                <button
                    type="button"
                    className="workforce-toolbar-button"
                    aria-pressed={showRelationships}
                    onClick={() => setShowRelationships((current) => !current)}
                >
                    <IconCirclesRelation size={17} />
                    {t('dashboard.topology.stableRelations')}
                </button>
                <button
                    type="button"
                    className="workforce-toolbar-button"
                    aria-pressed={showActivity}
                    onClick={() => setShowActivity((current) => !current)}
                >
                    <IconActivity size={17} />
                    {t('dashboard.topology.recentActivity')}
                </button>
            </div>

            <div ref={canvasRef} className="workforce-topology__canvas">
                {visibleNodes.length > 0 ? (
                    <>
                        <svg
                            ref={svgRef}
                            className="workforce-topology__svg"
                            width="100%"
                            height="100%"
                            role="img"
                            aria-label={t('dashboard.topology.canvasLabel', { count: visibleNodes.length })}
                            onPointerDown={onPointerDown}
                            onPointerMove={onPointerMove}
                            onPointerUp={finishPan}
                            onPointerCancel={finishPan}
                        >
                            <defs>
                                <marker
                                    id="workforce-edge-arrow"
                                    viewBox="0 0 10 10"
                                    refX="8"
                                    refY="5"
                                    markerWidth="5"
                                    markerHeight="5"
                                    orient="auto-start-reverse"
                                >
                                    <path d="M 0 0 L 10 5 L 0 10 z" />
                                </marker>
                            </defs>
                            <g transform={`translate(${view.x} ${view.y}) scale(${view.scale})`}>
                                {layout.mode === 'ring' && visibleNodes.length <= 20 && visibleNodes.map((node) => {
                                    const position = layout.positions.get(node.id);
                                    if (!position) return null;
                                    return (
                                        <line
                                            key={`membership-${node.id}`}
                                            className="workforce-edge workforce-edge--membership"
                                            x1={layout.center.x}
                                            y1={layout.center.y}
                                            x2={position.x}
                                            y2={position.y}
                                            vectorEffect="non-scaling-stroke"
                                        />
                                    );
                                })}
                                {showRelationships && visibleRelationshipEdges.map((edge) => {
                                    const source = layout.positions.get(edge.source_agent_id);
                                    const target = layout.positions.get(edge.target_agent_id);
                                    if (!source || !target) return null;
                                    return (
                                        <line
                                            key={edge.id}
                                            className="workforce-edge workforce-edge--relationship"
                                            x1={source.x}
                                            y1={source.y}
                                            x2={target.x}
                                            y2={target.y}
                                            markerEnd="url(#workforce-edge-arrow)"
                                            vectorEffect="non-scaling-stroke"
                                        >
                                            <title>{t(`dashboard.topology.relation.${edge.relation}`, edge.relation)}</title>
                                        </line>
                                    );
                                })}
                                {showActivity && visibleActivityEdges.map((edge) => {
                                    const source = layout.positions.get(edge.agent_a_id);
                                    const target = layout.positions.get(edge.agent_b_id);
                                    if (!source || !target) return null;
                                    return (
                                        <line
                                            key={`activity-${edge.agent_a_id}-${edge.agent_b_id}`}
                                            className="workforce-edge workforce-edge--activity"
                                            x1={source.x}
                                            y1={source.y}
                                            x2={target.x}
                                            y2={target.y}
                                            strokeWidth={Math.min(4, 1.4 + Math.log2(edge.interaction_count + 1) * 0.55)}
                                            vectorEffect="non-scaling-stroke"
                                        >
                                            <title>{t('dashboard.topology.interactionTooltip', {
                                                count: edge.interaction_count,
                                                time: formatRelativeTime(edge.last_activity_at, t),
                                            })}</title>
                                        </line>
                                    );
                                })}

                                <foreignObject
                                    x={layout.center.x - TOPOLOGY_CENTER_WIDTH / 2}
                                    y={layout.center.y - TOPOLOGY_CENTER_HEIGHT / 2}
                                    width={TOPOLOGY_CENTER_WIDTH}
                                    height={TOPOLOGY_CENTER_HEIGHT}
                                >
                                    <div className="workforce-center-node">
                                        <span className="workforce-center-node__icon">
                                            <IconTopologyRing size={22} />
                                        </span>
                                        <strong>{topology.company_name}</strong>
                                        <span>{t('dashboard.topology.companyCenter', { count: visibleNodes.length })}</span>
                                    </div>
                                </foreignObject>

                                {visibleNodes.map((node) => {
                                    const position = layout.positions.get(node.id);
                                    if (!position) return null;
                                    return (
                                        <foreignObject
                                            key={node.id}
                                            x={position.x - TOPOLOGY_NODE_WIDTH / 2}
                                            y={position.y - TOPOLOGY_NODE_HEIGHT / 2}
                                            width={TOPOLOGY_NODE_WIDTH}
                                            height={TOPOLOGY_NODE_HEIGHT}
                                        >
                                            <NodeCard
                                                node={node}
                                                work={workByAgent.get(node.id)}
                                                selected={node.id === selectedNodeId}
                                                onSelect={() => setSelectedNodeId(node.id)}
                                            />
                                        </foreignObject>
                                    );
                                })}
                            </g>
                        </svg>

                        <div className="workforce-topology__view-controls" aria-label={t('dashboard.topology.viewControls')}>
                            <button type="button" onClick={() => zoom(1.2)} title={t('dashboard.topology.zoomIn')} aria-label={t('dashboard.topology.zoomIn')}>
                                <IconPlus size={17} />
                            </button>
                            <button type="button" onClick={() => zoom(1 / 1.2)} title={t('dashboard.topology.zoomOut')} aria-label={t('dashboard.topology.zoomOut')}>
                                <IconMinus size={17} />
                            </button>
                            <button type="button" onClick={fitToView} title={t('dashboard.topology.fit')} aria-label={t('dashboard.topology.fit')}>
                                <IconArrowsMaximize size={17} />
                            </button>
                            <span aria-hidden="true" />
                            <button
                                type="button"
                                className={layout.mode === 'ring' ? 'is-active' : ''}
                                onClick={() => setLayoutPreference('ring')}
                                title={t('dashboard.topology.ringLayout')}
                                aria-label={t('dashboard.topology.ringLayout')}
                            >
                                <IconTopologyRing size={17} />
                            </button>
                            <button
                                type="button"
                                className={layout.mode === 'grid' ? 'is-active' : ''}
                                onClick={() => setLayoutPreference('grid')}
                                title={t('dashboard.topology.gridLayout')}
                                aria-label={t('dashboard.topology.gridLayout')}
                            >
                                <IconLayoutGrid size={17} />
                            </button>
                        </div>

                        <div className="workforce-topology__legend" aria-label={t('dashboard.topology.legend')}>
                            <span><i className="legend-dot is-running" />{t('dashboard.status.running')}</span>
                            <span><i className="legend-dot is-idle" />{t('dashboard.status.idle')}</span>
                            <span><i className="legend-dot is-error" />{t('dashboard.status.error')}</span>
                            <span><i className="legend-line is-relationship" />{t('dashboard.topology.stableRelations')}</span>
                            <span><i className="legend-line is-activity" />{t('dashboard.topology.recentActivity')}</span>
                        </div>

                        <div className="workforce-topology__mobile-list">
                            {visibleNodes.map((node) => {
                                const work = workByAgent.get(node.id);
                                return (
                                    <button
                                        type="button"
                                        key={node.id}
                                        className="workforce-mobile-node"
                                        onClick={() => setSelectedNodeId(node.id)}
                                    >
                                        <AgentAvatar node={node} size={36} />
                                        <span>
                                            <strong>{node.name}</strong>
                                            <small>{node.role_description || t('dashboard.topology.noRole')}</small>
                                        </span>
                                        <span className="workforce-work-chip" data-work={work?.group ?? 'no_work'}>
                                            {workGroupLabel(work?.group ?? 'no_work', t)}
                                        </span>
                                        <i className="workforce-node__status" data-status={node.status} />
                                    </button>
                                );
                            })}
                        </div>
                    </>
                ) : (
                    <div className="workforce-topology__empty">
                        <IconSearch size={24} />
                        <strong>{t('dashboard.topology.noMatches')}</strong>
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => {
                                setQuery('');
                                setHealthFilter('all');
                                setWorkFilter('all');
                            }}
                        >
                            {t('dashboard.topology.resetFilters')}
                        </button>
                    </div>
                )}
            </div>

            {selectedNode && (
                <WorkforceNodeDrawer
                    node={selectedNode}
                    work={workByAgent.get(selectedNode.id)}
                    activity={activityByAgent.get(selectedNode.id)}
                    relationshipCount={relationshipCountFor(selectedNode.id)}
                    interactionCount={interactionCountFor(selectedNode.id)}
                    onClose={() => setSelectedNodeId(null)}
                />
            )}
        </section>
    );
}
