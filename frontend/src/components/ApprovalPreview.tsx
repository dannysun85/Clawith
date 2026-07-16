export type PublicApprovalDetails = {
    payload_state?: string;
    approvable?: boolean;
    message?: string;
    tool?: string;
    parameters?: unknown;
};

export type ApprovalRecord = {
    id: string;
    agent_id: string | null;
    agent_name?: string | null;
    action_type: string;
    details?: PublicApprovalDetails | null;
    status: string;
    created_at?: string | null;
    resolved_at?: string | null;
    execution_status?: string | null;
    execution_error_code?: string | null;
    execution_result_summary?: { outcome_code?: string | null } | null;
    execution_available?: boolean;
    execution_paused_reason?: string | null;
};

export function isApprovalApprovable(details: PublicApprovalDetails | null | undefined): boolean {
    return details?.approvable === true && details.payload_state === 'verified';
}

export function approvalNeedsPolling(value: unknown): boolean {
    if (!Array.isArray(value)) return false;
    return value.some((approval) => {
        if (!approval || typeof approval !== 'object') return false;
        const record = approval as ApprovalRecord;
        return record.status === 'pending'
            || (record.execution_available !== false && record.execution_status === 'pending')
            || record.execution_status === 'executing';
    });
}

export function approvalExecutionLabel(approval: ApprovalRecord, isChinese: boolean): string {
    if (approval.status === 'pending') return isChinese ? '等待审批' : 'Awaiting approval';
    if (approval.status === 'rejected') return isChinese ? '已拒绝，不会执行' : 'Rejected; will not execute';
    if (
        approval.execution_available === false
        && approval.execution_status === 'pending'
    ) {
        return isChinese
            ? '已批准，但本版本自动执行已暂停；没有副作用发生'
            : 'Approved, but automatic execution is paused in this release; no side effect ran';
    }
    const outcomeCode = approval.execution_result_summary?.outcome_code || '';
    const outcomeLabels: Record<string, [string, string]> = {
        DouyinUserActionRequired: ['抖音发布包已生成，仍需用户确认；不代表已公开发布', 'Douyin package ready; user confirmation required; not publicly published'],
        DouyinAcceptedPendingReview: ['抖音已受理，等待审核；不代表已公开发布', 'Accepted by Douyin; review pending; not publicly published'],
        DouyinPublishedPendingVerification: ['已收到抖音发布回调，等待最终验证', 'Douyin publish callback received; final verification pending'],
        DouyinUserConfirmedPendingVerification: ['用户已确认，等待抖音最终验证', 'User confirmed; final Douyin verification pending'],
        DouyinConfirmed: ['抖音操作已由官方流程确认', 'Douyin operation confirmed by the official workflow'],
    };
    if (approval.execution_status === 'succeeded' && outcomeLabels[outcomeCode]) {
        return outcomeLabels[outcomeCode][isChinese ? 0 : 1];
    }
    const labels: Record<string, [string, string]> = {
        pending: ['已批准，等待后台执行', 'Approved; queued for execution'],
        executing: ['后台执行中', 'Executing in background'],
        succeeded: ['执行成功', 'Execution succeeded'],
        failed: ['执行失败', 'Execution failed'],
        ambiguous: ['结果不确定，请人工核对；系统不会自动重试', 'Outcome unknown; verify manually; no automatic replay'],
        legacy: ['旧审批记录，不能执行', 'Legacy approval; cannot execute'],
        invalid: ['审批内容无效，不能执行', 'Invalid approval; cannot execute'],
        not_required: ['无需执行', 'No execution required'],
    };
    const pair = labels[approval.execution_status || ''];
    return pair ? pair[isChinese ? 0 : 1] : (isChinese ? '执行状态未知' : 'Execution status unknown');
}

