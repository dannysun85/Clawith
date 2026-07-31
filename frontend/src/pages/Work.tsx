import { useMemo, useState } from 'react';
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

import { agentApi, workApi, type WorkItem } from '../services/api';
import { useAuthStore } from '../stores';
import { createRandomUUID } from '../utils/randomUUID';
import { partitionAgentRoles } from '../utils/productRoles';
import { useToast } from '../components/Toast/ToastProvider';
import './Work.css';


const QUICK_STARTS = [
    { id: 'general', zh: '通用任务', en: 'General task', icon: IconSparkles, prompt: '' },
    { id: 'image', zh: '图片 Brief', en: 'Image brief', icon: IconPhoto, prompt: '请整理一份商业图片制作 brief：确认用途、受众、尺寸、品牌约束和交付格式。本任务只整理可确认的 brief，不调用图片生成、不声称已经交付正式产物；brief 确认后应进入 Agent 对话的正式交付流程。' },
    { id: 'video', zh: '视频 Brief', en: 'Video brief', icon: IconVideo, prompt: '请整理一份带人物商业视频的制作 brief：确认受众、平台、时长、人物、脚本、镜头、声音和交付格式。本任务只整理 brief，不调用视频生成、不声称已经交付正式产物；brief 确认后应进入 Agent 对话的正式交付流程。' },
    { id: 'presentation', zh: 'PPT Brief', en: 'PPT brief', icon: IconPresentation, prompt: '请整理一份正式汇报 PPT 的制作 brief：确认受众、场景、页数、品牌、内容约束、版式与配图要求。本任务只整理 brief，不调用 PPT 生成、不声称已经交付 PPTX；brief 确认后应进入 Agent 对话的正式交付流程。' },
    { id: 'document', zh: '报告 Brief', en: 'Report brief', icon: IconFileDescription, prompt: '请整理一份正式报告的制作 brief：确认用途、读者、篇幅、格式、证据来源和审批要求。本任务只整理 brief，不声称已经交付正式文档。' },
];

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
    return (
        <article className="work-card">
            <button type="button" className="work-card-main" onClick={() => navigate(item.deep_link)}>
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
                        : `${item.agent_name}`}</span>
                    <span>{new Date(item.updated_at).toLocaleString()}</span>
                </div>
            </button>
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
            {(item.task_id || item.deliverable_id) && item.user_stage === 'delivery' && (
                <div className="work-card-actions">
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
                </div>
            )}
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
    const [title, setTitle] = useState('');
    const [intent, setIntent] = useState('');
    const [priority, setPriority] = useState<'low' | 'medium' | 'high' | 'urgent'>('medium');
    const [executorKind, setExecutorKind] = useState<'personal_assistant' | 'agent_employee' | 'temporary_expert'>('personal_assistant');
    const [agentId, setAgentId] = useState('');
    const [expertRole, setExpertRole] = useState('');

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
    const roles = useMemo(
        () => partitionAgentRoles(
            agentsQuery.data || [],
            workQuery.data?.personal_assistant_agent_id,
        ),
        [agentsQuery.data, workQuery.data?.personal_assistant_agent_id],
    );

    const createTask = useMutation({
        mutationFn: () => workApi.createTask({
            client_request_id: createRandomUUID(),
            title: title.trim() || intent.trim().split(/\n/)[0].slice(0, 80),
            intent: intent.trim(),
            priority,
            executor_kind: executorKind,
            ...(executorKind === 'agent_employee' ? { agent_id: agentId } : {}),
            ...(executorKind === 'temporary_expert' ? { expert_role: expertRole.trim() } : {}),
        }),
        onSuccess: async () => {
            setTitle('');
            setIntent('');
            setExpertRole('');
            await queryClient.invalidateQueries({ queryKey: ['work-index'] });
            toast.success(isChinese ? '任务已进入执行队列' : 'Task entered the execution queue');
        },
        onError: (error: any) => {
            toast.error(isChinese ? '任务创建失败' : 'Could not create task', {
                details: error?.message || String(error),
            });
        },
    });

    const canSubmit = intent.trim().length >= 3
        && (executorKind !== 'agent_employee' || !!agentId)
        && (executorKind !== 'temporary_expert' || expertRole.trim().length >= 3)
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
                            onClick={() => {
                                if (prompt) setIntent(prompt);
                            }}
                        >
                            <Icon size={17} stroke={1.7} />
                            {isChinese ? zh : en}
                        </button>
                    ))}
                </div>
                <div className="work-composer">
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
                            disabled={!canSubmit}
                            onClick={() => createTask.mutate()}
                        >
                            {createTask.isPending ? '…' : (isChinese ? '开始任务' : 'Start task')}
                            <IconArrowRight size={17} stroke={1.8} />
                        </button>
                    </div>
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
