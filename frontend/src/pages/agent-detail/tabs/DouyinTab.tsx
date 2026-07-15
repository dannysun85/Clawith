import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconCheck, IconClock, IconExternalLink, IconLink, IconMessageCircle, IconSend } from '@tabler/icons-react';

import { useToast } from '../../../components/Toast/ToastProvider';
import { fetchAuth } from '../utils/fetchAuth';

export default function DouyinTab({ agentId, canManage }: { agentId: string; canManage: boolean }) {
    const { t, i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const qc = useQueryClient();
    const toast = useToast();
    const [title, setTitle] = useState('');
    const [body, setBody] = useState('');
    const [contentType, setContentType] = useState<'video' | 'image'>('video');
    const [mediaUrl, setMediaUrl] = useState('');

    const { data, isLoading } = useQuery({
        queryKey: ['douyin-agent-dashboard', agentId],
        queryFn: () => fetchAuth<any>(`/douyin/agent/${agentId}/dashboard`),
        enabled: !!agentId,
        refetchInterval: (query) => {
            const jobs = (query.state.data as any)?.publish_jobs;
            if (!Array.isArray(jobs)) return 15000;
            return jobs.some((job: any) => ['approval_required', 'preparing_share_package', 'creating'].includes(job.status)) ? 3000 : 15000;
        },
    });

    const createJob = useMutation({
        mutationFn: () => fetchAuth<any>('/douyin/publish-jobs', {
            method: 'POST',
            body: JSON.stringify({
                agent_id: agentId,
                account_id: data?.account?.id || null,
                content_type: contentType,
                title,
                body,
                asset_refs: mediaUrl.trim()
                    ? [contentType === 'video' ? { video_path: mediaUrl.trim() } : { image_path: mediaUrl.trim() }]
                    : [],
            }),
        }),
        onSuccess: () => {
            setTitle('');
            setBody('');
            setMediaUrl('');
            qc.invalidateQueries({ queryKey: ['douyin-agent-dashboard', agentId] });
            qc.invalidateQueries({ queryKey: ['agent-approvals', agentId] });
            toast.success(isZh ? '已创建发布审批任务' : 'Publish approval task created');
        },
        onError: (err: any) => toast.error(isZh ? '创建失败' : 'Create failed', { details: String(err?.message || err) }),
    });

    const confirmPublish = useMutation({
        mutationFn: (jobId: string) => fetchAuth<any>(`/douyin/publish-jobs/${jobId}/confirm-user-publish`, { method: 'POST' }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['douyin-agent-dashboard', agentId] });
            toast.success(isZh ? '已记录用户确认' : 'Confirmation recorded');
        },
        onError: (err: any) => toast.error(isZh ? '确认失败' : 'Confirm failed', { details: String(err?.message || err) }),
    });

    const account = data?.account;
    const jobs = data?.publish_jobs || [];
    const operations = data?.operations || [];
    const snapshots = data?.metric_snapshots || [];
    const comments = data?.comments || [];
    const capabilityRows = account?.capabilities || [];

    const badgeClass = (status: string) => status === 'active' || status === 'ready' || status === 'succeeded' || status === 'approved'
        ? 'badge badge-success'
        : status === 'failed' || status === 'blocked' || status === 'permission_missing' || status === 'needs_reauth'
            ? 'badge badge-error'
            : 'badge badge-warning';

    const statusLabel = (status: string) => {
        if (status === 'verification_required') {
            return isZh ? '待人工核验' : 'Manual verification required';
        }
        if (status === 'created_reviewing') {
            return isZh ? '已受理，待审核' : 'Accepted; review pending';
        }
        if (status === 'awaiting_user_publish') {
            return isZh ? '待用户在抖音确认' : 'User confirmation required';
        }
        return status;
    };

    if (isLoading) {
        return <div style={{ padding: '24px', color: 'var(--text-tertiary)' }}>{t('common.loading')}</div>;
    }

    return (
        <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>
            <section className="card" style={{ padding: 18 }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14, flexWrap: 'wrap' }}>
                    <div style={{ width: 40, height: 40, borderRadius: 8, background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 750 }}>DY</div>
                    <div style={{ flex: '1 1 320px', minWidth: 240 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                            <h3 style={{ margin: 0, fontSize: 16 }}>{isZh ? '抖音运营' : 'Douyin Operations'}</h3>
                            <span className={badgeClass(account?.status || 'missing')}>{account ? account.status : (isZh ? '未连接' : 'Not connected')}</span>
                        </div>
                        <p style={{ margin: '8px 0 0', fontSize: 13, color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
                            {account
                                ? `${account.nickname || account.open_id} · ${isZh ? '最后同步' : 'Last sync'}: ${account.last_sync_at ? new Date(account.last_sync_at).toLocaleString() : (isZh ? '未同步' : 'Never')}`
                                : (data?.message || (isZh ? '需要先在企业设置连接抖音账号。' : 'Connect a Douyin account in company settings first.'))}
                        </p>
                    </div>
                    {!account && (
                        <button className="btn btn-primary" type="button" onClick={() => { window.location.href = '/enterprise#douyin'; }}>
                            <IconLink size={15} stroke={1.7} />
                            {isZh ? '连接抖音账号' : 'Connect account'}
                        </button>
                    )}
                </div>
                {account && (
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 10, marginTop: 14 }}>
                        {capabilityRows.map((row: any) => (
                            <div key={row.key} style={{ border: '1px solid var(--border-subtle)', borderRadius: 8, padding: '10px 12px', display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                                <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{row.label}</span>
                                <span className={badgeClass(row.status)}>{row.status === 'ready' ? (isZh ? '已授权' : 'Ready') : (isZh ? '缺权限' : 'Missing')}</span>
                            </div>
                        ))}
                    </div>
                )}
            </section>

            {account && canManage && (
                <section className="card" style={{ padding: 18 }}>
                    <h4 style={{ margin: '0 0 12px', fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <IconSend size={16} stroke={1.8} />
                        {isZh ? '创建发布审批任务' : 'Create Publish Approval'}
                    </h4>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 10 }}>
                        <select className="input" value={contentType} onChange={(e) => setContentType(e.target.value as 'video' | 'image')}>
                            <option value="video">{isZh ? '视频' : 'Video'}</option>
                            <option value="image">{isZh ? '图片' : 'Image'}</option>
                        </select>
                        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={isZh ? '标题' : 'Title'} />
                        <input className="input" value={mediaUrl} onChange={(e) => setMediaUrl(e.target.value)} placeholder={isZh ? '公开视频/图片素材 URL' : 'Public video/image URL'} />
                    </div>
                    <textarea className="input" value={body} onChange={(e) => setBody(e.target.value)} placeholder={isZh ? '文案，审批前不会发布' : 'Caption; nothing is published before approval'} style={{ marginTop: 10, minHeight: 82, resize: 'vertical' }} />
                    <div style={{ marginTop: 10, display: 'flex', justifyContent: 'flex-end' }}>
                        <button className="btn btn-primary" type="button" disabled={!title.trim() || !mediaUrl.trim() || createJob.isPending} onClick={() => createJob.mutate()}>
                            {createJob.isPending ? t('common.loading') : (isZh ? '生成审批任务' : 'Create approval')}
                        </button>
                    </div>
                </section>
            )}

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16 }}>
                <section className="card" style={{ padding: 18 }}>
                    <h4 style={{ margin: '0 0 12px', fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <IconClock size={16} stroke={1.8} />
                        {isZh ? '发布任务' : 'Publish Jobs'}
                    </h4>
                    {jobs.length === 0 ? (
                        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{isZh ? '暂无发布任务' : 'No publish jobs'}</div>
                    ) : jobs.slice(0, 8).map((job: any) => (
                        <div key={job.id} style={{ padding: '10px 0', borderTop: '1px solid var(--border-subtle)' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                                <span className={badgeClass(job.status)}>{statusLabel(job.status)}</span>
                                <span style={{ fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{job.title}</span>
                            </div>
                            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 4 }}>
                                {job.created_at ? new Date(job.created_at).toLocaleString() : ''} · {job.approval_status} · {job.publish_mode}
                            </div>
                            {job.response_summary?.message && (
                                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 6, lineHeight: 1.45 }}>
                                    {job.response_summary.message}
                                </div>
                            )}
                            {job.status === 'verification_required' && (
                                <div role="alert" style={{ fontSize: 12, color: 'var(--warning-text, #9a6700)', marginTop: 8, lineHeight: 1.5 }}>
                                    {isZh
                                        ? '结果未知：必须先去抖音官方后台核验，禁止重新提交同一素材。'
                                        : 'Outcome unknown: verify in Douyin first. Do not resubmit the same media.'}
                                </div>
                            )}
                            {canManage && job.status === 'approval_required' && (
                                <a
                                    className="btn btn-secondary"
                                    href={`/agents/${agentId}/settings#approvals`}
                                    style={{ marginTop: 8 }}
                                >
                                    {isZh ? '查看完整参数后审批' : 'Review full payload'}
                                </a>
                            )}
                            {job.share_schema_url && (
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
                                    <a className="btn btn-primary" href={job.share_schema_url}>
                                        <IconExternalLink size={14} stroke={1.7} />
                                        {isZh ? '打开抖音确认发布' : 'Open Douyin'}
                                    </a>
                                    {canManage && job.status === 'awaiting_user_publish' && (
                                        <button
                                            className="btn btn-secondary"
                                            type="button"
                                            disabled={confirmPublish.isPending}
                                            onClick={() => confirmPublish.mutate(job.id)}
                                        >
                                            {isZh ? '已在抖音发布' : 'Published in Douyin'}
                                        </button>
                                    )}
                                </div>
                            )}
                        </div>
                    ))}
                </section>

                <section className="card" style={{ padding: 18 }}>
                    <h4 style={{ margin: '0 0 12px', fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <IconExternalLink size={16} stroke={1.8} />
                        {isZh ? '近期操作' : 'Recent Operations'}
                    </h4>
                    {operations.length === 0 ? (
                        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{isZh ? '暂无操作记录' : 'No operations'}</div>
                    ) : operations.slice(0, 8).map((op: any) => (
                        <div key={op.id} style={{ padding: '10px 0', borderTop: '1px solid var(--border-subtle)' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                <span className={badgeClass(op.status)}>{statusLabel(op.status)}</span>
                                <span style={{ fontSize: 13, fontWeight: 600 }}>{op.operation_type}</span>
                            </div>
                            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 4 }}>
                                {op.created_at ? new Date(op.created_at).toLocaleString() : ''}
                            </div>
                            {op.status === 'verification_required' && (
                                <div role="alert" style={{ fontSize: 12, color: 'var(--warning-text, #9a6700)', marginTop: 6, lineHeight: 1.5 }}>
                                    {isZh
                                        ? '结果未知：请先在抖音核验，禁止重复回复。'
                                        : 'Outcome unknown: verify in Douyin before sending another reply.'}
                                </div>
                            )}
                        </div>
                    ))}
                </section>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16 }}>
                <section className="card" style={{ padding: 18 }}>
                    <h4 style={{ margin: '0 0 12px', fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <IconCheck size={16} stroke={1.8} />
                        {isZh ? '数据快照' : 'Metric Snapshots'}
                    </h4>
                    {snapshots.length === 0 ? (
                        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{isZh ? '暂无同步数据' : 'No synced data'}</div>
                    ) : snapshots.slice(0, 6).map((snapshot: any) => (
                        <div key={snapshot.id} style={{ padding: '10px 0', borderTop: '1px solid var(--border-subtle)', fontSize: 13 }}>
                            <div>{snapshot.metric_type} · {snapshot.data_freshness}</div>
                            <div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{new Date(snapshot.captured_at).toLocaleString()}</div>
                        </div>
                    ))}
                </section>

                <section className="card" style={{ padding: 18 }}>
                    <h4 style={{ margin: '0 0 12px', fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <IconMessageCircle size={16} stroke={1.8} />
                        {isZh ? '评论分诊' : 'Comment Triage'}
                    </h4>
                    {comments.length === 0 ? (
                        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{isZh ? '暂无已同步评论' : 'No synced comments'}</div>
                    ) : comments.slice(0, 6).map((comment: any) => (
                        <div key={comment.id} style={{ padding: '10px 0', borderTop: '1px solid var(--border-subtle)', fontSize: 13 }}>
                            <div style={{ display: 'flex', gap: 8 }}>
                                <span className={badgeClass(comment.risk_level)}>{comment.risk_level}</span>
                                <span>{comment.content}</span>
                            </div>
                        </div>
                    ))}
                </section>
            </div>
        </div>
    );
}
