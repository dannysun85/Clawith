import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
    IconArrowRight,
    IconCheck,
    IconFileDescription,
    IconPhoto,
    IconPresentation,
    IconRefresh,
    IconSparkles,
    IconUsers,
    IconVideo,
} from '@tabler/icons-react';

import {
    agentApi,
    workApi,
    type WorkItem,
    type WorkTaskDraft,
    type WorkTaskPreflight,
} from '../services/api';
import { useAuthStore } from '../stores';
import { createRandomUUID } from '../utils/randomUUID';
import { partitionAgentRoles } from '../utils/productRoles';
import {
    clearWorkDraft,
    loadWorkDraft,
    saveWorkDraft,
    workDraftStorageKey,
} from '../utils/workDraftPersistence';
import { useToast } from '../components/Toast/ToastProvider';
import { groupApi } from '../services/groupApi';
import './Work.css';


const QUICK_STARTS = [
    { id: 'general', zh: '通用任务', en: 'General task', icon: IconSparkles, prompt: '' },
    { id: 'image', zh: '图片 Brief', en: 'Image brief', icon: IconPhoto, prompt: '请整理一份商业图片制作 brief：确认用途、受众、尺寸、品牌约束和交付格式。本任务只整理可确认的 brief，不调用图片生成、不声称已经交付正式产物；brief 确认后应进入 Agent 对话的正式交付流程。' },
    { id: 'video', zh: '视频 Brief', en: 'Video brief', icon: IconVideo, prompt: '请整理一份带人物商业视频的制作 brief：确认受众、平台、时长、人物、脚本、镜头、声音和交付格式。本任务只整理 brief，不调用视频生成、不声称已经交付正式产物；brief 确认后应进入 Agent 对话的正式交付流程。' },
    { id: 'presentation', zh: 'PPT Brief', en: 'PPT brief', icon: IconPresentation, prompt: '请整理一份正式汇报 PPT 的制作 brief：确认受众、场景、页数、品牌、内容约束、版式与配图要求。本任务只整理 brief，不调用 PPT 生成、不声称已经交付 PPTX；brief 确认后应进入 Agent 对话的正式交付流程。' },
    { id: 'document', zh: '报告 Brief', en: 'Report brief', icon: IconFileDescription, prompt: '请整理一份正式报告的制作 brief：确认用途、读者、篇幅、格式、证据来源和审批要求。本任务只整理 brief，不声称已经交付正式文档。' },
] satisfies Array<{
    id: WorkTaskDraft['work_type'];
    zh: string;
    en: string;
    icon: typeof IconSparkles;
    prompt: string;
}>;

const EXPECTED_OUTPUT_LABELS: Record<string, { zh: string; en: string }> = {
    task_result: { zh: '可直接查看的任务结果', en: 'A visible task result' },
    confirmed_image_brief: { zh: '经确认的商业图片 Brief', en: 'A confirmed commercial image brief' },
    confirmed_video_brief: { zh: '经确认的商业视频 Brief', en: 'A confirmed commercial video brief' },
    confirmed_presentation_brief: { zh: '经确认的 PPT Brief', en: 'A confirmed presentation brief' },
    confirmed_document_brief: { zh: '经确认的报告 Brief', en: 'A confirmed document brief' },
};

const STAGE_LABELS: Record<string, { zh: string; en: string }> = {
    task: { zh: '任务已登记', en: 'Task registered' },
    execution: { zh: '执行中', en: 'In progress' },
    completed: { zh: '执行已完成', en: 'Execution complete' },
    artifact: { zh: '产物待检查', en: 'Artifact ready' },
    review: { zh: '质量检查', en: 'Quality review' },
    approval: { zh: '等待批准', en: 'Awaiting approval' },
    delivery: { zh: '已正式交付', en: 'Delivered' },
    blocked: { zh: '需要处理', en: 'Needs attention' },
    cancelled: { zh: '已取消', en: 'Cancelled' },
};

function stageLabel(stage: string, isChinese: boolean) {
    const label = STAGE_LABELS[stage] || { zh: stage, en: stage };
    return isChinese ? label.zh : label.en;
}