export function approvalExecutionErrorLabel(
    approval: ApprovalRecord,
    isChinese: boolean,
): string | null {
    if (!['failed', 'ambiguous'].includes(approval.execution_status || '')) return null;
    const rawCode = String(approval.execution_error_code || 'ExecutionFailed');
    const safeCode = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$/.test(rawCode)
        ? rawCode
        : 'ExecutionFailed';
    const known: Record<string, [string, string]> = {
        StaleExecutionClaim: ['Worker 中断后结果无法确认', 'Worker stopped; outcome cannot be confirmed'],
        CancelledDuringDispatch: ['执行中断，结果无法确认', 'Dispatch was cancelled; outcome cannot be confirmed'],
        CancelledBeforeDispatch: ['执行前已取消', 'Cancelled before dispatch'],
        WorkspaceMutationRejected: ['工作区写入被拒绝', 'Workspace mutation was rejected'],
        CodeOutcomeNotDurable: ['Code 执行结果未达到可持久确认标准', 'Code outcome was not durably confirmed'],
        DouyinBlocked: ['抖音任务被确定性条件阻止，未执行', 'Douyin task was blocked before execution'],
        DouyinPermissionMissing: ['抖音授权权限不足，未执行', 'Douyin permission is missing; not executed'],
        DouyinAuthenticationRequired: ['抖音账号需要重新授权，未执行', 'Douyin account requires reauthorization; not executed'],
        DouyinRateLimited: ['抖音官方限流，本次未执行', 'Douyin rate limited the request; not executed'],
        DouyinRejected: ['抖音官方明确拒绝，本次未执行', 'Douyin explicitly rejected the request; not executed'],
        DouyinInvalidBusinessStatus: ['抖音返回了未支持的业务状态', 'Douyin returned an unsupported business status'],
        DouyinOutcomeNotConfirmed: ['抖音返回结果无法确认', 'Douyin outcome could not be confirmed'],
        DouyinVerificationRequired: ['抖音写入结果未知；禁止重试，请先在官方后台核验', 'Douyin write outcome unknown; do not retry before official verification'],
    };
    const message = known[safeCode]?.[isChinese ? 0 : 1];
    return message
        ? `${message} (${safeCode})`
        : `${isChinese ? '安全错误代码' : 'Safe error code'}: ${safeCode}`;
}

export default function ApprovalPreview({
    details,
    isChinese,
}: {
    details: PublicApprovalDetails | null | undefined;
    isChinese: boolean;
}) {
    if (!details) {
        return (
            <div style={{ color: 'var(--error)', fontSize: 12 }}>
                {isChinese ? '审批内容不可用，只能拒绝。' : 'Approval payload unavailable; reject it.'}
            </div>
        );
    }

    return (
        <div style={{ marginTop: 10, borderTop: '1px solid var(--border-subtle)', paddingTop: 10 }}>
            {details.tool && (
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 6 }}>
                    {isChinese ? '工具' : 'Tool'}: <code>{details.tool}</code>
                </div>
            )}
            {details.message && (
                <div style={{ fontSize: 12, color: 'var(--error)', marginBottom: 8 }}>
                    {details.message}
                </div>
            )}
            {details.parameters !== undefined && (
                <div>
                    <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 5 }}>
                        {isChinese ? '完整审批参数（敏感字段已脱敏）' : 'Complete approval parameters (secrets redacted)'}
                    </div>
                    <pre
                        data-testid="approval-parameters"
                        style={{
                            margin: 0,
                            maxHeight: 320,
                            overflow: 'auto',
                            whiteSpace: 'pre-wrap',
                            overflowWrap: 'anywhere',
                            padding: 10,
                            borderRadius: 6,
                            background: 'var(--bg-tertiary, var(--bg-primary))',
                            border: '1px solid var(--border-subtle)',
                            fontSize: 11,
                            lineHeight: 1.55,
                        }}
                    >
                        {JSON.stringify(details.parameters, null, 2)}
                    </pre>
                </div>
            )}
        </div>
    );
}
