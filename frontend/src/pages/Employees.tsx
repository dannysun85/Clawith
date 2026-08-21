import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
    IconList,
    IconMessage,
    IconPlus,
    IconRefresh,
    IconSearch,
    IconSettings,
    IconTopologyRing,
    IconUsersGroup,
    IconX,
} from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useOutletContext, useSearchParams } from 'react-router';

import WorkforceTopology from '../components/WorkforceTopology/WorkforceTopology';
import LegacyAssistantOrganizer, {
    type LegacyAssistantAction,
} from '../components/LegacyAssistantOrganizer';
import { useDialog } from '../components/Dialog/DialogProvider';
import {
    ApiError,
    workforceApi,
    agentApi,
    ceoApi,
    type WorkforceTopology as WorkforceTopologyData,
    type WorkforceTopologyNode,
} from '../services/api';
import { useAuthStore } from '../stores';
import type { Agent } from '../types';
import { hasEffectiveCapability } from '../utils/productAccess';
import { topologyExecutionWorkGroup } from '../utils/workforceTopology';
import { SUBSCRIPTION_UPGRADE_PATH } from '../hooks/useAgentCreationLimit';
import './employees.css';

type LayoutOutletContext = {
    openTalentMarket?: () => void;
};

type EmployeeView = 'network' | 'directory';
type EmployeeScope = 'available' | 'managed' | 'governance';
type HealthFilter = 'all' | 'running' | 'idle' | 'attention';
type WorkFilter = 'all' | 'active' | 'blocked' | 'completed' | 'no_work';

function isEmployeeView(value: string | null): value is EmployeeView {
    return value === 'network' || value === 'directory';
}

function isEmployeeScope(value: string | null): value is EmployeeScope {
    return value === 'available' || value === 'managed' || value === 'governance';
}

function upgradeUrlFromError(error: unknown): string {
    if (!(error instanceof ApiError)) return '';

    for (const candidate of [error.details, error.detail]) {
        if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
        const detail = candidate as Record<string, unknown>;
        if (typeof detail.upgrade_url === 'string' && detail.upgrade_url) return detail.upgrade_url;
        if (detail.details && typeof detail.details === 'object' && !Array.isArray(detail.details)) {
            const nestedUrl = (detail.details as Record<string, unknown>).upgrade_url;
            if (typeof nestedUrl === 'string' && nestedUrl) return nestedUrl;
        }
    }

    return error.status === 402 ? SUBSCRIPTION_UPGRADE_PATH : '';
}

function relativeTime(value: string | null | undefined, isChinese: boolean): string {
    if (!value) return isChinese ? '暂无活动' : 'No activity yet';
    const elapsed = Math.max(0, Date.now() - new Date(value).getTime());
    const minutes = Math.floor(elapsed / 60_000);
    if (minutes < 1) return isChinese ? '刚刚' : 'Just now';
    if (minutes < 60) return isChinese ? `${minutes} 分钟前` : `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return isChinese ? `${hours} 小时前` : `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return isChinese ? `${days} 天前` : `${days}d ago`;
}

function healthLabel(status: string, isChinese: boolean): string {
    const labels: Record<string, [string, string]> = {
        running: ['运行中', 'Running'],
        idle: ['待命', 'Idle'],
        creating: ['入职设置中', 'Setting up'],
        stopped: ['已停止', 'Stopped'],
        error: ['需要处理', 'Needs attention'],
    };
    const label = labels[status];
    return label ? label[isChinese ? 0 : 1] : status;
}

function workLabel(node: WorkforceTopologyNode, isChinese: boolean): string {
    const labels: Record<string, [string, string]> = {
        executing: ['执行中', 'Executing'],
        waiting: ['等待中', 'Waiting'],
        review: ['待复核', 'In review'],
        approval: ['待审批', 'Awaiting approval'],
        blocked: ['受阻', 'Blocked'],
        completed: ['已完成', 'Completed'],
    };
    if (!node.execution && !node.work) return isChinese ? '暂无工作' : 'No current work';
    const group = topologyExecutionWorkGroup(node);
    const label = labels[group];
    return label ? label[isChinese ? 0 : 1] : group;
}

function visibilityLabel(visibility: WorkforceTopologyNode['visibility'], isChinese: boolean): string {
    const labels = {
        company: isChinese ? '公司可用' : 'Company',
        private: isChinese ? '仅自己' : 'Private',
        custom: isChinese ? '指定成员' : 'Selected members',
    };
    return labels[visibility];
}