function WorkCard({ item, isChinese }: { item: WorkItem; isChinese: boolean }) {
    const navigate = useNavigate();
    const isFormal = item.delivery_mode === 'formal_deliverable';
    const canContinueAsFormal = item.kind === 'task'
        && item.user_stage === 'completed'
        && ['image', 'video', 'presentation'].includes(String(item.work_statement?.work_type || ''))
        && !item.deliverable_id;
    return (
        <article className="work-card">
            <div className="work-card-main">
                <div className="work-card-heading">
                    <span className={`work-stage work-stage--${item.user_stage}`}>
                        {stageLabel(item.user_stage, isChinese)}
                    </span>
                    {item.work_type && <span className="work-type">{item.work_type}</span>}
                    <span className="work-type">
                        {item.delivery_mode === 'formal_deliverable'
                            ? (isChinese ? '正式交付流' : 'Formal delivery')
                            : (isChinese ? '任务模式' : 'Task only')}
                    </span>
                </div>
                <h3>{item.title}</h3>
                <p>{item.intent}</p>
                <div className="work-card-meta">
                    <span>{item.executor_kind === 'temporary_expert'
                        ? (isChinese ? `临时专家 · ${item.executor_snapshot.expert_role || ''}` : `Temporary expert · ${item.executor_snapshot.expert_role || ''}`)
                        : item.executor_kind === 'group'
                            ? (isChinese
                                ? `Group · ${item.executor_snapshot.group_name || ''} · 责任人 ${item.agent_name}`
                                : `Group · ${item.executor_snapshot.group_name || ''} · Owner ${item.agent_name}`)
                            : `${item.agent_name}`}</span>
                    <span>{new Date(item.updated_at).toLocaleString()}</span>
                </div>
            </div>
            {item.latest_update && (
                <details className="work-result" open={item.user_stage === 'completed'}>
                    <summary>
                        {item.user_stage === 'completed'
                            ? (isChinese ? '查看任务结果' : 'View task result')
                            : (isChinese ? '查看最新进展' : 'View latest update')}
                    </summary>
                    <div>{item.latest_update}</div>
                </details>
            )}
            {item.artifacts.length > 0 && (
                <div className="work-artifacts">
                    {item.artifacts.slice(0, 3).map((artifact) => (
                        <a
                            key={artifact.id}
                            href={`/api/deliverables/artifacts/${artifact.id}/download?inline=true`}
                            target="_blank"
                            rel="noreferrer"
                        >
                            {artifact.artifact_type} · v{artifact.revision_number}
                        </a>
                    ))}
                </div>
            )}
            <div className="work-card-actions">
                <button type="button" onClick={() => navigate(item.deep_link)}>
                    {item.executor_kind === 'group'
                        ? (isChinese ? '打开协作现场' : 'Open Group workspace')
                        : isFormal
                        ? (isChinese ? '打开交付现场' : 'Open delivery workspace')
                        : (isChinese ? '打开执行者' : 'Open executor')}
                </button>
                {canContinueAsFormal && (
                    <button
                        type="button"
                        onClick={() => navigate(item.formal_delivery_link || item.deep_link)}
                    >
                        {isChinese ? '继续正式交付' : 'Continue to formal delivery'}
                    </button>
                )}
                {(item.task_id || item.deliverable_id) && item.user_stage === 'delivery' && (
                    <button
                        type="button"
                        onClick={() => {
                            const query = new URLSearchParams();
                            if (item.task_id) query.set('task', item.task_id);
                            if (item.deliverable_id) query.set('delivery', item.deliverable_id);
                            navigate(`/plaza?${query.toString()}`);
                        }}
                    >
                        {isChinese ? '沉淀为团队经验' : 'Distill as team experience'}
                    </button>
                )}
            </div>
        </article>
    );
}

