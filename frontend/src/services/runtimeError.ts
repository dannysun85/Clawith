export interface RuntimeErrorContext {
    message: string;
    code?: string;
    traceId?: string;
    runId?: string;
    agentId?: string;
    stage?: string;
}

type RuntimePacket = Record<string, unknown>;

const text = (value: unknown): string | undefined =>
    typeof value === 'string' && value.trim() ? value.trim() : undefined;

export function normalizeRuntimeError(packet: RuntimePacket): RuntimeErrorContext {
    const canonical = packet.error && typeof packet.error === 'object'
        ? packet.error as RuntimePacket
        : {};
    return {
        message: text(canonical.message)
            || text(packet.message)
            || text(packet.content)
            || text(packet.detail)
            || 'Request denied',
        code: text(canonical.code) || text(packet.code) || text(packet.delivery_error),
        traceId: text(canonical.trace_id) || text(packet.trace_id),
        runId: text(canonical.run_id) || text(packet.run_id),
        agentId: text(canonical.agent_id) || text(packet.agent_id),
        stage: text(canonical.stage) || text(packet.stage),
    };
}

export function formatRuntimeErrorDiagnostics(error: RuntimeErrorContext): string {
    return [
        error.code && `Code: ${error.code}`,
        error.traceId && `Trace: ${error.traceId}`,
        error.runId && `Run: ${error.runId}`,
    ].filter(Boolean).join(' · ');
}

export const runtimeErrorDisablesReconnect = (error: RuntimeErrorContext): boolean =>
    error.code === 'model_unavailable'
    || error.code === 'agent_expired'
    || error.code === 'setup_failed'
    || error.code === 'chat_authorization_revoked'
    || error.code === 'authentication_failed';

export const runtimeErrorMarksAgentExpired = (error: RuntimeErrorContext): boolean =>
    error.code === 'agent_expired';

export function runtimeErrorChatCopy(error: RuntimeErrorContext, isChinese: boolean): string {
    if (error.code === 'chat_authorization_revoked' || error.code === 'authentication_failed') {
        return isChinese
            ? '登录状态已更新，请刷新页面后重新发送消息。'
            : 'Your sign-in was refreshed. Reload the page and send again.';
    }
    return error.message || (isChinese ? '请求被拒绝' : 'Request denied');
}