function EmployeeDirectory({
    topology,
    highlightId,
    scope,
    recoveryBusyId,
    onRecover,
}: {
    topology: WorkforceTopologyData;
    highlightId: string | null;
    scope: EmployeeScope;
    recoveryBusyId: string | null;
    onRecover: (agentId: string) => void;
}) {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const isChinese = i18n.language?.startsWith('zh') ?? false;
    const highlightedRef = useRef<HTMLDivElement | null>(null);
    const [query, setQuery] = useState('');
    const [health, setHealth] = useState<HealthFilter>('all');
    const [work, setWork] = useState<WorkFilter>('all');

    const visibleEmployees = useMemo(() => {
        const normalized = query.trim().toLocaleLowerCase();
        return [...topology.nodes]
            .filter((node) => {
                const workGroup = topologyExecutionWorkGroup(node);
                if (normalized && !`${node.name} ${node.role_description}`.toLocaleLowerCase().includes(normalized)) {
                    return false;
                }
                if (health === 'running' && node.status !== 'running') return false;
                if (health === 'idle' && node.status !== 'idle') return false;
                if (health === 'attention' && !['creating', 'stopped', 'error'].includes(node.status)) return false;
                if (work === 'active' && !['executing', 'waiting', 'review', 'approval'].includes(workGroup)) return false;
                if (work === 'blocked' && workGroup !== 'blocked') return false;
                if (work === 'completed' && workGroup !== 'completed') return false;
                if (work === 'no_work' && workGroup !== 'no_work') return false;
                return true;
            })
            .sort((left, right) => {
                if (left.id === highlightId) return -1;
                if (right.id === highlightId) return 1;
                const leftActive = left.last_active_at ? new Date(left.last_active_at).getTime() : 0;
                const rightActive = right.last_active_at ? new Date(right.last_active_at).getTime() : 0;
                return rightActive - leftActive || left.name.localeCompare(right.name);
            });
    }, [health, highlightId, query, topology.nodes, work]);

    useEffect(() => {
        if (!highlightId || !highlightedRef.current) return;
        highlightedRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' });
        highlightedRef.current.focus({ preventScroll: true });
    }, [highlightId, visibleEmployees.length]);

    const resetFilters = () => {
        setQuery('');
        setHealth('all');
        setWork('all');
    };

    return (
        <section className="employee-directory" aria-labelledby="employee-directory-title">
            <header className="employee-directory__header">
                <div>
                    <h2 id="employee-directory-title">{isChinese ? '员工名册' : 'Employee directory'}</h2>
                    <p>
                        {isChinese
                            ? `${scope === 'managed' ? '你拥有管理权的' : scope === 'governance' ? '公司治理范围内的' : '你可以使用的'} ${topology.nodes.length} 名数字员工。`
                            : `${topology.nodes.length} digital employees ${scope === 'managed' ? 'you can manage' : scope === 'governance' ? 'in company governance scope' : 'available to you'}.`}
                    </p>
                </div>
            </header>

            <div className="employee-directory__toolbar">
                <label className="employee-directory__search">
                    <IconSearch size={17} aria-hidden="true" />
                    <input
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        placeholder={isChinese ? '搜索姓名或职责' : 'Search name or responsibility'}
                        aria-label={isChinese ? '搜索员工' : 'Search employees'}
                    />
                    {query && (
                        <button type="button" onClick={() => setQuery('')} aria-label={isChinese ? '清空搜索' : 'Clear search'}>
                            <IconX size={15} />
                        </button>
                    )}
                </label>
                <select value={health} onChange={(event) => setHealth(event.target.value as HealthFilter)}>
                    <option value="all">{isChinese ? '全部健康状态' : 'All health states'}</option>
                    <option value="running">{isChinese ? '运行中' : 'Running'}</option>
                    <option value="idle">{isChinese ? '待命' : 'Idle'}</option>
                    <option value="attention">{isChinese ? '需要关注' : 'Needs attention'}</option>
                </select>
                <select value={work} onChange={(event) => setWork(event.target.value as WorkFilter)}>
                    <option value="all">{isChinese ? '全部工作阶段' : 'All work stages'}</option>
                    <option value="active">{isChinese ? '正在推进' : 'Active work'}</option>
                    <option value="blocked">{isChinese ? '受阻' : 'Blocked'}</option>
                    <option value="completed">{isChinese ? '最近完成' : 'Recently completed'}</option>
                    <option value="no_work">{isChinese ? '暂无工作' : 'No current work'}</option>
                </select>
            </div>

            {visibleEmployees.length > 0 ? (
                <div className="employee-directory__list" role="list">
                    <div className="employee-directory__columns" aria-hidden="true">
                        <span>{isChinese ? '数字员工' : 'Digital employee'}</span>
                        <span>{isChinese ? '健康状态' : 'Health'}</span>
                        <span>{isChinese ? '当前工作' : 'Current work'}</span>
                        <span>{isChinese ? '可见范围' : 'Visibility'}</span>
                        <span>{isChinese ? '最近活动' : 'Last activity'}</span>
                        <span>{isChinese ? '操作' : 'Actions'}</span>
                    </div>
                    {visibleEmployees.map((node) => (
                        <div
                            key={node.id}
                            ref={node.id === highlightId ? highlightedRef : undefined}
                            className={`employee-directory__row${node.id === highlightId ? ' is-highlighted' : ''}`}
                            role="listitem"
                            tabIndex={node.id === highlightId ? -1 : undefined}
                        >
                            <div className="employee-directory__identity">
                                <span className="employee-directory__avatar" aria-hidden="true">
                                    {node.avatar_url
                                        ? <img src={node.avatar_url} alt="" />
                                        : Array.from(node.name.trim())[0]?.toUpperCase() || 'A'}
                                </span>
                                <span>
                                    <strong>{node.name}</strong>
                                    {node.is_system && (
                                        <span className="employee-directory__system-badge">
                                            {isChinese ? '系统岗位' : 'System role'}
                                        </span>
                                    )}
                                    <small>{node.role_description || (isChinese ? '尚未填写职责' : 'No responsibility provided')}</small>
                                </span>
                            </div>
                            <span className="employee-directory__field" data-label={isChinese ? '健康状态' : 'Health'}>
                                <i className="employee-directory__health-dot" data-status={node.status} />
                                {healthLabel(node.status, isChinese)}
                            </span>
                            <span className="employee-directory__field employee-directory__work" data-label={isChinese ? '当前工作' : 'Current work'}>
                                <strong>{workLabel(node, isChinese)}</strong>
                                {(node.execution?.title || node.work?.title) && (
                                    <small>{node.execution?.title || node.work?.title}</small>
                                )}
                            </span>
                            <span className="employee-directory__field" data-label={isChinese ? '可见范围' : 'Visibility'}>
                                {visibilityLabel(node.visibility, isChinese)}
                            </span>
                            <span className="employee-directory__field" data-label={isChinese ? '最近活动' : 'Last activity'}>
                                {relativeTime(node.last_active_at, isChinese)}
                            </span>
                            <span className="employee-directory__actions">
                                <button type="button" className="btn btn-secondary" onClick={() => navigate(`/agents/${node.id}/chat`)}>
                                    <IconMessage size={15} />
                                    {isChinese ? '对话' : 'Chat'}
                                </button>
                                {node.can_manage && (
                                    <button type="button" className="btn btn-ghost" onClick={() => navigate(`/agents/${node.id}/settings#settings`)}>
                                        <IconSettings size={15} />
                                        {scope === 'governance'
                                            ? (isChinese ? '授权与设置' : 'Access & settings')
                                            : (isChinese ? '设置' : 'Settings')}
                                    </button>
                                )}
                                {node.can_manage && ['error', 'stopped'].includes(node.status) && (
                                    <button
                                        type="button"
                                        className="btn btn-ghost"
                                        disabled={recoveryBusyId === node.id}
                                        onClick={() => onRecover(node.id)}
                                    >
                                        <IconRefresh size={15} />
                                        {recoveryBusyId === node.id
                                            ? (isChinese ? '恢复中' : 'Recovering')
                                            : (isChinese ? '恢复' : 'Recover')}
                                    </button>
                                )}
                            </span>
                        </div>
                    ))}
                </div>
            ) : (
                <div className="employee-directory__empty">
                    <IconSearch size={24} />
                    <strong>{isChinese ? '没有匹配的数字员工' : 'No matching digital employees'}</strong>
                    <button type="button" className="btn btn-secondary" onClick={resetFilters}>
                        {isChinese ? '重置筛选' : 'Reset filters'}
                    </button>
                </div>
            )}
        </section>
    );
}