export default function Work() {
    const { i18n } = useTranslation();
    const isChinese = i18n.language.startsWith('zh');
    const user = useAuthStore((state) => state.user);
    const queryClient = useQueryClient();
    const navigate = useNavigate();
    const toast = useToast();
    const draftStorageKey = workDraftStorageKey(user?.id, user?.tenant_id);
    const restoredDraft = useMemo(() => {
        if (!draftStorageKey || typeof window === 'undefined') return null;
        return loadWorkDraft(window.sessionStorage, draftStorageKey);
    }, []); // The active tenant change is handled by the key-change effect below.
    const [title, setTitle] = useState(restoredDraft?.title || '');
    const [intent, setIntent] = useState(restoredDraft?.intent || '');
    const [workType, setWorkType] = useState<WorkTaskDraft['work_type']>(restoredDraft?.workType || 'general');
    const [priority, setPriority] = useState<'low' | 'medium' | 'high' | 'urgent'>(restoredDraft?.priority || 'medium');
    const [executorKind, setExecutorKind] = useState<WorkTaskDraft['executor_kind']>(restoredDraft?.executorKind || 'personal_assistant');
    const [agentId, setAgentId] = useState(restoredDraft?.agentId || '');
    const [expertRole, setExpertRole] = useState(restoredDraft?.expertRole || '');
    const [groupId, setGroupId] = useState(restoredDraft?.groupId || '');
    const [groupSessionId, setGroupSessionId] = useState(restoredDraft?.groupSessionId || '');
    const [groupAgentParticipantIds, setGroupAgentParticipantIds] = useState<string[]>(restoredDraft?.groupAgentParticipantIds || []);
    const [clientRequestId, setClientRequestId] = useState(restoredDraft?.clientRequestId || createRandomUUID());
    const [preflight, setPreflight] = useState<{
        draftKey: string;
        result: WorkTaskPreflight;
    } | null>(null);
    const previousDraftStorageKey = useRef(draftStorageKey);
    const restoringTenantDraft = useRef(false);

    useEffect(() => {
        if (previousDraftStorageKey.current === draftStorageKey) return;
        previousDraftStorageKey.current = draftStorageKey;
        restoringTenantDraft.current = true;
        const next = draftStorageKey && typeof window !== 'undefined'
            ? loadWorkDraft(window.sessionStorage, draftStorageKey)
            : null;
        setTitle(next?.title || '');
        setIntent(next?.intent || '');
        setWorkType(next?.workType || 'general');
        setPriority(next?.priority || 'medium');
        setExecutorKind(next?.executorKind || 'personal_assistant');
        setAgentId(next?.agentId || '');
        setExpertRole(next?.expertRole || '');
        setGroupId(next?.groupId || '');
        setGroupSessionId(next?.groupSessionId || '');
        setGroupAgentParticipantIds(next?.groupAgentParticipantIds || []);
        setClientRequestId(next?.clientRequestId || createRandomUUID());
        setPreflight(null);
    }, [draftStorageKey]);

    useEffect(() => {
        if (!draftStorageKey || typeof window === 'undefined') return;
        if (restoringTenantDraft.current) {
            restoringTenantDraft.current = false;
            return;
        }
        if (!title.trim() && !intent.trim()) {
            clearWorkDraft(window.sessionStorage, draftStorageKey);
            return;
        }
        saveWorkDraft(window.sessionStorage, draftStorageKey, {
            version: 1,
            title,
            intent,
            workType,
            priority,
            executorKind,
            agentId,
            expertRole,
            groupId,
            groupSessionId,
            groupAgentParticipantIds,
            clientRequestId,
        });
    }, [
        agentId,
        clientRequestId,
        draftStorageKey,
        executorKind,
        expertRole,
        groupAgentParticipantIds,
        groupId,
        groupSessionId,
        intent,
        priority,
        title,
        workType,
    ]);

    const workQuery = useQuery({
        queryKey: ['work-index', user?.tenant_id],
        queryFn: () => workApi.list(20),
        refetchInterval: 15_000,
    });
    const agentsQuery = useQuery({
        queryKey: ['agents', user?.tenant_id],
        queryFn: () => agentApi.list(user?.tenant_id || undefined),
        enabled: !!user?.tenant_id,
    });
    const groupsQuery = useQuery({
        queryKey: ['work-groups', user?.tenant_id],
        queryFn: () => groupApi.list(),
        enabled: !!user?.tenant_id && executorKind === 'group',
    });
    const groupSessionsQuery = useQuery({
        queryKey: ['work-group-sessions', groupId],
        queryFn: () => groupApi.sessions(groupId),
        enabled: executorKind === 'group' && !!groupId,
    });
    const groupMembersQuery = useQuery({
        queryKey: ['work-group-members', groupId],
        queryFn: () => groupApi.members(groupId),
        enabled: executorKind === 'group' && !!groupId,
    });
    const roles = useMemo(
        () => partitionAgentRoles(
            agentsQuery.data || [],
            workQuery.data?.personal_assistant_agent_id,
        ),
        [agentsQuery.data, workQuery.data?.personal_assistant_agent_id],
    );

    const taskDraft = useMemo<WorkTaskDraft>(() => ({
        title: title.trim() || intent.trim().split(/\n/)[0].slice(0, 80),
        intent: intent.trim(),
        work_type: workType,
        priority,
        executor_kind: executorKind,
        ...(executorKind === 'agent_employee' ? { agent_id: agentId } : {}),
        ...(executorKind === 'temporary_expert' ? { expert_role: expertRole.trim() } : {}),
        ...(executorKind === 'group' ? {
            group_id: groupId,
            group_session_id: groupSessionId,
            group_agent_participant_ids: groupAgentParticipantIds,
        } : {}),
    }), [
        agentId,
        executorKind,
        expertRole,
        groupAgentParticipantIds,
        groupId,
        groupSessionId,
        intent,
        priority,
        title,
        workType,
    ]);
    const taskDraftKey = JSON.stringify(taskDraft);
    const confirmedPreflight = preflight?.draftKey === taskDraftKey ? preflight.result : null;

    const preflightTask = useMutation({
        mutationFn: async ({ draft, draftKey }: { draft: WorkTaskDraft; draftKey: string }) => ({
            draftKey,
            result: await workApi.preflightTask(draft),
        }),
        onSuccess: ({ draftKey, result }) => setPreflight({ draftKey, result }),
        onError: (error: any) => {
            toast.error(isChinese ? '无法生成工作说明' : 'Could not prepare the work statement', {
                details: error?.message || String(error),
            });
        },
    });

    const createTask = useMutation({
        mutationFn: () => {
            if (!confirmedPreflight) throw new Error('The work statement must be reviewed before execution');
            return workApi.createTask({
                ...taskDraft,
                client_request_id: clientRequestId,
                confirmation_fingerprint: confirmedPreflight.confirmation_fingerprint,
            });
        },
        onSuccess: async () => {
            if (draftStorageKey && typeof window !== 'undefined') {
                clearWorkDraft(window.sessionStorage, draftStorageKey);
            }
            setTitle('');
            setIntent('');
            setWorkType('general');
            setExpertRole('');
            setPreflight(null);
            setClientRequestId(createRandomUUID());
            await queryClient.invalidateQueries({ queryKey: ['work-index'] });
            toast.success(isChinese ? '工作说明已确认，任务已进入执行队列' : 'Work statement confirmed; task queued');
        },
        onError: (error: any) => {
            if (String(error?.message || '').includes('work_confirmation_stale')) setPreflight(null);
            toast.error(isChinese ? '任务创建失败' : 'Could not create task', {
                details: error?.message || String(error),
            });
        },
    });

    const canPrepare = intent.trim().length >= 3
        && (executorKind !== 'agent_employee' || !!agentId)
        && (executorKind !== 'temporary_expert' || expertRole.trim().length >= 3)
        && (executorKind !== 'group' || (
            !!groupId
            && !!groupSessionId
            && groupAgentParticipantIds.length > 0
        ))
        && !preflightTask.isPending;
    const canSubmit = !!confirmedPreflight
        && confirmedPreflight.capability_status !== 'unavailable'
        && !createTask.isPending;

    return (
        <div className="work-page">
            <section className="work-hero">
                <div className="work-eyebrow">{isChinese ? '任务工作台' : 'TASK WORKBENCH'}</div>
                <h1>{isChinese ? '今天要完成什么任务？' : 'What needs to get done today?'}</h1>
                <p>{isChinese
                    ? '这里负责登记和执行任务。图片、视频、PPT 等正式产物需在 brief 确认后进入 Agent 对话的交付流；模型、Provider、Skill 和 Tool 由平台治理。'
                    : 'This workbench records and executes tasks. Formal image, video and PPT artifacts enter the delivery workflow from Agent chat after the brief is confirmed; models, providers, skills and tools stay platform-managed.'}</p>
                <div className="work-quick-starts">
                    {QUICK_STARTS.map(({ id, zh, en, icon: Icon, prompt }) => (
                        <button
                            type="button"
                            key={id}
                            className={workType === id ? 'is-active' : ''}
                            onClick={() => {
                                setWorkType(id);
                                if (prompt) setIntent(prompt);
                            }}
                        >
                            <Icon size={17} stroke={1.7} />
                            {isChinese ? zh : en}
                        </button>
                    ))}
                </div>
                <div className="work-composer">
                    <div className="work-composer-mode">
                        {isChinese ? '当前工作类型' : 'Work type'} · {
                            isChinese
                                ? QUICK_STARTS.find((item) => item.id === workType)?.zh
                                : QUICK_STARTS.find((item) => item.id === workType)?.en
                        }
                    </div>
                    <input
                        value={title}
                        onChange={(event) => setTitle(event.target.value)}
                        placeholder={isChinese ? '任务标题（可选）' : 'Task title (optional)'}
                    />
                    <textarea
                        value={intent}
                        onChange={(event) => setIntent(event.target.value)}
                        placeholder={isChinese ? '说清楚目标、受众、约束和期望交付物……' : 'Describe the goal, audience, constraints and expected output…'}
                        rows={5}
                    />
                    <div className="work-composer-controls">
                        <label>
                            <span>{isChinese ? '由谁执行' : 'Executor'}</span>
                            <select value={executorKind} onChange={(event) => setExecutorKind(event.target.value as typeof executorKind)}>
                                <option value="personal_assistant">{isChinese ? '我的助理协调' : 'My assistant coordinates'}</option>
                                <option value="agent_employee">{isChinese ? '指定 Agent 员工' : 'Choose an Agent employee'}</option>
                                <option value="temporary_expert">{isChinese ? '临时专家' : 'Temporary expert'}</option>
                                <option value="group">{isChinese ? 'Group 多人协作' : 'Group collaboration'}</option>
                            </select>
                        </label>
                        {executorKind === 'agent_employee' && (
                            <label>
                                <span>{isChinese ? 'Agent 员工' : 'Agent employee'}</span>
                                <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                                    <option value="">{isChinese ? '请选择' : 'Select'}</option>
                                    {roles.employees.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
                                </select>
                            </label>
                        )}
                        {executorKind === 'temporary_expert' && (
                            <label className="work-expert-role">
                                <span>{isChinese ? '专家角色' : 'Expert role'}</span>
                                <input
                                    value={expertRole}
                                    onChange={(event) => setExpertRole(event.target.value)}
                                    placeholder={isChinese ? '例如：消费品牌广告创意总监' : 'e.g. Consumer brand creative director'}
                                />
                            </label>
                        )}
                        <label>
                            <span>{isChinese ? '优先级' : 'Priority'}</span>
                            <select value={priority} onChange={(event) => setPriority(event.target.value as typeof priority)}>
                                <option value="low">{isChinese ? '低' : 'Low'}</option>
                                <option value="medium">{isChinese ? '普通' : 'Normal'}</option>
                                <option value="high">{isChinese ? '高' : 'High'}</option>
                                <option value="urgent">{isChinese ? '紧急' : 'Urgent'}</option>
                            </select>
                        </label>
                        <button
                            type="button"
                            className="work-submit"
                            disabled={!canPrepare || !!confirmedPreflight}
                            onClick={() => preflightTask.mutate({ draft: taskDraft, draftKey: taskDraftKey })}
                        >
                            {preflightTask.isPending
                                ? '…'
                                : confirmedPreflight
                                    ? (isChinese ? '工作说明已生成' : 'Statement ready')
                                    : (isChinese ? '检查工作说明' : 'Review work statement')}
                            <IconArrowRight size={17} stroke={1.8} />
                        </button>
                    </div>
                    {executorKind === 'group' && (
                        <section className="work-group-config">
                            <div className="work-group-selectors">
                                <label>
                                    <span>{isChinese ? '协作 Group' : 'Group'}</span>
                                    <select
                                        value={groupId}
                                        onChange={(event) => {
                                            setGroupId(event.target.value);
                                            setGroupSessionId('');
                                            setGroupAgentParticipantIds([]);
                                        }}
                                    >
                                        <option value="">{isChinese ? '选择已有 Group' : 'Choose an existing Group'}</option>
                                        {(groupsQuery.data || []).map((group) => (
                                            <option key={group.id} value={group.id}>{group.name}</option>
                                        ))}
                                    </select>
                                </label>
                                <label>
                                    <span>{isChinese ? '协作会话' : 'Session'}</span>
                                    <select
                                        value={groupSessionId}
                                        disabled={!groupId}
                                        onChange={(event) => setGroupSessionId(event.target.value)}
                                    >
                                        <option value="">{isChinese ? '选择工作现场' : 'Choose a workspace'}</option>
                                        {(groupSessionsQuery.data || []).map((session) => (
                                            <option key={session.id} value={session.id}>{session.title}</option>
                                        ))}
                                    </select>
                                </label>
                            </div>
                            <div className="work-group-agents">
                                <div>
                                    <strong>{isChinese ? '参与协作的 Agent' : 'Participating Agents'}</strong>
                                    <span>{isChinese
                                        ? '按选择顺序确定责任：第一个是第一责任人，其余为协作者。'
                                        : 'Selection order sets responsibility: the first is the owner; the rest collaborate.'}</span>
                                </div>
                                <div className="work-group-agent-grid">
                                    {(groupMembersQuery.data || [])
                                        .filter((member) => member.participant_type === 'agent' && !member.is_deleted)
                                        .map((member) => {
                                            const selectedIndex = groupAgentParticipantIds.indexOf(member.participant_id);
                                            return (
                                                <button
                                                    type="button"
                                                    key={member.participant_id}
                                                    className={selectedIndex >= 0 ? 'is-selected' : ''}
                                                    onClick={() => setGroupAgentParticipantIds((current) => (
                                                        current.includes(member.participant_id)
                                                            ? current.filter((id) => id !== member.participant_id)
                                                            : [...current, member.participant_id]
                                                    ))}
                                                >
                                                    <span>{selectedIndex >= 0 ? selectedIndex + 1 : '+'}</span>
                                                    <div>
                                                        <strong>{member.display_name}</strong>
                                                        <small>{member.role_description || (isChinese ? 'Group Agent' : 'Group Agent')}</small>
                                                    </div>
                                                </button>
                                            );
                                        })}
                                </div>
                                {groupId && groupMembersQuery.isSuccess && !groupMembersQuery.data.some(
                                    (member) => member.participant_type === 'agent' && !member.is_deleted,
                                ) && (
                                    <p>{isChinese
                                        ? '这个 Group 还没有可执行的 Agent，请先到 Group 成员管理中添加。'
                                        : 'This Group has no executable Agent members. Add them in Group member management first.'}</p>
                                )}
                            </div>
                        </section>
                    )}
                    {confirmedPreflight && (
                        <section className="work-confirmation" aria-label={isChinese ? '待确认工作说明' : 'Work statement awaiting confirmation'}>
                            <div className="work-confirmation-heading">
                                <div>
                                    <span>{isChinese ? '执行前确认' : 'BEFORE EXECUTION'}</span>
                                    <h2>{isChinese ? '请确认这份工作说明' : 'Confirm this work statement'}</h2>
                                </div>
                                <span className={`work-capability work-capability--${confirmedPreflight.capability_status}`}>
                                    <IconCheck size={15} />
                                    {confirmedPreflight.capability_status === 'available'
                                        ? (isChinese ? '执行者可用' : 'Executor available')
                                        : confirmedPreflight.capability_status}
                                </span>
                            </div>
                            <div className="work-confirmation-objective">
                                <strong>{confirmedPreflight.work_statement.title}</strong>
                                <p>{confirmedPreflight.work_statement.objective}</p>
                            </div>
                            <dl className="work-confirmation-grid">
                                <div>
                                    <dt>{isChinese ? '交付边界' : 'Output boundary'}</dt>
                                    <dd>{
                                        EXPECTED_OUTPUT_LABELS[confirmedPreflight.work_statement.expected_output]?.[
                                            isChinese ? 'zh' : 'en'
                                        ] || confirmedPreflight.work_statement.expected_output
                                    }</dd>
                                </div>
                                <div>
                                    <dt>{isChinese ? '责任人' : 'Owner'}</dt>
                                    <dd>{confirmedPreflight.work_statement.executor.expert_role
                                        || confirmedPreflight.work_statement.executor.group_name
                                        || confirmedPreflight.work_statement.executor.agent_name}</dd>
                                </div>
                                <div>
                                    <dt>{isChinese ? '费用说明' : 'Cost'}</dt>
                                    <dd>{confirmedPreflight.estimated_credits == null
                                        ? (isChinese
                                            ? '按实际任务用量结算；正式媒体生成将再次预检'
                                            : 'Usage based; formal media is preflighted separately')
                                        : `${confirmedPreflight.estimated_credits} Credits`}</dd>
                                </div>
                                <div>
                                    <dt>{isChinese ? '审批' : 'Approval'}</dt>
                                    <dd>{confirmedPreflight.approval_required
                                        ? (isChinese ? '启动前需要审批' : 'Required before launch')
                                        : (isChinese ? '可启动；高风险动作仍单独审批' : 'Launchable; risky actions remain gated')}</dd>
                                </div>
                            </dl>
                            {confirmedPreflight.work_statement.cost.formal_media_requires_separate_preflight && (
                                <p className="work-confirmation-note">
                                    {isChinese
                                        ? '当前确认的是 Brief/任务执行，不代表图片、视频或 PPT 已经生成。正式交付会绑定本任务，并继续经过能力、Credits、质量检查和业务批准。'
                                        : 'This confirms brief/task execution, not a finished creative artifact. Formal delivery will link back to this task and run its own capability, Credits, quality and approval gates.'}
                                </p>
                            )}
                            {confirmedPreflight.capability_status === 'unavailable' && (
                                <p className="work-confirmation-note work-confirmation-note--blocked">
                                    {isChinese
                                        ? '当前执行者没有可用的文字执行线路，任务尚未创建，也不会扣除 Credits。请联系公司管理员检查套餐或平台路由。'
                                        : 'The executor has no available text route. No task was created and no Credits were charged. Ask a company administrator to check the plan or route.'}
                                </p>
                            )}
                            <div className="work-confirmation-actions">
                                <button type="button" onClick={() => setPreflight(null)} disabled={createTask.isPending}>
                                    {isChinese ? '返回修改' : 'Edit'}
                                </button>
                                <button
                                    type="button"
                                    className="work-confirm"
                                    disabled={!canSubmit}
                                    onClick={() => createTask.mutate()}
                                >
                                    {createTask.isPending ? '…' : (isChinese ? '确认并开始执行' : 'Confirm and start')}
                                    <IconArrowRight size={17} stroke={1.8} />
                                </button>
                            </div>
                        </section>
                    )}
                </div>
            </section>

            <section className="work-list-section">
                <div className="work-list-header">
                    <div>
                        <h2>{isChinese ? '我的工作' : 'My work'}</h2>
                        <p>{isChinese ? '任务、执行、产物、检查、批准和交付是不同状态。' : 'Tasks, runs, artifacts, reviews, approvals and delivery remain distinct.'}</p>
                    </div>
                    <button type="button" onClick={() => workQuery.refetch()} disabled={workQuery.isFetching}>
                        <IconRefresh size={16} className={workQuery.isFetching ? 'is-spinning' : ''} />
                        {isChinese ? '刷新' : 'Refresh'}
                    </button>
                </div>
                {workQuery.isError && (
                    <div className="work-empty work-empty--error">
                        {isChinese ? '工作索引加载失败，请重试。' : 'Could not load the work index. Please retry.'}
                    </div>
                )}
                {!workQuery.isLoading && !workQuery.isError && (workQuery.data?.items.length || 0) === 0 && (
                    <div className="work-empty">
                        <IconCheck size={24} />
                        <strong>{isChinese ? '还没有工作记录' : 'No work yet'}</strong>
                        <span>{isChinese ? '从上方描述第一项业务结果。' : 'Describe your first outcome above.'}</span>
                    </div>
                )}
                <div className="work-list">
                    {(workQuery.data?.items || []).map((item) => (
                        <WorkCard key={`${item.kind}:${item.id}`} item={item} isChinese={isChinese} />
                    ))}
                </div>
                {roles.personalAssistant && (
                    <button
                        type="button"
                        className="work-assistant-link"
                        onClick={() => navigate(`/agents/${roles.personalAssistant!.id}/chat`)}
                    >
                        <IconUsers size={17} />
                        {isChinese ? `打开我的助理：${roles.personalAssistant.name}` : `Open my assistant: ${roles.personalAssistant.name}`}
                    </button>
                )}
            </section>
        </div>
    );
}
