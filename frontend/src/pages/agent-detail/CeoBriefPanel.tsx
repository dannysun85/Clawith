import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { ceoApi } from '../../services/api';

/**
 * CEO "业务全景" (business panorama) read-only block for the Agent detail page.
 *
 * Renders only when this Agent is the tenant's currently enabled CEO and the
 * rollout canary covers the tenant — every other Agent page is untouched.
 * The block is read-only: a refreshable snapshot plus the manual meeting start
 * (double-gated: rollout AND the tenant's own enabled switches).
 */
export default function CeoBriefPanel({ agentId }: { agentId: string | undefined }) {
    const { i18n } = useTranslation();
    const zh = i18n.language?.startsWith('zh');
    const queryClient = useQueryClient();
    const [meetingMessage, setMeetingMessage] = useState('');

    const { data: ceoStatus } = useQuery({
        queryKey: ['ceo-orchestrator-status'],
        queryFn: () => ceoApi.status(),
        retry: false,
    });

    const visible = Boolean(
        agentId
        && ceoStatus?.feature_available
        && ceoStatus.configured
        && ceoStatus.enabled
        && ceoStatus.ceo_agent_id === agentId,
    );

    const {
        data: brief,
        isFetching,
        isError,
        refetch,
    } = useQuery({
        queryKey: ['ceo-company-brief', agentId],
        queryFn: () => ceoApi.companyBrief(agentId!),
        enabled: visible,
        retry: false,
        refetchInterval: (query) => (query.state.status === 'error' ? false : 60_000),
    });

    const meetingMutation = useMutation({
        mutationFn: (kind: 'morning' | 'weekly') => ceoApi.startMeeting(agentId!, kind),
        onSuccess: (result) => {
            setMeetingMessage(
                zh
                    ? `会议已注册为后台运行（执行 ${result.trigger_execution_id.slice(0, 8)}…），纪要完成后会发到 CEO 会话。`
                    : `Meeting registered as a background run (${result.trigger_execution_id.slice(0, 8)}…); minutes land in the CEO session when done.`,
            );
            queryClient.invalidateQueries({ queryKey: ['ceo-company-brief', agentId] });
        },
        onError: (error: unknown) => {
            setMeetingMessage(error instanceof Error ? error.message : String(error));
        },
    });

    if (!visible || !ceoStatus) return null;

    const snapshot = brief?.snapshot;

    return (
        <section
            className="card"
            data-testid="ceo-brief-panel"
            style={{ margin: '12px 0', padding: '16px 20px' }}
        >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                <div>
                    <div style={{ fontWeight: 600, fontSize: '14px', color: 'var(--text-primary)' }}>
                        {zh ? '业务全景（只读）' : 'Business panorama (read-only)'}
                        {snapshot?.truncated && (
                            <span style={{ marginLeft: 8, fontSize: 11, color: 'var(--text-tertiary)' }}>
                                {zh ? '已按长度上限截断' : 'truncated to length budget'}
                            </span>
                        )}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: 2 }}>
                        {zh ? '来自员工拓扑与 OKR 读模型的只读组合，不写入任何业务数据。' : 'A read-only composition of the workforce topology and OKR read models.'}
                    </div>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <button
                        type="button"
                        className="btn btn-ghost"
                        disabled={isFetching}
                        onClick={() => void refetch({ cancelRefetch: true })}
                    >
                        {isFetching ? (zh ? '刷新中…' : 'Refreshing…') : (zh ? '刷新' : 'Refresh')}
                    </button>
                    <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={meetingMutation.isPending}
                        onClick={() => meetingMutation.mutate('morning')}
                    >
                        {meetingMutation.isPending ? (zh ? '注册中…' : 'Starting…') : (zh ? '开始晨会' : 'Start morning meeting')}
                    </button>
                    <button
                        type="button"
                        className="btn btn-ghost"
                        disabled={meetingMutation.isPending}
                        onClick={() => meetingMutation.mutate('weekly')}
                    >
                        {zh ? '开始周会' : 'Start weekly meeting'}
                    </button>
                </div>
            </div>

            {meetingMessage && (
                <div style={{ marginTop: 10, fontSize: 12, color: 'var(--text-secondary)' }} role="status">
                    {meetingMessage}
                </div>
            )}

            {isError ? (
                <div style={{ marginTop: 12, fontSize: 12, color: 'var(--danger, #dc2626)' }}>
                    {zh ? '暂时无法加载业务全景。' : 'The business panorama could not be loaded.'}
                </div>
            ) : snapshot ? (
                <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 }}>
                    <PanoramaStat
                        label={zh ? '员工 / 窗口内活跃' : 'Employees / active'}
                        value={`${snapshot.employee_total} / ${snapshot.employee_active_in_window}`}
                    />
                    <PanoramaStat
                        label={zh ? '受阻工作' : 'Blocked work'}
                        value={String(snapshot.work_blocked)}
                        tone={snapshot.work_blocked > 0 ? 'blocked' : 'normal'}
                    />
                    <PanoramaStat
                        label={zh ? '进行中（执行/复核/审批）' : 'In progress (exec/review/approval)'}
                        value={`${snapshot.work_executing} / ${snapshot.work_review} / ${snapshot.work_approval}`}
                    />
                    <PanoramaStat
                        label={zh ? '窗口内完成' : 'Completed in window'}
                        value={String(snapshot.work_completed_recent)}
                    />
                    <PanoramaStat
                        label={zh ? 'OKR 跟踪成员 / 今日已报' : 'OKR tracked / reported today'}
                        value={`${snapshot.okr_tracked_members} / ${snapshot.okr_reports_today_submitted}`}
                    />
                </div>
            ) : (
                <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {zh ? '正在加载业务全景…' : 'Loading the business panorama…'}
                </div>
            )}

            {snapshot && (snapshot.blocked_items.length > 0 || snapshot.in_progress_items.length > 0) && (
                <div style={{ marginTop: 12, fontSize: 12, lineHeight: 1.7 }}>
                    {snapshot.blocked_items.length > 0 && (
                        <div>
                            <strong style={{ color: 'var(--danger, #dc2626)' }}>{zh ? '阻塞：' : 'Blocked: '}</strong>
                            {snapshot.blocked_items.map((item) => `${item.agent_name}「${item.title}」`).join('；')}
                        </div>
                    )}
                    {snapshot.in_progress_items.length > 0 && (
                        <div>
                            <strong>{zh ? '进行中：' : 'In progress: '}</strong>
                            {snapshot.in_progress_items.slice(0, 8).map((item) => `${item.agent_name}「${item.title}」`).join('；')}
                            {snapshot.in_progress_items.length > 8 && (zh ? ' 等' : ' …')}
                        </div>
                    )}
                </div>
            )}

        </section>
    );
}

function PanoramaStat({ label, value, tone = 'normal' }: { label: string; value: string; tone?: 'normal' | 'blocked' }) {
    return (
        <div style={{
            padding: '10px 12px',
            borderRadius: 8,
            border: '1px solid var(--border-subtle)',
            background: tone === 'blocked' ? 'rgba(220,38,38,0.06)' : 'var(--bg-secondary)',
        }}>
            <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{label}</div>
            <div style={{ fontSize: 15, fontWeight: 600, color: tone === 'blocked' ? 'var(--danger, #dc2626)' : 'var(--text-primary)' }}>{value}</div>
        </div>
    );
}
