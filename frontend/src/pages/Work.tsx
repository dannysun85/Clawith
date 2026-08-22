import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import {
    IconArrowRight,
    IconAlertCircle,
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
    type WorkExecutorKind,
    type WorkItem,
    type WorkNextAction,
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

const DEFAULT_ACCEPTANCE_CRITERIA = {
    zh: '结果直接回应已确认目标，并能用于下一步业务决策或执行。',
    en: 'The result directly addresses the confirmed objective and is usable for the next business action.',
};

const contractLines = (value: string) => value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

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

function inboxActionLabel(action: WorkNextAction, isChinese: boolean) {
    const labels: Record<WorkNextAction['kind'], { zh: string; en: string }> = {
        quality_review: { zh: '提交质量检查', en: 'Submit quality review' },
        task_result_review: { zh: '验收任务结果', en: 'Review task result' },
        runtime_approval: { zh: '处理运行期审批', en: 'Review Runtime approval' },
        delivery_approval: { zh: '批准交付或要求修改', en: 'Approve or request changes' },
        tool_reconciliation: { zh: '核对工具执行结果', en: 'Reconcile tool outcome' },
        task_recovery: { zh: '恢复失败任务', en: 'Recover failed task' },
        delivery_recovery: { zh: '处理交付阻塞', en: 'Resolve delivery issue' },
    };
    return isChinese ? labels[action.kind].zh : labels[action.kind].en;
}

function executorReasonLabel(
    proposal: WorkTaskPreflight['executor_proposal'],
    isChinese: boolean,
) {
    if (proposal.reason_codes.includes('manual_override')) {
        return isChinese ? '按你的高级设置指定' : 'Selected in advanced settings';
    }
    if (proposal.reason_codes.includes('low_confidence_personal_assistant_fallback')) {
        return isChinese
            ? '任务没有足够明确的专业角色匹配，由个人助理负责协调'
            : 'No specialist match was strong enough, so your assistant will coordinate';
    }
    return isChinese
        ? '根据任务目标、角色职责和当前可执行状态匹配'
        : 'Matched from the task goal, role responsibilities, and current readiness';
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
                {item.task_id && (
                    <button type="button" className="is-primary" onClick={() => navigate(`/work/${item.task_id}`)}>
                        {isChinese ? '查看任务详情' : 'View task detail'}
                    </button>
                )}
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
    const [routingMode, setRoutingMode] = useState<'auto' | 'manual'>(restoredDraft?.routingMode || 'auto');
    const [advancedExecutor, setAdvancedExecutor] = useState(restoredDraft?.routingMode === 'manual');
    const [executorKind, setExecutorKind] = useState<WorkExecutorKind>(restoredDraft?.executorKind || 'personal_assistant');
    const [agentId, setAgentId] = useState(restoredDraft?.agentId || '');
    const [expertRole, setExpertRole] = useState(restoredDraft?.expertRole || '');
    const [groupId, setGroupId] = useState(restoredDraft?.groupId || '');
    const [groupSessionId, setGroupSessionId] = useState(restoredDraft?.groupSessionId || '');
    const [groupAgentParticipantIds, setGroupAgentParticipantIds] = useState<string[]>(restoredDraft?.groupAgentParticipantIds || []);
    const [acceptanceCriteria, setAcceptanceCriteria] = useState(
        restoredDraft?.acceptanceCriteria || (isChinese ? DEFAULT_ACCEPTANCE_CRITERIA.zh : DEFAULT_ACCEPTANCE_CRITERIA.en),
    );
    const [requiredSections, setRequiredSections] = useState(restoredDraft?.requiredSections || '');
    const [forbiddenTerms, setForbiddenTerms] = useState(restoredDraft?.forbiddenTerms || '');
    const [minimumLength, setMinimumLength] = useState(restoredDraft?.minimumLength || '');
    const [maximumLength, setMaximumLength] = useState(restoredDraft?.maximumLength || '');
    const [lengthUnit, setLengthUnit] = useState<'characters' | 'cjk_characters' | 'words'>(
        restoredDraft?.lengthUnit || (isChinese ? 'cjk_characters' : 'words'),
    );
    const [evidenceRequired, setEvidenceRequired] = useState(restoredDraft?.evidenceRequired || false);
    const [clientRequestId, setClientRequestId] = useState(restoredDraft?.clientRequestId || createRandomUUID());
    const [preflight, setPreflight] = useState<{
        draftKey: string;
        result: WorkTaskPreflight;
    } | null>(null);
    const [workView, setWorkView] = useState<'attention' | 'active' | 'completed'>('attention');
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
        setRoutingMode(next?.routingMode || 'auto');
        setAdvancedExecutor(next?.routingMode === 'manual');
        setExecutorKind(next?.executorKind || 'personal_assistant');
        setAgentId(next?.agentId || '');
        setExpertRole(next?.expertRole || '');
        setGroupId(next?.groupId || '');
        setGroupSessionId(next?.groupSessionId || '');
        setGroupAgentParticipantIds(next?.groupAgentParticipantIds || []);
        setAcceptanceCriteria(next?.acceptanceCriteria || (isChinese ? DEFAULT_ACCEPTANCE_CRITERIA.zh : DEFAULT_ACCEPTANCE_CRITERIA.en));
        setRequiredSections(next?.requiredSections || '');
        setForbiddenTerms(next?.forbiddenTerms || '');
        setMinimumLength(next?.minimumLength || '');
        setMaximumLength(next?.maximumLength || '');
        setLengthUnit(next?.lengthUnit || (isChinese ? 'cjk_characters' : 'words'));
        setEvidenceRequired(next?.evidenceRequired || false);
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
            version: 2,
            title,
            intent,
            workType,
            priority,
            routingMode,
            executorKind,
            agentId,
            expertRole,
            groupId,
            groupSessionId,
            groupAgentParticipantIds,
            acceptanceCriteria,
            requiredSections,
            forbiddenTerms,
            minimumLength,
            maximumLength,
            lengthUnit,
            evidenceRequired,
            clientRequestId,
        });
    }, [
        agentId,
        acceptanceCriteria,
        clientRequestId,
        draftStorageKey,
        executorKind,
        expertRole,
        evidenceRequired,
        forbiddenTerms,
        groupAgentParticipantIds,
        groupId,
        groupSessionId,
        intent,
        lengthUnit,
        maximumLength,
        minimumLength,
        priority,
        routingMode,
        requiredSections,
        title,
        workType,
    ]);

    const workQuery = useQuery({
        queryKey: ['work-index', user?.id, user?.tenant_id],
        queryFn: () => workApi.list(20),
        enabled: !!user?.id && !!user?.tenant_id,
        refetchInterval: 15_000,
    });
    const inboxQuery = useQuery({
        queryKey: ['work-inbox', user?.id, user?.tenant_id],
        queryFn: () => workApi.getInbox({ limit: 50 }),
        enabled: !!user?.id && !!user?.tenant_id,
        refetchInterval: 15_000,
    });
    const inboxCountQuery = useQuery({
        queryKey: ['work-inbox-count', user?.id, user?.tenant_id],
        queryFn: () => workApi.getInboxCount(),
        enabled: !!user?.id && !!user?.tenant_id,
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
        enabled: !!user?.tenant_id && routingMode === 'manual' && executorKind === 'group',
    });
    const groupSessionsQuery = useQuery({
        queryKey: ['work-group-sessions', groupId],
        queryFn: () => groupApi.sessions(groupId),
        enabled: routingMode === 'manual' && executorKind === 'group' && !!groupId,
    });
    const groupMembersQuery = useQuery({
        queryKey: ['work-group-members', groupId],
        queryFn: () => groupApi.members(groupId),
        enabled: routingMode === 'manual' && executorKind === 'group' && !!groupId,
    });
    const roles = useMemo(
        () => partitionAgentRoles(
            agentsQuery.data || [],
            workQuery.data?.personal_assistant_agent_id,
        ),
        [agentsQuery.data, workQuery.data?.personal_assistant_agent_id],
    );
    const workBuckets = useMemo(() => {
        const items = workQuery.data?.items || [];
        const completedStages = new Set(['completed', 'delivery', 'cancelled']);
        return {
            active: items.filter((item) => !completedStages.has(item.user_stage)),
            completed: items.filter((item) => completedStages.has(item.user_stage)),
        };
    }, [workQuery.data?.items]);

    const taskDraft = useMemo<WorkTaskDraft>(() => ({
        title: title.trim() || intent.trim().split(/\n/)[0].slice(0, 80),
        intent: intent.trim(),
        work_type: workType,
        priority,
        routing_mode: routingMode,
        acceptance_contract: {
            version: 1,
            criteria: contractLines(acceptanceCriteria),
            required_sections: contractLines(requiredSections),
            forbidden_terms: contractLines(forbiddenTerms),
            result_language: 'auto',
            ...(minimumLength || maximumLength ? {
                length: {
                    unit: lengthUnit,
                    ...(minimumLength ? { minimum: Number(minimumLength) } : {}),
                    ...(maximumLength ? { maximum: Number(maximumLength) } : {}),
                },
            } : {}),
            evidence_required: evidenceRequired,
            owner_review_required: true,
        },
        ...(routingMode === 'manual' ? {
            executor_kind: executorKind,
            ...(executorKind === 'agent_employee' ? { agent_id: agentId } : {}),
            ...(executorKind === 'temporary_expert' ? { expert_role: expertRole.trim() } : {}),
            ...(executorKind === 'group' ? {
                group_id: groupId,
                group_session_id: groupSessionId,
                group_agent_participant_ids: groupAgentParticipantIds,
            } : {}),
        } : {}),
    }), [
        agentId,
        acceptanceCriteria,
        executorKind,
        expertRole,
        evidenceRequired,
        forbiddenTerms,
        groupAgentParticipantIds,
        groupId,
        groupSessionId,
        intent,
        lengthUnit,
        maximumLength,
        minimumLength,
        priority,
        routingMode,
        requiredSections,
        title,
        workType,
    ]);
    const taskDraftKey = JSON.stringify(taskDraft);
    const confirmedPreflight = preflight?.draftKey === taskDraftKey ? preflight.result : null;
    const executorProposal = confirmedPreflight?.executor_proposal || null;

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
            setRoutingMode('auto');
            setAdvancedExecutor(false);
            setExpertRole('');
            setAcceptanceCriteria(isChinese ? DEFAULT_ACCEPTANCE_CRITERIA.zh : DEFAULT_ACCEPTANCE_CRITERIA.en);
            setRequiredSections('');
            setForbiddenTerms('');
            setMinimumLength('');
            setMaximumLength('');
            setEvidenceRequired(false);
            setPreflight(null);
            setClientRequestId(createRandomUUID());
            await Promise.all([
                queryClient.invalidateQueries({ queryKey: ['work-index'] }),
                queryClient.invalidateQueries({ queryKey: ['work-inbox'] }),
                queryClient.invalidateQueries({ queryKey: ['work-inbox-count'] }),
            ]);
            toast.success(isChinese ? '工作说明已确认，任务已进入执行队列' : 'Work statement confirmed; task queued');
        },
        onError: (error: any) => {
            if (['work_confirmation_stale', 'work_capability_changed'].includes(String(error?.code || ''))) {
                setPreflight(null);
            }
            toast.error(isChinese ? '任务创建失败' : 'Could not create task', {
                details: error?.message || String(error),
            });
        },
    });

    const canPrepare = intent.trim().length >= 3
        && contractLines(acceptanceCriteria).length > 0
        && (!minimumLength || Number.isInteger(Number(minimumLength)))
        && (!maximumLength || Number.isInteger(Number(maximumLength)))
        && (!minimumLength || !maximumLength || Number(minimumLength) <= Number(maximumLength))
        && (routingMode === 'auto' || executorKind !== 'agent_employee' || !!agentId)
        && (routingMode === 'auto' || executorKind !== 'temporary_expert' || expertRole.trim().length >= 3)
        && (routingMode === 'auto' || executorKind !== 'group' || (
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
                    <section className="work-acceptance-contract">
                        <div className="work-acceptance-heading">
                            <div>
                                <strong>{isChinese ? '业务验收标准' : 'Business acceptance'}</strong>
                                <span>{isChinese
                                    ? '这些条件会进入工作说明；可机器检查的边界会阻止伪完成，执行结果仍需你最终验收。'
                                    : 'These criteria become part of the work statement. Deterministic boundaries block false completion, and you still approve the business result.'}</span>
                            </div>
                            <span>{isChinese ? '必填' : 'Required'}</span>
                        </div>
                        <label>
                            <span>{isChinese ? '验收条件（每行一条）' : 'Acceptance criteria (one per line)'}</span>
                            <textarea
                                value={acceptanceCriteria}
                                onChange={(event) => setAcceptanceCriteria(event.target.value)}
                                rows={3}
                                maxLength={3600}
                            />
                        </label>
                        <div className="work-acceptance-grid">
                            <label>
                                <span>{isChinese ? '必须包含的章节（每行一项）' : 'Required sections (one per line)'}</span>
                                <textarea
                                    value={requiredSections}
                                    onChange={(event) => setRequiredSections(event.target.value)}
                                    rows={2}
                                    placeholder={isChinese ? '例如：结论\n30 天行动计划' : 'e.g. Conclusion\n30-day action plan'}
                                />
                            </label>
                            <label>
                                <span>{isChinese ? '不得出现的内容（每行一项）' : 'Forbidden content (one per line)'}</span>
                                <textarea
                                    value={forbiddenTerms}
                                    onChange={(event) => setForbiddenTerms(event.target.value)}
                                    rows={2}
                                    placeholder={isChinese ? '例如：内部工具名称\n未经证实的数据' : 'e.g. Internal tool names\nUnverified claims'}
                                />
                            </label>
                        </div>
                        <div className="work-acceptance-length">
                            <label>
                                <span>{isChinese ? '计数方式' : 'Length unit'}</span>
                                <select value={lengthUnit} onChange={(event) => setLengthUnit(event.target.value as typeof lengthUnit)}>
                                    <option value="cjk_characters">{isChinese ? '中文字符' : 'CJK characters'}</option>
                                    <option value="words">{isChinese ? '英文单词' : 'Words'}</option>
                                    <option value="characters">{isChinese ? '全部字符' : 'Characters'}</option>
                                </select>
                            </label>
                            <label>
                                <span>{isChinese ? '最少' : 'Minimum'}</span>
                                <input type="number" min="1" value={minimumLength} onChange={(event) => setMinimumLength(event.target.value)} placeholder={isChinese ? '可选' : 'Optional'} />
                            </label>
                            <label>
                                <span>{isChinese ? '最多' : 'Maximum'}</span>
                                <input type="number" min="1" value={maximumLength} onChange={(event) => setMaximumLength(event.target.value)} placeholder={isChinese ? '可选' : 'Optional'} />
                            </label>
                            <label className="work-acceptance-evidence">
                                <input type="checkbox" checked={evidenceRequired} onChange={(event) => setEvidenceRequired(event.target.checked)} />
                                <span>{isChinese ? '必须提供可验证证据' : 'Require verifiable evidence'}</span>
                            </label>
                        </div>
                        <small>{isChinese
                            ? '超长报告不要放在任务消息里：先选择“报告 Brief”，确认后进入正式交付流，生成可下载、可质检、可批准的产物。'
                            : 'Do not squeeze long reports into a task message. Confirm a Report brief, then use the formal delivery workflow for a downloadable, reviewed, and approved artifact.'}</small>
                    </section>
                    <div className="work-composer-controls">
                        <div className="work-routing-mode">
                            <span>{isChinese ? '执行方式' : 'Routing'}</span>
                            <strong>{routingMode === 'auto'
                                ? (isChinese ? '系统自动匹配执行者' : 'System selects the executor')
                                : (isChinese ? '按你的指定执行' : 'Use my manual selection')}</strong>
                        </div>
                        <button
                            type="button"
                            className="work-advanced-executor"
                            onClick={() => {
                                const next = !advancedExecutor;
                                setAdvancedExecutor(next);
                                setRoutingMode(next ? 'manual' : 'auto');
                                setPreflight(null);
                            }}
                        >
                            {advancedExecutor
                                ? (isChinese ? '恢复自动匹配' : 'Use automatic routing')
                                : (isChinese ? '高级：指定执行者' : 'Advanced: choose executor')}
                        </button>
                        {advancedExecutor && (
                            <label>
                                <span>{isChinese ? '由谁执行' : 'Executor'}</span>
                                <select value={executorKind} onChange={(event) => setExecutorKind(event.target.value as WorkExecutorKind)}>
                                    <option value="personal_assistant">{isChinese ? '我的助理协调' : 'My assistant coordinates'}</option>
                                    <option value="agent_employee">{isChinese ? '指定 Agent 员工' : 'Choose an Agent employee'}</option>
                                    <option value="temporary_expert">{isChinese ? '临时专家（助理运行期角色）' : 'Temporary expert (assistant run role)'}</option>
                                    <option value="group">{isChinese ? 'Group 多人协作' : 'Group collaboration'}</option>
                                </select>
                            </label>
                        )}
                        {advancedExecutor && executorKind === 'agent_employee' && (
                            <label>
                                <span>{isChinese ? 'Agent 员工' : 'Agent employee'}</span>
                                <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                                    <option value="">{isChinese ? '请选择' : 'Select'}</option>
                                    {roles.employees.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
                                </select>
                            </label>
                        )}
                        {advancedExecutor && executorKind === 'temporary_expert' && (
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
                    {advancedExecutor && executorKind === 'group' && (
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
                            {executorProposal && (
                                <div className="work-executor-proposal">
                                    <strong>{routingMode === 'auto'
                                        ? (isChinese
                                            ? `系统已选择：${executorProposal.agent_name}`
                                            : `System selected: ${executorProposal.agent_name}`)
                                        : (isChinese
                                            ? `你已指定：${executorProposal.agent_name}`
                                            : `You selected: ${executorProposal.agent_name}`)}</strong>
                                    <span>{executorReasonLabel(executorProposal, isChinese)}</span>
                                    <small>{isChinese ? '路由置信度' : 'Routing confidence'} · {Math.round(executorProposal.confidence * 100)}%</small>
                                </div>
                            )}
                            <div className="work-confirmation-objective">
                                <strong>{confirmedPreflight.work_statement.title}</strong>
                                <p>{confirmedPreflight.work_statement.objective}</p>
                            </div>
                            <div className="work-confirmation-acceptance">
                                <strong>{isChinese ? '你将按以下标准验收' : 'You will review against'}</strong>
                                <ul>
                                    {confirmedPreflight.work_statement.acceptance_contract.criteria.map((criterion) => (
                                        <li key={criterion}>{criterion}</li>
                                    ))}
                                </ul>
                                {confirmedPreflight.work_statement.acceptance_contract.length && (
                                    <small>
                                        {isChinese ? '结果长度' : 'Result length'} · {
                                            confirmedPreflight.work_statement.acceptance_contract.length.minimum || '—'
                                        }–{
                                            confirmedPreflight.work_statement.acceptance_contract.length.maximum || '—'
                                        } {confirmedPreflight.work_statement.acceptance_contract.length.unit}
                                    </small>
                                )}
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
                                        ? '当前确认的是 Brief/任务执行，不代表图片、视频或 PPT 已经生成。正式交付会绑定本任务，并继续经过能力、Credits、质量检查和正式交付批准。'
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
                    <button
                        type="button"
                        onClick={() => void Promise.all([
                            workQuery.refetch(),
                            inboxQuery.refetch(),
                            inboxCountQuery.refetch(),
                        ])}
                        disabled={workQuery.isFetching || inboxQuery.isFetching || inboxCountQuery.isFetching}
                    >
                        <IconRefresh
                            size={16}
                            className={workQuery.isFetching || inboxQuery.isFetching || inboxCountQuery.isFetching ? 'is-spinning' : ''}
                        />
                        {isChinese ? '刷新' : 'Refresh'}
                    </button>
                </div>
                <div className="work-view-tabs" role="tablist" aria-label={isChinese ? '工作视图' : 'Work views'}>
                    <button
                        type="button"
                        role="tab"
                        aria-selected={workView === 'attention'}
                        className={workView === 'attention' ? 'is-active' : ''}
                        onClick={() => setWorkView('attention')}
                    >
                        <IconAlertCircle size={15} />
                        {isChinese ? '待我处理' : 'Needs my attention'}
                        <span>{inboxCountQuery.data?.count || 0}</span>
                    </button>
                    <button
                        type="button"
                        role="tab"
                        aria-selected={workView === 'active'}
                        className={workView === 'active' ? 'is-active' : ''}
                        onClick={() => setWorkView('active')}
                    >
                        {isChinese ? '进行中' : 'In progress'}
                        <span>{workBuckets.active.length}</span>
                    </button>
                    <button
                        type="button"
                        role="tab"
                        aria-selected={workView === 'completed'}
                        className={workView === 'completed' ? 'is-active' : ''}
                        onClick={() => setWorkView('completed')}
                    >
                        {isChinese ? '最近完成' : 'Recently completed'}
                        <span>{workBuckets.completed.length}</span>
                    </button>
                </div>
                {(workQuery.isError || (workView === 'attention' && inboxQuery.isError)) && (
                    <div className="work-empty work-empty--error">
                        {isChinese ? '工作事实加载失败，请重试。' : 'Could not load the work facts. Please retry.'}
                    </div>
                )}
                {workView === 'attention' && !inboxQuery.isLoading && !inboxQuery.isError && (inboxQuery.data?.items.length || 0) === 0 && (
                    <div className="work-empty">
                        <IconCheck size={24} />
                        <strong>{isChinese ? '当前没有待你处理的动作' : 'Nothing needs your attention'}</strong>
                        <span>{isChinese ? '审批、质量检查或失败恢复出现时，会从权威事实进入这里。' : 'Approvals, quality reviews and recoveries appear here from authoritative facts.'}</span>
                    </div>
                )}
                {workView === 'attention' && (
                    <div className="work-inbox-list">
                        {(inboxQuery.data?.items || []).map((action) => (
                            <article className="work-inbox-card" key={action.id}>
                                <div>
                                    <span>{inboxActionLabel(action, isChinese)}</span>
                                    <h3>{action.title}</h3>
                                    <p>{action.reason_code}</p>
                                    <small>{new Date(action.created_at).toLocaleString()}</small>
                                </div>
                                <button
                                    type="button"
                                    onClick={() => navigate(
                                        action.kind === 'task_recovery' && action.task_id
                                            ? `/work/${action.task_id}`
                                            : action.action_url,
                                    )}
                                >
                                    {isChinese ? '去处理' : 'Open action'}
                                    <IconArrowRight size={16} />
                                </button>
                            </article>
                        ))}
                    </div>
                )}
                {workView !== 'attention' && !workQuery.isLoading && !workQuery.isError && workBuckets[workView].length === 0 && (
                    <div className="work-empty">
                        <IconCheck size={24} />
                        <strong>{workView === 'active'
                            ? (isChinese ? '当前没有进行中的工作' : 'No work is in progress')
                            : (isChinese ? '还没有最近完成的工作' : 'No recently completed work')}</strong>
                        <span>{isChinese ? '从上方描述一项业务结果。' : 'Describe a business outcome above.'}</span>
                    </div>
                )}
                {workView !== 'attention' && (
                    <div className="work-list">
                        {workBuckets[workView].map((item) => (
                            <WorkCard key={`${item.kind}:${item.id}`} item={item} isChinese={isChinese} />
                        ))}
                    </div>
                )}
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