export default function Employees() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const dialog = useDialog();
    const outletContext = useOutletContext<LayoutOutletContext | null>();
    const [searchParams, setSearchParams] = useSearchParams();
    const isChinese = i18n.language?.startsWith('zh') ?? false;
    const user = useAuthStore((state) => state.user);
    const canCreateCompanyAgent = hasEffectiveCapability(user, 'agent.create.company');
    const canCreatePrivateAgent = hasEffectiveCapability(user, 'agent.create.private');
    const canCreateAgent = canCreateCompanyAgent || canCreatePrivateAgent;
    const canGovernAgents = hasEffectiveCapability(user, 'agent.manage.company');
    const currentTenant = user?.tenant_id || localStorage.getItem('current_tenant_id') || '';
    const requestedView = searchParams.get('view');
    const mobileDefault = typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches;
    const view: EmployeeView = isEmployeeView(requestedView)
        ? requestedView
        : mobileDefault
            ? 'directory'
            : 'network';
    const highlightId = searchParams.get('highlight');
    const requestedScope = searchParams.get('scope');
    const scope: EmployeeScope = isEmployeeScope(requestedScope)
        ? (requestedScope === 'governance' && !canGovernAgents ? 'available' : requestedScope)
        : 'available';
    const creationComplete = searchParams.get('created') === '1';
    const [recoveryBusyId, setRecoveryBusyId] = useState<string | null>(null);
    const [legacyBusyId, setLegacyBusyId] = useState<string | null>(null);
    const [actionError, setActionError] = useState('');
    const [actionSuccess, setActionSuccess] = useState('');
    const [upgradeUrl, setUpgradeUrl] = useState('');

    const {
        data: topology,
        isLoading,
        isFetching,
        isError,
        refetch,
    } = useQuery({
        queryKey: ['workforce-topology', currentTenant, 24],
        queryFn: () => workforceApi.topology(24),
        retry: false,
        refetchInterval: (query) => (
            query.state.status === 'error'
            || (typeof document !== 'undefined' && document.visibilityState === 'hidden')
                ? false
                : 15_000
        ),
        refetchIntervalInBackground: false,
        refetchOnMount: 'always',
        refetchOnWindowFocus: 'always',
        refetchOnReconnect: 'always',
    });

    const { data: allAgents = [] } = useQuery({
        queryKey: ['agents', currentTenant],
        queryFn: () => agentApi.list(currentTenant || undefined),
        enabled: Boolean(currentTenant),
        retry: false,
    });

    // CEO orchestrator entry card — only company governors covered by the
    // rollout canary see it; everyone else gets zero entry points.
    const { data: ceoSettings } = useQuery({
        queryKey: ['ceo-orchestrator-settings', currentTenant],
        queryFn: () => ceoApi.settings(),
        enabled: canGovernAgents && Boolean(currentTenant),
        retry: false,
    });

    const retainedAssistants = useMemo(
        () => (allAgents as Agent[])
            .filter((agent) => (
                agent.creator_id === user?.id
                && agent.legacy_assistant_disposition != null
            ))
            .sort((left, right) => (
                new Date(right.created_at).getTime() - new Date(left.created_at).getTime()
            )),
        [allAgents, user?.id],
    );

    const setView = (nextView: EmployeeView) => {
        const next = new URLSearchParams(searchParams);
        next.set('view', nextView);
        setSearchParams(next, { replace: true });
    };

    const setScope = (nextScope: EmployeeScope) => {
        const next = new URLSearchParams(searchParams);
        next.set('scope', nextScope);
        setSearchParams(next, { replace: true });
    };

    const openAddEmployee = () => {
        if (!canCreateAgent) return;
        if (outletContext?.openTalentMarket) {
            outletContext.openTalentMarket();
            return;
        }
        navigate('/agents/new');
    };

    const recoverAgent = async (agentId: string) => {
        setRecoveryBusyId(agentId);
        setActionError('');
        try {
            await agentApi.recover(agentId);
            await refetch({ cancelRefetch: true });
        } catch (nextError) {
            setActionError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setRecoveryBusyId(null);
        }
    };

    const updateLegacyAssistant = async (agent: Agent, action: LegacyAssistantAction) => {
        const disposition = agent.legacy_assistant_disposition ?? 'active';
        const confirmation = action === 'archive'
            ? (isChinese
                ? '归档后会停止执行并从侧栏隐藏；旧对话、文件、Workspace、Agent ID 和深链都会保留，也不会占用员工名额。'
                : 'Archiving stops execution and hides the assistant from the sidebar. Conversations, files, Workspace, Agent ID, and deep links remain, and no employee seat is used.')
            : action === 'convert_to_employee'
                ? (isChinese
                    ? '转为数字员工会占用 1 个员工名额。当前仅自己可见的范围不会自动扩大；需要共享时可在转为员工后另行授权。'
                    : 'Conversion reserves one employee seat. Private access is not expanded automatically; sharing can be configured after conversion.')
                : disposition === 'converted'
                    ? (isChinese
                        ? '撤回后不再占用员工名额，并恢复为仅自己可见的历史助理；转为员工后增加的共享授权会被移除。运行不会自动启动。'
                        : 'Returning to history releases the employee seat, restores private-only history, and removes sharing added after conversion. Runtime does not start automatically.')
                    : (isChinese
                        ? '恢复后会重新出现在历史助理入口；运行不会自动启动，旧内容和对象 ID 保持不变。'
                        : 'Restoring brings back the previous-assistant entry. Runtime does not start automatically, and old content and object IDs remain unchanged.');
        const confirmed = await dialog.confirm(confirmation, {
            title: action === 'archive'
                ? (isChinese ? '归档历史助理' : 'Archive previous assistant')
                : action === 'convert_to_employee'
                    ? (isChinese ? '转为数字员工' : 'Convert to digital employee')
                    : (isChinese ? '恢复为历史助理' : 'Return to previous assistants'),
            danger: action === 'archive',
            confirmLabel: action === 'archive'
                ? (isChinese ? '确认归档' : 'Archive')
                : action === 'convert_to_employee'
                    ? (isChinese ? '确认占用名额并转换' : 'Reserve seat and convert')
                    : (isChinese ? '确认恢复' : 'Restore'),
        });
        if (!confirmed) return;

        setLegacyBusyId(agent.id);
        setActionError('');
        setActionSuccess('');
        setUpgradeUrl('');
        try {
            await agentApi.updateLegacyAssistantDisposition(agent.id, {
                action,
                expected_disposition: disposition,
            });
            setActionSuccess(action === 'archive'
                ? (isChinese ? '历史助理已归档，旧内容仍可从这里访问。' : 'Previous assistant archived; old content remains available here.')
                : action === 'convert_to_employee'
                    ? (isChinese ? '已转为数字员工，并计入员工名额。' : 'Converted to a digital employee and counted as a seat.')
                    : (isChinese ? '已恢复为历史助理。' : 'Returned to previous assistants.'));
            await Promise.all([
                queryClient.invalidateQueries({ queryKey: ['agents'] }),
                queryClient.invalidateQueries({ queryKey: ['workforce-topology'] }),
            ]);
        } catch (nextError: unknown) {
            setActionError(nextError instanceof Error ? nextError.message : String(nextError));
            setUpgradeUrl(upgradeUrlFromError(nextError));
        } finally {
            setLegacyBusyId(null);
        }
    };

    const scopedTopology = useMemo<WorkforceTopologyData | undefined>(() => {
        if (!topology) return undefined;
        const nodes = scope === 'managed'
            ? topology.nodes.filter((node) => node.can_manage)
            : scope === 'governance'
                ? topology.nodes.filter((node) => node.visibility !== 'private')
                : topology.nodes;
        const nodeIds = new Set(nodes.map((node) => node.id));
        return {
            ...topology,
            nodes,
            relationship_edges: topology.relationship_edges.filter((edge) => (
                nodeIds.has(edge.source_agent_id) && nodeIds.has(edge.target_agent_id)
            )),
            activity_edges: topology.activity_edges.filter((edge) => (
                nodeIds.has(edge.agent_a_id) && nodeIds.has(edge.agent_b_id)
            )),
            recent_activities: topology.recent_activities.filter((activity) => nodeIds.has(activity.agent_id)),
        };
    }, [scope, topology]);

    const dismissCreated = () => {
        const next = new URLSearchParams(searchParams);
        next.delete('created');
        setSearchParams(next, { replace: true });
    };

    return (
        <div className="employees-page">
            <header className="employees-page__header">
                <div>
                    <h1>{isChinese ? '数字员工' : 'Digital employees'}</h1>
                    <p>
                        {isChinese
                            ? '管理长期责任角色，在协作网络和完整员工名册之间切换。'
                            : 'Manage long-term accountable roles and switch between the collaboration network and complete roster.'}
                    </p>
                </div>
                {canCreateAgent ? (
                    <button type="button" className="btn btn-primary" onClick={openAddEmployee}>
                        <IconPlus size={16} />
                        {isChinese ? '添加员工' : 'Add employee'}
                    </button>
                ) : (
                    <span className="employees-page__policy-note">
                        {isChinese ? '当前公司策略未授予创建权限' : 'Company policy does not grant creation access'}
                    </span>
                )}
            </header>

            <div className="employees-page__scopes" role="tablist" aria-label={isChinese ? '权限范围' : 'Permission scope'}>
                <button type="button" role="tab" aria-selected={scope === 'available'} className={scope === 'available' ? 'is-active' : ''} onClick={() => setScope('available')}>
                    {isChinese ? '可用员工' : 'Available'}
                    <small>{topology?.nodes.length ?? 0}</small>
                </button>
                <button type="button" role="tab" aria-selected={scope === 'managed'} className={scope === 'managed' ? 'is-active' : ''} onClick={() => setScope('managed')}>
                    {isChinese ? '我管理的员工' : 'Managed by me'}
                    <small>{topology?.nodes.filter((node) => node.can_manage).length ?? 0}</small>
                </button>
                {canGovernAgents && (
                    <button type="button" role="tab" aria-selected={scope === 'governance'} className={scope === 'governance' ? 'is-active' : ''} onClick={() => setScope('governance')}>
                        {isChinese ? '公司治理范围' : 'Company governance'}
                        <small>{topology?.nodes.filter((node) => node.visibility !== 'private').length ?? 0}</small>
                    </button>
                )}
            </div>

            <p className="employees-page__scope-contract" data-testid="workforce-scope-contract">
                {isChinese
                    ? '执行状态：公司可见，敏感详情按权限脱敏；当前工作：仅显示你拥有或可见的工作；关系与活动：仅管理员或受托管理范围。'
                    : 'Execution status is company-visible with sensitive details redacted by permission; current work is limited to work you own or may view; relationships and activity are limited to governors or delegated management scope.'}
            </p>

            <div className="employees-page__tabs" role="tablist" aria-label={isChinese ? '员工视图' : 'Employee views'}>
                <button
                    type="button"
                    role="tab"
                    aria-selected={view === 'network'}
                    className={view === 'network' ? 'is-active' : ''}
                    onClick={() => setView('network')}
                >
                    <IconTopologyRing size={17} />
                    {isChinese ? '协作网络' : 'Collaboration network'}
                </button>
                <button
                    type="button"
                    role="tab"
                    aria-selected={view === 'directory'}
                    className={view === 'directory' ? 'is-active' : ''}
                    onClick={() => setView('directory')}
                >
                    <IconList size={17} />
                    {isChinese ? '员工名册' : 'Employee directory'}
                </button>
            </div>

            {creationComplete && (
                <div className="employees-page__success" role="status">
                    <span>
                        <strong>{isChinese ? '员工已添加' : 'Employee added'}</strong>
                        {isChinese
                            ? '后台正在完成工作区和岗位能力配置；员工状态会在此自动更新。'
                            : 'Workspace and role capabilities are being configured in the background. Status updates here automatically.'}
                    </span>
                    <span className="employees-page__success-actions">
                        {highlightId && (
                            <button type="button" className="btn btn-secondary" onClick={() => navigate(`/agents/${highlightId}/chat`)}>
                                <IconMessage size={15} />
                                {isChinese ? '开始对话' : 'Start chat'}
                            </button>
                        )}
                        <button type="button" className="btn btn-ghost" onClick={dismissCreated} aria-label={isChinese ? '关闭提示' : 'Dismiss'}>
                            <IconX size={16} />
                        </button>
                    </span>
                </div>
            )}

            {actionError && (
                <div className="employees-page__error" role="alert">
                    <span>{actionError}</span>
                    {upgradeUrl && (
                        <button type="button" className="btn btn-secondary" onClick={() => navigate(upgradeUrl)}>
                            {isChinese ? '查看套餐' : 'View plans'}
                        </button>
                    )}
                </div>
            )}
            {actionSuccess && <div className="employees-page__success" role="status">{actionSuccess}</div>}

            <LegacyAssistantOrganizer
                agents={retainedAssistants}
                busyAgentId={legacyBusyId}
                onAction={(agent, action) => void updateLegacyAssistant(agent, action)}
            />

            {canGovernAgents && ceoSettings?.feature_available && (
                <div className="employees-page__ceo-card" data-testid="ceo-orchestrator-entry">
                    <div>
                        <div className="employees-page__ceo-card-title">
                            {isChinese ? '公司 CEO' : 'Company CEO'}
                        </div>
                        <div className="employees-page__ceo-card-desc">
                            {ceoSettings.enabled
                                ? (isChinese
                                    ? (ceoSettings.coordination_enabled
                                        ? '已启用协调型。CEO 可在人工对话中依据能力目录委派任务；自动派发由独立开关控制。'
                                        : '已启用观察型。CEO 汇总业务全景、生成简报并主持晨会；不会下发任务。')
                                    : (ceoSettings.coordination_enabled
                                        ? 'Coordinator enabled. The CEO may delegate from human chat using Directory evidence; autonomous dispatch has a separate switch.'
                                        : 'Observer enabled. The CEO reads the panorama, publishes briefings, and chairs meetings without dispatching work.'))
                                : (isChinese
                                    ? '每家公司可启用一位系统岗位 CEO：业务全景、日报/周报节奏、晨会纪要。不占员工席位，消耗租户 Credits。'
                                    : 'Enable one system-role CEO per company: business panorama, daily/weekly briefings, meeting minutes. Uses no employee seat; consumes tenant Credits.')}
                        </div>
                    </div>
                    {ceoSettings.enabled && ceoSettings.ceo_agent_id ? (
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => navigate(`/agents/${ceoSettings.ceo_agent_id}/chat`)}
                        >
                            {isChinese ? '打开 CEO 详情' : 'Open CEO page'}
                        </button>
                    ) : (
                        <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => navigate('/company-admin/settings/okr')}
                        >
                            {isChinese ? '启用公司 CEO' : 'Enable company CEO'}
                        </button>
                    )}
                </div>
            )}

            {isLoading ? (
                <div className="employees-page__state">{isChinese ? '正在加载数字员工…' : 'Loading digital employees…'}</div>
            ) : isError || !topology ? (
                <div className="employees-page__state">
                    <strong>{isChinese ? '暂时无法加载数字员工' : 'Digital employees could not be loaded'}</strong>
                    <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={isFetching}
                        onClick={() => void refetch({ cancelRefetch: true })}
                    >
                        {isFetching
                            ? (isChinese ? '正在重试…' : 'Retrying…')
                            : (isChinese ? '重试' : 'Retry')}
                    </button>
                </div>
            ) : !scopedTopology || scopedTopology.nodes.length === 0 ? (
                <div className="employees-page__empty">
                    <IconUsersGroup size={34} />
                    <h2>{scope === 'managed'
                        ? (isChinese ? '你还没有受托管理的员工' : 'No employees are managed by you yet')
                        : (isChinese ? '当前范围内没有长期数字员工' : 'No long-term digital employees in this scope')}</h2>
                    <p>
                        {isChinese
                            ? '只有需要长期记忆、定时触发、渠道身份或持续责任的岗位才需要添加为员工。一次性工作可直接从工作台开始。'
                            : 'Add an employee only for work that needs durable memory, triggers, channel identity, or ongoing accountability. Start one-off work from the workbench.'}
                    </p>
                    <div>
                        {canCreateAgent && <button type="button" className="btn btn-primary" onClick={openAddEmployee}>
                            <IconPlus size={16} />
                            {isChinese ? '添加第一个员工' : 'Add the first employee'}
                        </button>}
                        <button type="button" className="btn btn-secondary" onClick={() => navigate('/work')}>
                            {isChinese ? '开始一次性工作' : 'Start one-off work'}
                        </button>
                    </div>
                </div>
            ) : view === 'network' ? (
                <WorkforceTopology topology={scopedTopology} />
            ) : (
                <EmployeeDirectory
                    topology={scopedTopology}
                    highlightId={highlightId}
                    scope={scope}
                    recoveryBusyId={recoveryBusyId}
                    onRecover={(agentId) => void recoverAgent(agentId)}
                />
            )}
        </div>
    );
}
