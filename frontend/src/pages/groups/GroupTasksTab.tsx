import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { IconArrowRight, IconChecklist, IconRefresh } from '@tabler/icons-react';

import { groupApi } from '../../services/groupApi';
import { useAuthStore } from '../../stores';


interface GroupTasksTabProps {
    groupId: string;
    sessionId?: string;
}

const ACTIVE_EXECUTION = new Set(['queued', 'running', 'waiting']);
const TERMINAL_RUN_EVENTS = new Set(['run_completed', 'run_failed', 'run_cancelled']);

const runStateLabel = (event: string | null, isChinese: boolean) => {
    if (!event) return isChinese ? '已登记' : 'Registered';
    const labels: Record<string, [string, string]> = {
        run_created: ['等待执行', 'Queued'],
        status_changed: ['执行中', 'Running'],
        waiting_started: ['等待中', 'Waiting'],
        resumed: ['已恢复', 'Resumed'],
        run_completed: ['已完成', 'Completed'],
        run_failed: ['失败', 'Failed'],
        run_cancelled: ['已取消', 'Cancelled'],
    };
    return labels[event]?.[isChinese ? 0 : 1] || event.replace(/_/g, ' ');
};

const outcomeNotificationLabel = (status: string, isChinese: boolean) => {
    const labels: Record<string, [string, string]> = {
        not_required: ['无需结果通知', 'No outcome notification required'],
        pending: ['结果通知待发送', 'Outcome notification pending'],
        delivered: ['结果通知已送达', 'Outcome notification delivered'],
        failed: ['结果通知发送失败', 'Outcome notification failed'],
    };
    return labels[status]?.[isChinese ? 0 : 1] || status.replace(/_/g, ' ');
};

export default function GroupTasksTab({ groupId, sessionId }: GroupTasksTabProps) {
    const { t, i18n } = useTranslation();
    const isChinese = i18n.language.startsWith('zh');
    const navigate = useNavigate();
    const tenantId = useAuthStore((state) => state.user?.tenant_id);
    const tasksQuery = useQuery({
        queryKey: ['group-tasks', tenantId, groupId, sessionId],
        queryFn: () => groupApi.tasks(groupId, sessionId),
        enabled: Boolean(tenantId && groupId),
        refetchInterval: (query) => (
            query.state.data?.some((task) => ACTIVE_EXECUTION.has(task.status_axes.execution))
                ? 5_000
                : false
        ),
    });

    if (tasksQuery.isLoading) {
        return <div className="group-task-empty">{t('groups.tasksLoading', '正在读取关联任务…')}</div>;
    }
    if (tasksQuery.isError) {
        return (
            <div className="group-task-empty group-task-empty--error">
                <span>{t('groups.tasksLoadFailed', '关联任务加载失败')}</span>
                <button type="button" onClick={() => tasksQuery.refetch()}>
                    <IconRefresh size={14} />
                    {t('common.retry', '重试')}
                </button>
            </div>
        );
    }

    const tasks = tasksQuery.data || [];
    if (tasks.length === 0) {
        return (
            <div className="group-task-empty">
                <IconChecklist size={22} />
                <strong>{t('groups.noLinkedTasks', '这个会话还没有正式任务')}</strong>
                <span>{t(
                    'groups.noLinkedTasksHint',
                    '普通消息仍是协作内容；需要时从某条消息明确创建任务。',
                )}</span>
            </div>
        );
    }

    return (
        <div className="group-task-list">
            {tasks.map((task) => {
                const failedRuns = task.runs.filter((run) => run.latest_event === 'run_failed');
                const activeRuns = task.runs.filter(
                    (run) => !run.latest_event || !TERMINAL_RUN_EVENTS.has(run.latest_event),
                );
                const visibleRuns = failedRuns.length > 0 ? failedRuns : activeRuns.slice(0, 3);
                return (
                    <button
                        type="button"
                        className="group-task-card"
                        key={task.task_id}
                        onClick={() => navigate(task.work_link)}
                    >
                        <div className="group-task-card-topline">
                            <span>{task.user_stage}</span>
                            <small>{new Date(task.updated_at).toLocaleString()}</small>
                        </div>
                        <strong>{task.title}</strong>
                        <p>{task.intent}</p>
                        <div className="group-task-owner">
                            <span>
                                {t('groups.primaryOwner', '第一责任人')} · {task.primary_owner_agent_name}
                                {task.participants.length > 1 && (
                                    <> · {t('groups.collaboratorCount', '{{count}} 位协作者', { count: task.participants.length - 1 })}</>
                                )}
                            </span>
                            <IconArrowRight size={15} />
                        </div>
                        {visibleRuns.length > 0 && (
                            <div className={`group-task-runs ${failedRuns.length > 0 ? 'has-failure' : ''}`}>
                                {visibleRuns.map((run) => (
                                    <span key={run.id}>
                                        {run.agent_name || t('groups.groupPlanner', 'Group 规划器')}
                                        {' · '}
                                        {runStateLabel(run.latest_event, isChinese)}
                                        {' · '}
                                        {outcomeNotificationLabel(run.delivery_status, isChinese)}
                                    </span>
                                ))}
                            </div>
                        )}
                        {task.next_actions.length > 0 && (
                            <span className="group-task-action-count">
                                {t('groups.yourPendingActions', '你有 {{count}} 项待处理', { count: task.next_actions.length })}
                            </span>
                        )}
                    </button>
                );
            })}
        </div>
    );
}
