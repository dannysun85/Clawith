import { useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconArrowRight, IconCheck, IconRobot, IconUserCheck, IconX } from '@tabler/icons-react';

import { useToast } from '../../components/Toast/ToastProvider';
import {
    workApi,
    type WorkTaskDraft,
    type WorkTaskPreflight,
} from '../../services/api';
import type { GroupMember, GroupMessage } from '../../types/group';
import { createRandomUUID } from '../../utils/randomUUID';


interface CreateGroupTaskModalProps {
    groupId: string;
    groupName: string;
    sessionId: string;
    sessionTitle: string;
    sourceMessage: GroupMessage;
    members: GroupMember[];
    onClose: () => void;
    onCreated: (taskId: string) => void;
}

const defaultTitle = (content: string) => {
    const firstLine = content.split('\n').find((line) => line.trim())?.trim() || 'Group task';
    return firstLine.slice(0, 80);
};

export default function CreateGroupTaskModal({
    groupId,
    groupName,
    sessionId,
    sessionTitle,
    sourceMessage,
    members,
    onClose,
    onCreated,
}: CreateGroupTaskModalProps) {
    const { t } = useTranslation();
    const toast = useToast();
    const agentMembers = useMemo(
        () => members.filter(
            (member) => member.participant_type === 'agent' && !member.is_deleted,
        ),
        [members],
    );
    const [title, setTitle] = useState(() => defaultTitle(sourceMessage.content));
    const [intent, setIntent] = useState(sourceMessage.content);
    const [primaryOwnerId, setPrimaryOwnerId] = useState('');
    const [collaboratorIds, setCollaboratorIds] = useState<string[]>([]);
    const [preflight, setPreflight] = useState<WorkTaskPreflight | null>(null);
    const [checking, setChecking] = useState(false);
    const [creating, setCreating] = useState(false);
    const clientRequestId = useRef(createRandomUUID());

    const selectedAgentParticipantIds = useMemo(
        () => primaryOwnerId
            ? [
                primaryOwnerId,
                ...collaboratorIds.filter((participantId) => participantId !== primaryOwnerId),
            ]
            : [],
        [collaboratorIds, primaryOwnerId],
    );
    const draft = useMemo<WorkTaskDraft>(() => ({
        title: title.trim(),
        intent: intent.trim(),
        work_type: 'general',
        priority: 'medium',
        routing_mode: 'manual',
        executor_kind: 'group',
        group_id: groupId,
        group_session_id: sessionId,
        group_agent_participant_ids: selectedAgentParticipantIds,
        source_kind: 'group_message',
        source_group_id: groupId,
        source_session_id: sessionId,
        source_message_id: sourceMessage.id,
    }), [groupId, intent, selectedAgentParticipantIds, sessionId, sourceMessage.id, title]);
    const draftKey = JSON.stringify(draft);
    const confirmedKey = useRef<string | null>(null);
    const currentPreflight = confirmedKey.current === draftKey ? preflight : null;
    const canCheck = title.trim().length > 0 && intent.trim().length >= 3 && primaryOwnerId.length > 0;

    const choosePrimaryOwner = (participantId: string) => {
        setPrimaryOwnerId(participantId);
        setCollaboratorIds((current) => current.filter((id) => id !== participantId));
    };

    const toggleCollaborator = (participantId: string) => {
        if (participantId === primaryOwnerId) return;
        setCollaboratorIds((current) => (
            current.includes(participantId)
                ? current.filter((id) => id !== participantId)
                : [...current, participantId]
        ));
    };

    const check = async () => {
        if (!canCheck || checking) return;
        setChecking(true);
        try {
            const result = await workApi.preflightTask(draft);
            confirmedKey.current = draftKey;
            setPreflight(result);
        } catch (error: any) {
            toast.error(t('groups.taskPreflightFailed', '任务检查失败'), {
                details: error?.message || String(error),
            });
        } finally {
            setChecking(false);
        }
    };

    const create = async () => {
        if (!currentPreflight || currentPreflight.capability_status === 'unavailable' || creating) return;
        setCreating(true);
        try {
            const result = await workApi.createTask({
                ...draft,
                client_request_id: clientRequestId.current,
                confirmation_fingerprint: currentPreflight.confirmation_fingerprint,
            });
            toast.success(result.created
                ? t('groups.taskCreated', '正式任务已创建')
                : t('groups.taskAlreadyCreated', '已打开这条消息关联的任务'));
            onCreated(result.item.task_id || result.item.id);
        } catch (error: any) {
            const existingTaskId = error?.code === 'group_message_already_converted'
                && error?.details
                && typeof error.details === 'object'
                && typeof error.details.task_id === 'string'
                ? error.details.task_id
                : null;
            if (existingTaskId) {
                toast.info(t(
                    'groups.taskAlreadyCreated',
                    '这条消息已经关联正式任务，正在打开已有任务',
                ));
                onCreated(existingTaskId);
                return;
            }
            toast.error(t('groups.taskCreateFailed', '正式任务创建失败'), {
                details: error?.message || String(error),
            });
        } finally {
            setCreating(false);
        }
    };

    return (
        <div className="group-modal-backdrop" onClick={() => !creating && onClose()}>
            <div className="group-modal group-task-modal" onClick={(event) => event.stopPropagation()}>
                <div className="group-modal-header">
                    <div>
                        <span className="group-task-modal-eyebrow">
                            {groupName} · {sessionTitle}
                        </span>
                        <h3>{t('groups.createFormalTask', '从消息创建正式任务')}</h3>
                    </div>
                    <button
                        type="button"
                        className="group-icon-btn"
                        aria-label={t('common.close', '关闭')}
                        title={t('common.close', '关闭')}
                        onClick={onClose}
                        disabled={creating}
                    >
                        <IconX size={16} />
                    </button>
                </div>

                <div className="group-task-source">
                    <span>{t('groups.sourceMessage', '来源消息')}</span>
                    <p>{sourceMessage.content}</p>
                    <small>{new Date(sourceMessage.created_at).toLocaleString()}</small>
                </div>

                <label className="group-task-field">
                    <span>{t('groups.taskTitle', '任务标题')}</span>
                    <input value={title} maxLength={500} onChange={(event) => setTitle(event.target.value)} />
                </label>
                <label className="group-task-field">
                    <span>{t('groups.taskObjective', '任务目标与边界')}</span>
                    <textarea value={intent} maxLength={4000} onChange={(event) => setIntent(event.target.value)} />
                </label>

                <div className="group-task-agent-picker">
                    <div>
                        <strong>{t('groups.taskAgents', '参与执行的 Agent')}</strong>
                        <span>{t(
                            'groups.taskOwnerOrder',
                            '必须明确选择唯一第一责任人；协作者不会共享第一责任。',
                        )}</span>
                    </div>
                    <span className="group-task-role-label">
                        {t('groups.choosePrimaryOwner', '第一责任人（必选）')}
                    </span>
                    <div className="group-task-agent-grid">
                        {agentMembers.map((member) => {
                            const isOwner = primaryOwnerId === member.participant_id;
                            return (
                                <button
                                    type="button"
                                    key={member.participant_id}
                                    className={isOwner ? 'is-selected is-owner' : ''}
                                    aria-pressed={isOwner}
                                    onClick={() => choosePrimaryOwner(member.participant_id)}
                                >
                                    <span>{isOwner ? <IconUserCheck size={14} /> : <IconRobot size={14} />}</span>
                                    <strong>{member.display_name}</strong>
                                    <small>{isOwner
                                        ? t('groups.primaryOwner', '第一责任人')
                                        : t('groups.setPrimaryOwner', '设为第一责任人')}</small>
                                </button>
                            );
                        })}
                    </div>
                    {primaryOwnerId && agentMembers.length > 1 && (
                        <>
                            <span className="group-task-role-label">
                                {t('groups.chooseCollaborators', '协作者（可选）')}
                            </span>
                            <div className="group-task-agent-grid">
                                {agentMembers
                                    .filter((member) => member.participant_id !== primaryOwnerId)
                                    .map((member) => {
                                        const isCollaborator = collaboratorIds.includes(member.participant_id);
                                        return (
                                            <button
                                                type="button"
                                                key={member.participant_id}
                                                className={isCollaborator ? 'is-selected' : ''}
                                                aria-pressed={isCollaborator}
                                                onClick={() => toggleCollaborator(member.participant_id)}
                                            >
                                                <span>{isCollaborator ? <IconCheck size={14} /> : <IconRobot size={14} />}</span>
                                                <strong>{member.display_name}</strong>
                                                <small>{isCollaborator
                                                    ? t('groups.collaboratorSelected', '已加入协作')
                                                    : t('groups.addCollaborator', '添加为协作者')}</small>
                                            </button>
                                        );
                                    })}
                            </div>
                        </>
                    )}
                    {agentMembers.length === 0 && (
                        <p>{t('groups.noTaskAgents', '这个 Group 没有可执行的 Agent，暂时不能创建正式任务。')}</p>
                    )}
                </div>

                {currentPreflight && (
                    <div className={`group-task-preflight group-task-preflight--${currentPreflight.capability_status}`}>
                        <IconCheck size={16} />
                        <div>
                            <strong>{t('groups.taskStatementReady', '工作说明已生成，等待最终确认')}</strong>
                            <span>{currentPreflight.executor_proposal.agent_name} · {currentPreflight.capability_status}</span>
                            <small>{currentPreflight.cost_note}</small>
                            <small>
                                {currentPreflight.approval_required
                                    ? t('groups.taskApprovalRequired', '启动前需要审批')
                                    : t('groups.taskApprovalCheckedLater', '高风险运行期动作仍会单独审批')}
                            </small>
                            {currentPreflight.reasons.map((reason) => (
                                <small key={reason}>{reason}</small>
                            ))}
                            <small>{t(
                                'groups.taskTruthHint',
                                '确认后才会创建 Task；原消息仍保留在当前会话。',
                            )}</small>
                        </div>
                    </div>
                )}

                <div className="group-create-footer">
                    <span className="group-create-count">
                        {primaryOwnerId
                            ? t('groups.taskResponsibilityConfirmed', '1 位责任人 · {{count}} 位协作者', { count: collaboratorIds.length })
                            : t('groups.primaryOwnerRequired', '尚未选择第一责任人')}
                    </span>
                    <div className="group-create-actions">
                        <button type="button" className="btn btn-sm" onClick={onClose} disabled={creating}>
                            {t('common.cancel', '取消')}
                        </button>
                        {!currentPreflight ? (
                            <button type="button" className="btn btn-sm btn-primary" disabled={!canCheck || checking} onClick={check}>
                                {checking ? t('common.loading', '加载中...') : t('groups.reviewTaskStatement', '检查工作说明')}
                                <IconArrowRight size={14} />
                            </button>
                        ) : (
                            <button
                                type="button"
                                className="btn btn-sm btn-primary"
                                disabled={creating || currentPreflight.capability_status === 'unavailable'}
                                onClick={create}
                            >
                                {creating ? t('common.loading', '加载中...') : t('groups.confirmCreateTask', '确认并创建任务')}
                                <IconArrowRight size={14} />
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
