import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import ApprovalPreview, {
    approvalExecutionLabel,
    approvalExecutionErrorLabel,
    approvalNeedsPolling,
    type ApprovalRecord,
    isApprovalApprovable,
} from '../../../components/ApprovalPreview';
import { fetchAuth } from '../utils/fetchAuth';

export function normalizeApprovals(value: unknown): ApprovalRecord[] {
    if (Array.isArray(value)) return value as ApprovalRecord[];
    if (value && typeof value === 'object' && 'items' in value) {
        const items = (value as { items?: unknown }).items;
        return Array.isArray(items) ? items as ApprovalRecord[] : [];
    }
    return [];
}

export default function ApprovalsTab({ agentId, canManage }: { agentId: string; canManage: boolean }) {
    const { i18n } = useTranslation();
    const queryClient = useQueryClient();
    const isChinese = i18n.language?.startsWith('zh');
    const { data: approvalData, isError, refetch: refetchApprovals } = useQuery({
        queryKey: ['agent-approvals', agentId],
        queryFn: () => fetchAuth<unknown>(`/agents/${agentId}/approvals`),
        enabled: !!agentId,
        refetchInterval: (query) => approvalNeedsPolling(normalizeApprovals(query.state.data)) ? 3000 : false,
    });
    const approvals = normalizeApprovals(approvalData);

    const resolveMut = useMutation({
        mutationFn: async ({ approvalId, action }: { approvalId: string; action: 'approve' | 'reject' }) => {
            if (!canManage) return;
            return fetchAuth(`/agents/${agentId}/approvals/${approvalId}/resolve`, {
                method: 'POST',
                body: JSON.stringify({ action }),
            });
        },
        onSuccess: () => {
            refetchApprovals();
            queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
        },
    });

    const pending = approvals.filter((approval) => approval.status === 'pending');
    const resolved = approvals.filter((approval) => approval.status !== 'pending');
    const statusStyle = (status: string) => ({
        padding: '2px 8px',
        borderRadius: '4px',
        fontSize: '11px',
        fontWeight: 600,
        background: status === 'approved'
            ? 'rgba(0,180,120,0.12)'
            : status === 'rejected'
                ? 'rgba(255,80,80,0.12)'
                : 'rgba(255,180,0,0.12)',
        color: status === 'approved'
            ? 'var(--success)'
            : status === 'rejected'
                ? 'var(--error)'
                : 'var(--warning)',
    });

    return (
        <div style={{ padding: '20px 24px' }}>
            {isError && (
                <div style={{ marginBottom: '12px', color: 'var(--error)', fontSize: '13px' }}>
                    {isChinese ? '审批记录加载失败，请稍后重试。' : 'Failed to load approvals. Please try again.'}
                </div>
            )}
            {resolveMut.isError && (
                <div role="alert" style={{ marginBottom: '12px', color: 'var(--error)', fontSize: '13px' }}>
                    {isChinese ? '审批操作失败：' : 'Approval action failed: '}
                    {resolveMut.error instanceof Error ? resolveMut.error.message : String(resolveMut.error)}
                </div>
            )}
            {pending.length > 0 && (
                <>
                    <h4 style={{ margin: '0 0 12px', fontSize: '13px', color: 'var(--warning)' }}>
                        {isChinese ? `${pending.length} 个待审批` : `${pending.length} Pending`}
                    </h4>
                    {pending.map((approval) => (
                        <div
                            key={approval.id}
                            style={{
                                padding: '14px 16px',
                                marginBottom: '8px',
                                borderRadius: '8px',
                                background: 'var(--bg-secondary)',
                                border: '1px solid var(--border-subtle)',
                            }}
                        >
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                                <span style={statusStyle(approval.status)}>{approval.status}</span>
                                <span style={{ fontSize: '13px', fontWeight: 500 }}>{approval.action_type}</span>
                                <span style={{ flex: 1 }} />
                                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                    {approval.created_at ? new Date(approval.created_at).toLocaleString() : ''}
                                </span>
                            </div>
                            <ApprovalPreview details={approval.details} isChinese={isChinese} />
                            {canManage && <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                                <button
                                    className="btn btn-primary"
                                    style={{ padding: '6px 16px', fontSize: '12px' }}
                                    onClick={() => {
                                        if (canManage) resolveMut.mutate({ approvalId: approval.id, action: 'approve' });
                                    }}
                                    disabled={!canManage || resolveMut.isPending || !isApprovalApprovable(approval.details)}
                                    title={!isApprovalApprovable(approval.details)
                                        ? (isChinese ? '审批内容未通过完整性校验，只能拒绝。' : 'Payload integrity is not verified; reject it.')
                                        : undefined}
                                >
                                    {isChinese ? '批准' : 'Approve'}
                                </button>
                                <button
                                    className="btn btn-danger"
                                    style={{ padding: '6px 16px', fontSize: '12px' }}
                                    onClick={() => {
                                        if (canManage) resolveMut.mutate({ approvalId: approval.id, action: 'reject' });
                                    }}
                                    disabled={!canManage || resolveMut.isPending}
                                >
                                    {isChinese ? '拒绝' : 'Reject'}
                                </button>
                            </div>}
                        </div>
                    ))}
                    <div style={{ borderTop: '1px solid var(--border-subtle)', margin: '16px 0' }} />
                </>
            )}

            <h4 style={{ margin: '0 0 12px', fontSize: '13px', color: 'var(--text-secondary)' }}>
                {isChinese ? '审批历史' : 'History'}
            </h4>
            {resolved.length === 0 && pending.length === 0 && (
                <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                    {isChinese ? '暂无审批记录' : 'No approval records'}
                </div>
            )}
            {resolved.map((approval) => (
                <div
                    key={approval.id}
                    style={{
                        padding: '12px 16px',
                        marginBottom: '6px',
                        borderRadius: '8px',
                        background: 'var(--bg-secondary)',
                        border: '1px solid var(--border-subtle)',
                    }}
                >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <span style={statusStyle(approval.status)}>{approval.status}</span>
                        <span style={{ fontSize: '12px' }}>{approval.action_type}</span>
                        <span style={{ fontSize: '12px', color: approval.execution_status === 'ambiguous' ? 'var(--warning)' : 'var(--text-secondary)' }}>
                            {approvalExecutionLabel(approval, isChinese)}
                        </span>
                        {approvalExecutionErrorLabel(approval, isChinese) && (
                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                {approvalExecutionErrorLabel(approval, isChinese)}
                            </span>
                        )}
                        <span style={{ flex: 1 }} />
                        <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                            {approval.resolved_at ? new Date(approval.resolved_at).toLocaleString() : ''}
                        </span>
                    </div>
                    <ApprovalPreview details={approval.details} isChinese={isChinese} />
                </div>
            ))}
        </div>
    );
}
