import { useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import {
    IconArrowLeft,
    IconArrowRight,
    IconRefresh,
    IconTimeline,
} from '@tabler/icons-react';

import { useToast } from '../components/Toast/ToastProvider';
import { workApi, type WorkNextAction, type WorkStatusAxes } from '../services/api';
import { useAuthStore } from '../stores';
import { createRandomUUID } from '../utils/randomUUID';
import './Work.css';


const AXIS_LABELS: Record<keyof WorkStatusAxes, { zh: string; en: string }> = {
    execution: { zh: '任务执行', en: 'Execution' },
    artifact: { zh: '产物版本', en: 'Artifact' },
    quality: { zh: '质量检查', en: 'Quality' },
    runtime_approval: { zh: '运行期审批', en: 'Runtime approval' },
    delivery_approval: { zh: '业务批准', en: 'Delivery approval' },
    delivery: { zh: '正式交付', en: 'Delivery' },
};

const STATUS_LABELS: Record<string, { zh: string; en: string }> = {
    not_started: { zh: '未开始', en: 'Not started' },
    not_required: { zh: '不需要', en: 'Not required' },
    not_requested: { zh: '未发起', en: 'Not requested' },
    missing: { zh: '暂无产物', en: 'Missing' },
    queued: { zh: '已排队', en: 'Queued' },
    running: { zh: '执行中', en: 'Running' },
    waiting: { zh: '等待输入', en: 'Waiting' },
    completed: { zh: '已完成', en: 'Completed' },
    failed: { zh: '失败', en: 'Failed' },
    cancelled: { zh: '已取消', en: 'Cancelled' },
    candidate: { zh: '候选版本', en: 'Candidate' },
    approved: { zh: '已批准', en: 'Approved' },
    rejected: { zh: '已拒绝', en: 'Rejected' },
    superseded: { zh: '已被替代', en: 'Superseded' },
    open: { zh: '进行中', en: 'Open' },
    passed: { zh: '已通过', en: 'Passed' },
    blocked: { zh: '受阻', en: 'Blocked' },
    incomplete: { zh: '不完整', en: 'Incomplete' },
    pending: { zh: '待处理', en: 'Pending' },
    executing: { zh: '审批后执行中', en: 'Executing' },
    succeeded: { zh: '已成功', en: 'Succeeded' },
    ambiguous: { zh: '结果待核对', en: 'Ambiguous' },
    request_changes: { zh: '要求修改', en: 'Changes requested' },
    reconciling: { zh: '核对中', en: 'Reconciling' },
    delivered: { zh: '已交付', en: 'Delivered' },
};

function statusLabel(status: string, isChinese: boolean) {
    const label = STATUS_LABELS[status] || { zh: status, en: status };
    return isChinese ? label.zh : label.en;
}

function runtimeEventLabel(event: string | null | undefined, isChinese: boolean) {
    if (!event) return isChinese ? '已登记' : 'Registered';
    const labels: Record<string, { zh: string; en: string }> = {
        run_created: { zh: '等待执行', en: 'Queued' },
        status_changed: { zh: '执行中', en: 'Running' },
        waiting_started: { zh: '等待输入', en: 'Waiting for input' },
        resumed: { zh: '已恢复', en: 'Resumed' },
        run_completed: { zh: '执行完成', en: 'Execution completed' },
        run_failed: { zh: '执行失败', en: 'Execution failed' },
        run_cancelled: { zh: '执行已取消', en: 'Execution cancelled' },
    };
    const label = labels[event] || { zh: event, en: event };
    return isChinese ? label.zh : label.en;
}

function outcomeNotificationLabel(status: string, isChinese: boolean) {
    const labels: Record<string, { zh: string; en: string }> = {
        not_required: { zh: '无需结果通知', en: 'No outcome notification required' },
        pending: { zh: '结果通知待发送', en: 'Outcome notification pending' },
        delivered: { zh: '结果通知已送达', en: 'Outcome notification delivered' },
        failed: { zh: '结果通知发送失败', en: 'Outcome notification failed' },
    };
    const label = labels[status] || { zh: status, en: status };
    return isChinese ? label.zh : label.en;
}

function actionLabel(action: WorkNextAction, isChinese: boolean) {
    const labels: Record<WorkNextAction['kind'], { zh: string; en: string }> = {
        quality_review: { zh: '提交质量检查', en: 'Submit quality review' },
        runtime_approval: { zh: '处理运行期审批', en: 'Review Runtime approval' },
        delivery_approval: { zh: '批准或要求修改', en: 'Approve or request changes' },
        task_recovery: { zh: '重试任务', en: 'Retry task' },
        delivery_recovery: { zh: '处理交付阻塞', en: 'Resolve delivery issue' },
    };
    return isChinese ? labels[action.kind].zh : labels[action.kind].en;
}

export default function WorkDetail() {
    const { taskId } = useParams<{ taskId: string }>();
    const navigate = useNavigate();
    const { i18n } = useTranslation();
    const isChinese = i18n.language.startsWith('zh');
    const user = useAuthStore((state) => state.user);
    const queryClient = useQueryClient();
    const toast = useToast();
    const retryRequestId = useRef(createRandomUUID());
    const detailQuery = useQuery({
        queryKey: ['work-detail', user?.id, user?.tenant_id, taskId],
        queryFn: () => workApi.getTaskDetail(taskId!),
        enabled: !!taskId && !!user?.tenant_id,
        refetchInterval: (query) => {
            const execution = query.state.data?.status_axes.execution;
            return execution === 'queued' || execution === 'running' || execution === 'waiting'
                ? 5_000
                : false;
        },
    });
    const retryTask = useMutation({
        mutationFn: () => workApi.retryTask(taskId!, retryRequestId.current),
        onSuccess: async () => {
            retryRequestId.current = createRandomUUID();
            await Promise.all([
                queryClient.invalidateQueries({ queryKey: ['work-detail'] }),
                queryClient.invalidateQueries({ queryKey: ['work-index'] }),
                queryClient.invalidateQueries({ queryKey: ['work-inbox'] }),
                queryClient.invalidateQueries({ queryKey: ['work-inbox-count'] }),
            ]);
            toast.success(isChinese ? '新的执行尝试已进入队列' : 'A new attempt has been queued');
        },
        onError: (error: any) => {
            toast.error(isChinese ? '任务重试失败' : 'Could not retry the task', {
                details: error?.message || String(error),
            });
        },
    });

    if (detailQuery.isLoading) {
        return <div className="work-detail-shell work-detail-loading">{isChinese ? '正在加载任务事实…' : 'Loading task facts…'}</div>;
    }
    if (detailQuery.isError || !detailQuery.data) {
        return (
            <div className="work-detail-shell work-detail-loading">
                <strong>{isChinese ? '无法加载任务详情' : 'Could not load task detail'}</strong>
                <button type="button" onClick={() => detailQuery.refetch()}>{isChinese ? '重试' : 'Retry'}</button>
            </div>
        );
    }

    const detail = detailQuery.data;
    const summary = detail.summary;
    const origin = summary.work_statement?.origin || summary.executor_snapshot?.origin;
    const isFullDetail = detail.detail_scope === 'full';
    const workspaceLink = summary.executor_kind === 'group'
        ? detail.links.executor
        : detail.links.formal_delivery || detail.links.executor;
    const runAction = (action: WorkNextAction) => {
        if (action.kind === 'task_recovery') {
            retryTask.mutate();
            return;
        }
        navigate(action.action_url);
    };

    return (
        <div className="work-detail-shell">
            <header className="work-detail-header">
                <button type="button" onClick={() => navigate('/work')}>
                    <IconArrowLeft size={17} />
                    {isChinese ? '返回工作台' : 'Back to Work'}
                </button>
                <button type="button" onClick={() => detailQuery.refetch()} disabled={detailQuery.isFetching}>
                    <IconRefresh size={16} className={detailQuery.isFetching ? 'is-spinning' : ''} />
                    {isChinese ? '刷新事实' : 'Refresh facts'}
                </button>
            </header>

            <section className="work-detail-hero">
                <div className="work-eyebrow">
                    {isFullDetail
                        ? (isChinese ? '统一任务详情' : 'WORK DETAIL')
                        : (isChinese ? '协作任务摘要' : 'COLLABORATION SUMMARY')}
                </div>
                <h1>{summary.title}</h1>
                <p>{summary.intent}</p>
                <div className="work-detail-owner">
                    <span>{isChinese ? '执行责任人' : 'Executor'} · {summary.agent_name}</span>
                    <span>{isChinese ? '当前阶段' : 'Current stage'} · {summary.user_stage}</span>
                    <span>{new Date(summary.updated_at).toLocaleString()}</span>
                </div>
                {origin?.kind === 'group_message' && (
                    <div className="work-detail-origin">
                        <span>{isChinese ? '来源' : 'Origin'} · {isChinese ? '协作群组消息' : 'Group message'}</span>
                        <small>{origin.message_excerpt}</small>
                    </div>
                )}
                {detail.detail_scope === 'collaboration' && (
                    <div className="work-detail-scope-notice" role="note">
                        <strong>{isChinese ? '协作安全视图' : 'Collaboration-safe view'}</strong>
                        <span>{isChinese
                            ? '这里只显示共同任务、责任主体与运行摘要。产物、评审、审批、交付详情及动作仍按原对象权限查看。'
                            : 'Only shared task, ownership, and run context is shown. Artifact, review, approval, delivery details, and actions retain their original object permissions.'}</span>
                    </div>
                )}
            </section>

            <section className="work-axis-grid" aria-label={isChinese ? '独立状态轴' : 'Independent status axes'}>
                {(Object.entries(detail.status_axes) as Array<[keyof WorkStatusAxes, string]>).map(([axis, value]) => (
                    <div key={axis} className={`work-axis work-axis--${value}`}>
                        <span>{isChinese ? AXIS_LABELS[axis].zh : AXIS_LABELS[axis].en}</span>
                        <strong>{statusLabel(value, isChinese)}</strong>
                    </div>
                ))}
            </section>

            {detail.next_actions.length > 0 && (
                <section className="work-detail-section work-next-actions">
                    <div className="work-detail-section-title">
                        <div>
                            <span>{isChinese ? '需要你处理' : 'YOUR ACTIONS'}</span>
                            <h2>{isChinese ? '下一步动作' : 'Next actions'}</h2>
                        </div>
                    </div>
                    <div className="work-action-list">
                        {detail.next_actions.map((action) => (
                            <button
                                type="button"
                                key={action.id}
                                onClick={() => runAction(action)}
                                disabled={action.kind === 'task_recovery' && retryTask.isPending}
                            >
                                <div>
                                    <strong>{actionLabel(action, isChinese)}</strong>
                                    <span>{action.reason_code}</span>
                                </div>
                                <IconArrowRight size={17} />
                            </button>
                        ))}
                    </div>
                </section>
            )}

            <div className="work-detail-columns">
                <section className="work-detail-section">
                    <div className="work-detail-section-title">
                        <div>
                            <span>{isChinese ? '全部尝试' : 'ALL ATTEMPTS'}</span>
                            <h2>{isChinese ? `Runtime 尝试（${detail.runs.length}）` : `Runtime attempts (${detail.runs.length})`}</h2>
                        </div>
                    </div>
                    <div className="work-fact-list">
                        {detail.runs.map((run, index) => (
                            <div key={run.id}>
                                <strong>#{detail.runs.length - index} · {run.run_kind}</strong>
                                <span>
                                    {runtimeEventLabel(run.latest_event, isChinese)}
                                    {' · '}
                                    {outcomeNotificationLabel(run.delivery_status, isChinese)}
                                </span>
                                <small>{new Date(run.created_at).toLocaleString()}</small>
                            </div>
                        ))}
                        {detail.runs.length === 0 && <p>{isChinese ? '尚未登记 Runtime 尝试。' : 'No Runtime attempt registered.'}</p>}
                    </div>
                </section>

                <section className="work-detail-section">
                    <div className="work-detail-section-title">
                        <div>
                            <span>{isChinese ? '正式交付事实' : 'DELIVERY FACTS'}</span>
                            <h2>{isChinese ? '版本、检查与批准' : 'Revisions, reviews, approvals'}</h2>
                        </div>
                    </div>
                    {isFullDetail ? (
                        <dl className="work-fact-counts">
                            <div><dt>{isChinese ? '交付请求' : 'Deliverables'}</dt><dd>{detail.deliverables.length}</dd></div>
                            <div><dt>{isChinese ? '产物版本' : 'Artifacts'}</dt><dd>{detail.artifacts.length}</dd></div>
                            <div><dt>{isChinese ? '质量检查' : 'Reviews'}</dt><dd>{detail.reviews.length}</dd></div>
                            <div><dt>{isChinese ? '批准记录' : 'Approvals'}</dt><dd>{detail.approvals.length}</dd></div>
                        </dl>
                    ) : (
                        <p className="work-detail-scope-note">
                            {isChinese
                                ? '这里只展示协作所需的状态。产物、评审、审批和交付明细仍按原对象权限打开。'
                                : 'This view only shows collaboration-safe status. Artifacts, reviews, approvals, and delivery details retain their original object permissions.'}
                        </p>
                    )}
                    {isFullDetail && detail.artifacts.length > 0 && (
                        <div className="work-detail-artifacts">
                            {detail.artifacts.map((artifact) => (
                                <a
                                    key={artifact.id}
                                    href={`/api/deliverables/artifacts/${artifact.id}/download?inline=true`}
                                    target="_blank"
                                    rel="noreferrer"
                                >
                                    {artifact.artifact_type} · v{artifact.revision_number} · {statusLabel(artifact.status, isChinese)}
                                </a>
                            ))}
                        </div>
                    )}
                    {workspaceLink && (
                        <button className="work-open-executor" type="button" onClick={() => navigate(workspaceLink)}>
                            {summary.executor_kind === 'group'
                                ? (isChinese ? '返回协作群组现场' : 'Open Group workspace')
                                : (isChinese ? '打开执行/交付现场' : 'Open execution or delivery workspace')}
                            <IconArrowRight size={16} />
                        </button>
                    )}
                </section>
            </div>

            <section className="work-detail-section work-timeline">
                <div className="work-detail-section-title">
                    <div>
                        <span>{isChinese ? '只读事实流' : 'READ-ONLY FACT STREAM'}</span>
                        <h2>
                            {isFullDetail
                                ? (isChinese ? '完整时间线' : 'Complete timeline')
                                : (isChinese ? '协作安全时间线' : 'Collaboration-safe timeline')}
                        </h2>
                    </div>
                    <IconTimeline size={22} />
                </div>
                <ol>
                    {detail.timeline.map((event) => (
                        <li key={event.id}>
                            <div className="work-timeline-dot" />
                            <div>
                                <div>
                                    <strong>{event.title}</strong>
                                    {event.status && <span>{statusLabel(event.status, isChinese)}</span>}
                                </div>
                                {event.summary && <p>{event.summary}</p>}
                                <small>{new Date(event.occurred_at).toLocaleString()} · {event.source_type}</small>
                            </div>
                        </li>
                    ))}
                </ol>
            </section>
        </div>
    );
}
