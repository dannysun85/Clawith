import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
    IconAlertCircle,
    IconCheck,
    IconChevronRight,
    IconClock,
    IconDownload,
    IconFileTypePpt,
    IconLoader2,
    IconPhoto,
    IconSparkles,
    IconVideo,
    IconX,
} from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { useToast } from '../Toast/ToastProvider';
import {
    deliverableApi,
    type DeliverableBrief,
    type DeliverableExecution,
    type DeliverableExecutionUnit,
    type DeliverablePreflight,
    type DeliverableQualityReview,
    type DeliverableQualityReviewer,
    type DeliverableRequest,
    type DeliverableWorkflow,
    type DeliverableWorkType,
} from '../../services/api';
import {
    deliverableApprovalBlocked,
    deliverableApprovalStatusMessage,
    deliverableNextAction,
} from '../../utils/deliverables';
import type { SaasTier } from '../TierSelector';


export interface DeliverableAttachmentInput {
    name: string;
    path?: string;
}

interface DeliverableLauncherProps {
    agentId: string;
    sessionId?: string;
    taskId?: string;
    initialWorkType?: DeliverableWorkType;
    initialGoal?: string;
    initialSpecOverrides?: Record<string, string | number>;
    autoOpenKey?: string;
    tier: SaasTier;
    attachments: DeliverableAttachmentInput[];
    disabled?: boolean;
    onCreated: (request: DeliverableRequest, launchable: boolean) => void;
}

interface DeliverableRequestCardProps {
    request: DeliverableRequest;
    launchable: boolean;
    onRemove: () => void;
    onOpen: () => void;
    onUpdated?: (request: DeliverableRequest) => void;
}

interface DeliverableReviewCardProps {
    request: DeliverableRequest;
    onUpdated: (request: DeliverableRequest) => void;
}

const WORK_TYPE_ICONS: Record<string, React.ReactNode> = {
    presentation: <IconFileTypePpt size={20} stroke={1.75} />,
    poster: <IconPhoto size={20} stroke={1.75} />,
    video: <IconVideo size={20} stroke={1.75} />,
};

function workflowLabel(workflow: DeliverableWorkflow, isZh: boolean) {
    return isZh ? workflow.label_zh : workflow.label_en;
}

function preflightReasonLabel(reason: string, isZh: boolean) {
    const labels: Record<string, [string, string]> = {
        plan_denied: ['当前 SaaS 档位未授权', 'The current SaaS tier is not entitled'],
        agent_tool_disabled: ['当前数字员工未启用所需工具', 'The required Agent tool is disabled'],
        pool_unavailable: ['当前平台账号池没有可用线路', 'No provider route is available in the platform pool'],
        text_route_unavailable: ['当前没有可用的文本模型线路', 'No text-model route is available'],
        presentation_tool_unavailable: ['当前数字员工没有可用的 PPT 工具', 'The Agent has no presentation tool'],
        media_capability_unavailable: ['当前没有可用的媒体生成能力', 'No media-generation capability is available'],
        degraded_route_requires_confirmation: ['当前只有应急质量线路，需要明确确认质量降级', 'Only an emergency-quality route is available and requires explicit confirmation'],
        video_post_production_tool_unavailable: ['当前数字员工缺少视频后期工具', 'The Agent lacks a video post-production tool'],
        workflow_execution_not_enabled: ['该工作流尚未开放执行', 'Workflow execution is not enabled'],
        deliverable_poster_v2_not_allowlisted: ['多候选图片流程尚未对该账号开放', 'The multi-candidate image pipeline is not enabled for this account'],
        deliverable_video_v2_not_allowlisted: ['分镜视频流程尚未对该账号开放', 'The storyboard-gated video pipeline is not enabled for this account'],
        deliverable_presentation_v2_not_allowlisted: ['大纲审批 PPT 流程尚未对该账号开放', 'The outline-gated presentation pipeline is not enabled for this account'],
        deliverable_stage_approvals_disabled: ['阶段审批总闸尚未开启，V2 不可进入制作', 'Staged approvals are disabled, so V2 cannot enter production'],
        audio_mode_route_mismatch: ['镜头内同步对白需要具备原生音轨能力的线路，请改用旁白或静音模式', 'In-scene dialogue needs a native-audio route; switch to voiceover or silent'],
    };
    if (reason.startsWith('brief_missing:')) {
        const field = reason.slice('brief_missing:'.length);
        const fieldLabel = BRIEF_FIELD_LABELS[field];
        const name = fieldLabel ? fieldLabel[isZh ? 0 : 1] : field;
        return isZh ? `工作说明缺少要素：${name}` : `The brief is missing: ${name}`;
    }
    const label = labels[reason];
    return label ? label[isZh ? 0 : 1] : reason;
}

const BRIEF_FIELD_LABELS: Record<string, [string, string]> = {
    purpose: ['用途', 'Purpose'],
    channel: ['使用渠道', 'Channel'],
    audience: ['目标受众', 'Audience'],
    aspect_ratio: ['画面比例', 'Aspect ratio'],
    style: ['视觉风格', 'Visual style'],
    exact_copy_blocks: ['精确文案', 'Exact copy'],
    brand_assets: ['品牌资产', 'Brand assets'],
    reference_assets: ['参考素材', 'Reference assets'],
    redraw_scope: ['允许重绘范围', 'Redraw scope'],
    prohibitions: ['禁止项', 'Prohibitions'],
    duration: ['总时长', 'Duration'],
    language: ['语言', 'Language'],
    story: ['故事与镜头要求', 'Story and shots'],
    audio_mode: ['声音模式', 'Audio mode'],
    dialogue_script: ['对白脚本', 'Dialogue script'],
};

function deliverableErrorLabel(code: string, isZh: boolean) {
    const labels: Record<string, [string, string]> = {
        presentation_picture_coverage_below_minimum: [
            'PPT 图片覆盖不足，请增加大幅主视觉或场景图后重新生成。',
            'Presentation imagery is too sparse; add larger hero or scene images and regenerate.',
        ],
        presentation_visual_quality_failed: [
            'PPT 视觉质量检查未通过，请根据检查项修订后重新生成。',
            'Presentation visual quality checks failed; revise the listed issues and regenerate.',
        ],
        deliverable_artifact_invalid: [
            '交付文件未通过结构检查，请重新生成。',
            'The deliverable failed structural checks; regenerate it.',
        ],
    };
    const label = labels[code];
    return label ? label[isZh ? 0 : 1] : code;
}

function initialSpec(workflow?: DeliverableWorkflow): Record<string, string | number> {
    if (!workflow) return {};
    return Object.fromEntries(
        workflow.fields
            .filter((field) => field.default !== null && field.default !== undefined)
            .map((field) => [field.key, field.default as string | number]),
    );
}

function workflowOptionLabel(fieldKey: string, option: string, isZh: boolean) {
    if (fieldKey === 'fallback_policy') {
        if (option === 'primary_only') {
            return isZh ? '正式质量优先（推荐）' : 'Formal quality first (recommended)';
        }
        if (option === 'allow_degraded') {
            return isZh ? '允许应急质量（需明确接受差异）' : 'Allow emergency quality (accept differences)';
        }
    }
    return option;
}

export function DeliverableLauncher({
    agentId,
    sessionId,
    taskId,
    initialWorkType,
    initialGoal,
    initialSpecOverrides,
    autoOpenKey,
    tier,
    attachments,
    disabled = false,
    onCreated,
}: DeliverableLauncherProps) {
    const { i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const toast = useToast();
    const triggerRef = useRef<HTMLButtonElement>(null);
    const drawerRef = useRef<HTMLElement>(null);
    const handledAutoOpenKeyRef = useRef('');
    const [open, setOpen] = useState(false);
    const [workflows, setWorkflows] = useState<DeliverableWorkflow[]>([]);
    const [loadingWorkflows, setLoadingWorkflows] = useState(false);
    const [workflowsLoaded, setWorkflowsLoaded] = useState(false);
    const [selectedType, setSelectedType] = useState<DeliverableWorkType>('presentation');
    const [goal, setGoal] = useState('');
    const [spec, setSpec] = useState<Record<string, string | number>>({});
    const [checking, setChecking] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [preflight, setPreflight] = useState<DeliverablePreflight | null>(null);
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [clientRequestId, setClientRequestId] = useState(() => crypto.randomUUID());

    const selectedWorkflow = useMemo(
        () => workflows.find((workflow) => workflow.work_type === selectedType),
        [selectedType, workflows],
    );
    const requiredWorkflowFields = useMemo(
        () => selectedWorkflow?.fields.filter((field) => field.required) ?? [],
        [selectedWorkflow],
    );
    const optionalWorkflowFields = useMemo(
        () => selectedWorkflow?.fields.filter((field) => !field.required) ?? [],
        [selectedWorkflow],
    );

    useEffect(() => {
        let active = true;
        setLoadingWorkflows(true);
        setWorkflowsLoaded(false);
        setWorkflows([]);
        deliverableApi.workflows(agentId, tier)
            .then((response) => {
                if (!active) return;
                setLoadingWorkflows(false);
                setWorkflowsLoaded(true);
                setWorkflows(response.workflows);
                const presentation = response.workflows.find((item) => item.work_type === 'presentation');
                const initial = presentation || response.workflows[0];
                if (initial) {
                    setSelectedType(initial.work_type);
                    setSpec(initialSpec(initial));
                    setPreflight(null);
                }
            })
            .catch((nextError) => {
                if (!active) return;
                setLoadingWorkflows(false);
                setWorkflowsLoaded(true);
                const message = nextError instanceof Error ? nextError.message : String(nextError);
                setError(message);
                toast.error(isZh ? '无法加载交付物工作流' : 'Could not load deliverable workflows', { details: message });
            });
        return () => { active = false; };
    }, [agentId, isZh, tier, toast]);

    useEffect(() => {
        if (
            !autoOpenKey
            || handledAutoOpenKeyRef.current === autoOpenKey
            || !sessionId
            || !initialWorkType
            || !initialGoal?.trim()
            || !workflowsLoaded
            || workflows.length === 0
        ) {
            return;
        }
        const workflow = workflows.find((item) => item.work_type === initialWorkType);
        if (!workflow) {
            setError(
                isZh
                    ? '当前数字员工没有与该任务匹配的正式交付工作流'
                    : 'This Agent has no formal delivery workflow matching the task',
            );
            setOpen(true);
            return;
        }
        handledAutoOpenKeyRef.current = autoOpenKey;
        const mergedSpec = {
            ...initialSpec(workflow),
            ...(initialSpecOverrides || {}),
        };
        const optionalFieldKeys = new Set(
            workflow.fields.filter((field) => !field.required).map((field) => field.key),
        );
        setSelectedType(workflow.work_type);
        setSpec(mergedSpec);
        setGoal(initialGoal.trim());
        setError('');
        setPreflight(null);
        setShowAdvanced(
            Object.keys(initialSpecOverrides || {}).some((key) => optionalFieldKeys.has(key)),
        );
        setClientRequestId(crypto.randomUUID());
        setOpen(true);
    }, [
        autoOpenKey,
        initialGoal,
        initialSpecOverrides,
        initialWorkType,
        isZh,
        sessionId,
        workflows,
        workflowsLoaded,
    ]);

    const closeDrawer = useCallback(() => {
        if (saving) return;
        setOpen(false);
        window.setTimeout(() => triggerRef.current?.focus(), 0);
    }, [saving]);

    useEffect(() => {
        if (!open) return;
        const drawer = drawerRef.current;
        const focusableSelector = [
            'button:not([disabled])',
            'input:not([disabled])',
            'select:not([disabled])',
            'textarea:not([disabled])',
            '[href]',
            '[tabindex]:not([tabindex="-1"])',
        ].join(',');
        const animationFrame = window.requestAnimationFrame(() => {
            drawer?.querySelector<HTMLElement>(focusableSelector)?.focus();
        });
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape' && !saving) {
                closeDrawer();
                return;
            }
            if (event.key !== 'Tab' || !drawer) return;
            const focusable = Array.from(drawer.querySelectorAll<HTMLElement>(focusableSelector));
            if (focusable.length === 0) {
                event.preventDefault();
                drawer.focus();
                return;
            }
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };
        document.addEventListener('keydown', onKeyDown);
        return () => {
            window.cancelAnimationFrame(animationFrame);
            document.removeEventListener('keydown', onKeyDown);
        };
    }, [closeDrawer, open, saving]);

    const selectWorkflow = (workflow: DeliverableWorkflow) => {
        setSelectedType(workflow.work_type);
        setSpec(initialSpec(workflow));
        setError('');
        setPreflight(null);
        setClientRequestId(crypto.randomUUID());
    };

    const updateField = (key: string, value: string | number) => {
        setSpec((current) => ({ ...current, [key]: value }));
        setError('');
        setPreflight(null);
    };

    const renderWorkflowField = (field: DeliverableWorkflow['fields'][number]) => {
        const label = isZh ? field.label_zh : field.label_en;
        const placeholder = isZh ? field.placeholder_zh : field.placeholder_en;
        const value = spec[field.key] ?? '';
        if (field.kind === 'textarea' || field.kind === 'json') {
            return (
                <label key={field.key} className="deliverable-field deliverable-field--full">
                    <span>{label} {field.required && <em>*</em>}</span>
                    <textarea value={value} onChange={(event) => updateField(field.key, event.target.value)} placeholder={placeholder} rows={3} />
                    {field.kind === 'json' && (
                        <small>
                            {isZh
                                ? '结构化 JSON 数组，可留空；保存时由服务端校验。'
                                : 'Structured JSON array; optional. Validated by the server on save.'}
                        </small>
                    )}
                </label>
            );
        }
        if (field.kind === 'select') {
            return (
                <label key={field.key} className="deliverable-field">
                    <span>{label} {field.required && <em>*</em>}</span>
                    <select value={value} onChange={(event) => updateField(field.key, event.target.value)}>
                        {field.options.map((option) => (
                            <option key={option} value={option}>
                                {workflowOptionLabel(field.key, option, isZh)}
                            </option>
                        ))}
                    </select>
                    {field.key === 'fallback_policy' && (
                        <small>
                            {isZh
                                ? '图片和视频仅有应急线路时，正式质量优先会保存工作说明但不提交付费任务。'
                                : 'When only an emergency image/video route remains, formal-quality mode saves the brief without submitting a paid task.'}
                        </small>
                    )}
                </label>
            );
        }
        return (
            <label key={field.key} className="deliverable-field">
                <span>{label} {field.required && <em>*</em>}</span>
                <input
                    type={field.kind === 'number' ? 'number' : 'text'}
                    value={value}
                    min={field.minimum ?? undefined}
                    max={field.maximum ?? undefined}
                    placeholder={placeholder}
                    onChange={(event) => updateField(
                        field.key,
                        field.kind === 'number' ? Number(event.target.value) : event.target.value,
                    )}
                />
            </label>
        );
    };

    const checkCapability = async (): Promise<DeliverablePreflight | null> => {
        if (!selectedWorkflow) return null;
        setChecking(true);
        setError('');
        try {
            const result = await deliverableApi.preflight({
                agent_id: agentId,
                work_type: selectedWorkflow.work_type,
                workflow_id: selectedWorkflow.workflow_id,
                workflow_version: selectedWorkflow.workflow_version,
                goal: goal.trim(),
                inputs: attachments
                    .filter((attachment): attachment is DeliverableAttachmentInput & { path: string } => Boolean(attachment.path))
                    .map((attachment) => ({
                        type: 'workspace_file' as const,
                        path: attachment.path,
                        name: attachment.name,
                    })),
                spec,
                tier,
            });
            setSpec(result.normalized_spec);
            setPreflight(result);
            if (!result.available) {
                const reasonText = result.reasons.length > 0
                    ? result.reasons.map((reason) => preflightReasonLabel(reason, isZh)).join(isZh ? '；' : '; ')
                    : (isZh ? '当前账号没有可用的执行路由' : 'No execution route is available for this account');
                setError(
                    isZh
                        ? `当前账号或档位暂不支持立即启动这项交付；工作说明仍可保存，不会扣 Credits。原因：${reasonText}。下一步：${result.next_action}`
                        : `This account or tier cannot start this delivery right now. The brief can still be saved and no Credits will be spent. Reason: ${reasonText}. Next: ${result.next_action}`,
                );
            }
            return result;
        } catch (nextError) {
            const message = nextError instanceof Error ? nextError.message : String(nextError);
            setPreflight(null);
            setError(message);
            return null;
        } finally {
            setChecking(false);
        }
    };

    const saveBrief = async () => {
        if (!selectedWorkflow || !sessionId) {
            setError(isZh ? '请先打开一个可用的对话会话' : 'Open an available chat session first');
            return;
        }
        if (goal.trim().length < 3) {
            setError(isZh ? '请写清楚希望交付的目标' : 'Describe the intended outcome');
            return;
        }
        setSaving(true);
        setError('');
        try {
            const result = await checkCapability();
            if (!result) return;
            const request = await deliverableApi.create({
                client_request_id: clientRequestId,
                agent_id: agentId,
                session_id: sessionId,
                ...(taskId ? { task_id: taskId } : {}),
                work_type: selectedWorkflow.work_type,
                workflow_id: selectedWorkflow.workflow_id,
                workflow_version: selectedWorkflow.workflow_version,
                goal: goal.trim(),
                inputs: attachments
                    .filter((attachment): attachment is DeliverableAttachmentInput & { path: string } => Boolean(attachment.path))
                    .map((attachment) => ({
                        type: 'workspace_file' as const,
                        path: attachment.path,
                        name: attachment.name,
                    })),
                spec: result.normalized_spec,
                tier,
                approval_policy: selectedWorkflow.approval_policy,
                output_contract: selectedWorkflow.output_contract,
            });
            const requestWithPreflight: DeliverableRequest = {
                ...request,
                latest_preflight: request.latest_preflight
                    || result as unknown as Record<string, unknown>,
            };
            onCreated(requestWithPreflight, result.launchable);
            setOpen(false);
            setClientRequestId(crypto.randomUUID());
            toast.success(
                !result.available
                    ? (isZh ? '工作说明已保存；当前没有可用线路，未启动生成' : 'Brief saved; no execution route is available yet, so generation was not started')
                    : result.capability_status === 'degraded'
                    ? result.launchable
                        ? (isZh ? '已按你确认的应急质量策略保存，可在对话中启动' : 'Saved with your confirmed emergency-quality policy and ready to launch in chat')
                        : (isZh ? '工作说明已保存；当前只有应急质量线路，正式执行将等待主线路' : 'Brief saved; only emergency quality is available, so formal execution will wait for the primary route')
                    : result.launchable
                        ? (isZh ? '工作说明已保存，可在对话中确认启动' : 'Brief saved and ready to launch in chat')
                        : (isZh ? '工作说明已保存；当前阶段不会调用生成服务' : 'Brief saved; generation is not started in this phase'),
                { details: result.next_action },
            );
            window.setTimeout(() => triggerRef.current?.focus(), 0);
        } catch (nextError) {
            const message = nextError instanceof Error ? nextError.message : String(nextError);
            setError(message);
        } finally {
            setSaving(false);
        }
    };

    const drawer = open ? createPortal(
        <div className="deliverable-drawer-layer" aria-hidden={false}>
            <button
                type="button"
                className="deliverable-drawer-backdrop"
                aria-label={isZh ? '关闭工作说明' : 'Close work brief'}
                onClick={closeDrawer}
                disabled={saving}
                tabIndex={-1}
            />
            <section
                ref={drawerRef}
                className="deliverable-drawer"
                role="dialog"
                aria-modal="true"
                aria-labelledby="deliverable-drawer-title"
                tabIndex={-1}
            >
                <header className="deliverable-drawer__header">
                    <div>
                        <span className="deliverable-drawer__eyebrow">{isZh ? '交付物工作台' : 'Deliverable workbench'}</span>
                        <h2 id="deliverable-drawer-title">{isZh ? '制作交付物' : 'Create a deliverable'}</h2>
                        <p>{isZh ? '直接描述业务结果；数字员工会按已确认的合同制作并交付。' : 'Describe the business outcome; the Agent executes the confirmed delivery contract.'}</p>
                    </div>
                    <button
                        type="button"
                        className="deliverable-icon-button"
                        onClick={closeDrawer}
                        aria-label={isZh ? '关闭' : 'Close'}
                    >
                        <IconX size={18} stroke={1.75} />
                    </button>
                </header>

                <div className="deliverable-drawer__body">
                    {workflows.length > 1 && <section className="deliverable-section" aria-labelledby="deliverable-type-heading">
                        <div className="deliverable-section__heading">
                            <span className="deliverable-step">1</span>
                            <div>
                                <h3 id="deliverable-type-heading">{isZh ? '选择交付物' : 'Choose a deliverable'}</h3>
                                <p>{isZh ? '选择业务结果，不需要选择具体模型。' : 'Choose the business outcome, not a model.'}</p>
                            </div>
                        </div>
                        <div className="deliverable-workflow-grid" aria-busy={loadingWorkflows}>
                            {workflows.map((workflow) => {
                                const active = workflow.work_type === selectedType;
                                return (
                                    <button
                                        type="button"
                                        key={workflow.workflow_id}
                                        className={`deliverable-workflow-card${active ? ' active' : ''}`}
                                        onClick={() => selectWorkflow(workflow)}
                                        aria-pressed={active}
                                    >
                                        <span className="deliverable-workflow-card__icon">{WORK_TYPE_ICONS[workflow.work_type]}</span>
                                        <span>
                                            <strong>{workflowLabel(workflow, isZh)}</strong>
                                            <small>{isZh ? workflow.description_zh : workflow.description_en}</small>
                                        </span>
                                    </button>
                                );
                            })}
                            {loadingWorkflows && <div className="deliverable-loading">{isZh ? '正在加载…' : 'Loading…'}</div>}
                        </div>
                    </section>}

                    {selectedWorkflow && (
                        <section className="deliverable-section" aria-labelledby="deliverable-brief-heading">
                            <div className="deliverable-section__heading">
                                <span className="deliverable-step">1</span>
                                <div>
                                    <h3 id="deliverable-brief-heading">{isZh ? '你想做什么？' : 'What should it communicate?'}</h3>
                                    <p>{isZh ? `使用当前 ${tier.toUpperCase()} 档位` : `Using the current ${tier.toUpperCase()} tier`}</p>
                                </div>
                            </div>
                            <div className="deliverable-form">
                                <label className="deliverable-field deliverable-field--full">
                                    <span>{isZh ? '交付目标' : 'Outcome'} <em>*</em></span>
                                    <textarea
                                        value={goal}
                                        onChange={(event) => { setGoal(event.target.value); setError(''); setPreflight(null); }}
                                        placeholder={isZh ? '例如：用上传的产品资料制作一份面向经销商的招商演示，突出卖点、渠道政策和合作方式' : 'e.g. Create a partner pitch from the attached product material, focusing on benefits, channel terms, and next steps'}
                                        rows={3}
                                    />
                                </label>
                                {requiredWorkflowFields.map(renderWorkflowField)}
                                {optionalWorkflowFields.length > 0 && (
                                    <button
                                        type="button"
                                        className="btn btn-secondary deliverable-preflight-button"
                                        onClick={() => setShowAdvanced((current) => !current)}
                                        aria-expanded={showAdvanced}
                                    >
                                        {showAdvanced
                                            ? (isZh ? '收起可选设置' : 'Hide optional settings')
                                            : (isZh ? '补充可选信息' : 'Add optional details')}
                                    </button>
                                )}
                                {showAdvanced && optionalWorkflowFields.map(renderWorkflowField)}
                                {error && <div className="deliverable-error" role="alert">{error}</div>}
                                {preflight && (
                                    <div className={`deliverable-preflight ${preflight.launchable ? 'is-ready' : 'is-blocked'}`}>
                                        <span className="deliverable-preflight__icon">
                                            {preflight.launchable
                                                ? <IconCheck size={18} stroke={1.8} />
                                                : <IconAlertCircle size={18} stroke={1.8} />}
                                        </span>
                                        <div>
                                            <strong>
                                                {preflight.launchable
                                                    ? (isZh ? '能力检查通过' : 'Capability check passed')
                                                    : (isZh ? '暂不可启动' : 'Not ready to launch')}
                                            </strong>
                                            {preflight.reasons.length > 0 && (
                                                <p>{preflight.reasons.map((reason) => preflightReasonLabel(reason, isZh)).join(isZh ? '；' : '; ')}</p>
                                            )}
                                            <small>{preflight.next_action}</small>
                                        </div>
                                    </div>
                                )}
                            </div>
                            <div className="deliverable-input-summary">
                                <span>{isZh ? '已附加资料' : 'Attached references'}</span>
                                <strong>{attachments.filter((item) => item.path).length}</strong>
                                <small>{isZh ? '仅保存 workspace 引用，不复制文件内容。' : 'Only workspace references are saved; file contents are not copied.'}</small>
                            </div>
                        </section>
                    )}

                </div>

                <footer className="deliverable-drawer__footer">
                    <div>
                        <strong>{isZh ? '发送前不会扣除 Credits' : 'No Credits are spent before you send'}</strong>
                        <small>{isZh ? '系统会先检查当前数字员工是否具备所选交付能力。' : 'The system validates this Agent\'s selected delivery capability first.'}</small>
                    </div>
                    <button type="button" className="btn btn-primary" disabled={saving || checking || !selectedWorkflow || !sessionId} onClick={() => void saveBrief()}>
                        {saving ? (isZh ? '正在保存…' : 'Saving…') : (isZh ? '确认并保存' : 'Confirm and save')}
                        {!saving && <IconChevronRight size={16} stroke={1.75} />}
                    </button>
                </footer>
            </section>
        </div>,
        document.body,
    ) : null;

    if (!workflowsLoaded || loadingWorkflows || workflows.length === 0) {
        return null;
    }

    return (
        <>
            <button
                ref={triggerRef}
                type="button"
                className="chat-composer-btn deliverable-launcher"
                onClick={() => setOpen(true)}
                disabled={disabled || !sessionId}
                aria-label={isZh ? '制作交付物' : 'Create a deliverable'}
                title={isZh ? '制作交付物' : 'Create a deliverable'}
            >
                <IconSparkles size={16} stroke={1.75} />
                <span>{isZh ? '制作' : 'Create'}</span>
            </button>
            {drawer}
        </>
    );
}

export function DeliverableRequestCard({ request, launchable, onRemove, onOpen, onUpdated }: DeliverableRequestCardProps) {
    const { i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const toast = useToast();
    const [clarifyOpen, setClarifyOpen] = useState(false);
    const [brief, setBrief] = useState<DeliverableBrief | null>(null);
    const [briefLoading, setBriefLoading] = useState(false);
    const [answers, setAnswers] = useState<Record<string, string>>({});
    const [clarifyError, setClarifyError] = useState('');
    const [clarifySaving, setClarifySaving] = useState(false);
    const clarifying = request.current_stage === 'brief_clarifying';
    const nextAction = deliverableNextAction(request);
    const label = {
        presentation: isZh ? 'PPT 演示文稿' : 'Presentation',
        poster: isZh ? '海报 / 图片' : 'Poster / Image',
        video: isZh ? '短视频' : 'Short video',
        report: isZh ? '报告' : 'Report',
        spreadsheet: isZh ? '表格' : 'Spreadsheet',
    }[request.work_type];

    const openClarification = async () => {
        setClarifyOpen(true);
        if (brief || briefLoading) return;
        setBriefLoading(true);
        setClarifyError('');
        try {
            setBrief(await deliverableApi.brief(request.id));
        } catch (nextError) {
            setClarifyError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setBriefLoading(false);
        }
    };

    const submitClarification = async () => {
        setClarifySaving(true);
        setClarifyError('');
        try {
            const answersPayload = Object.fromEntries(
                Object.entries(answers).filter(([, value]) => value.trim()),
            );
            const updatedBrief = await deliverableApi.clarify(request.id, {
                expected_version: request.version,
                answers: answersPayload,
            });
            setBrief(updatedBrief);
            if (updatedBrief.status === 'confirmed') {
                toast.success(isZh ? '工作说明已补齐，可在对话中启动' : 'Brief completed and ready to launch');
            }
            if (onUpdated) {
                onUpdated(await deliverableApi.get(request.id));
            }
            if (updatedBrief.status === 'confirmed') {
                setClarifyOpen(false);
            }
        } catch (nextError) {
            setClarifyError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setClarifySaving(false);
        }
    };

    return (
        <div className="deliverable-request-card" data-status={request.status}>
            <span className="deliverable-request-card__icon">{WORK_TYPE_ICONS[request.work_type] || <IconSparkles size={18} />}</span>
            <button type="button" className="deliverable-request-card__body" onClick={onOpen}>
                <span>
                    <strong>{label}</strong>
                    <small>
                        {clarifying
                            ? (isZh ? '工作说明待补充 · 暂不启动生成' : 'Brief needs details · generation not started')
                            : launchable
                                ? (isZh ? '工作说明已保存 · 发送后启动' : 'Brief saved · send to launch')
                                : (isZh ? '工作说明已保存 · 暂不启动生成' : 'Brief saved · generation not started')}
                    </small>
                    {!launchable && nextAction && <small>{nextAction}</small>}
                </span>
                <em>{request.tier.toUpperCase()}</em>
            </button>
            {clarifying && (
                <button
                    type="button"
                    className="deliverable-icon-button"
                    onClick={() => (clarifyOpen ? setClarifyOpen(false) : void openClarification())}
                    aria-label={isZh ? '补充工作说明' : 'Complete the brief'}
                    title={isZh ? '补充工作说明' : 'Complete the brief'}
                >
                    <IconAlertCircle size={16} stroke={1.75} />
                </button>
            )}
            <button type="button" className="deliverable-icon-button" onClick={onRemove} aria-label={isZh ? '从本次发送中移除' : 'Remove from this message'}>
                <IconX size={16} stroke={1.75} />
            </button>
            {clarifying && clarifyOpen && (
                <div className="deliverable-request-card__clarification">
                    {briefLoading && <small>{isZh ? '正在读取待补充要素…' : 'Loading missing details…'}</small>}
                    {brief && brief.missing_fields.length > 0 && (
                        <>
                            <small>
                                {isZh ? '补齐以下要素后才能开始制作：' : 'Complete these details before production:'}
                            </small>
                            {brief.missing_fields.map((field) => {
                                const fieldLabel = BRIEF_FIELD_LABELS[field];
                                const fieldName = fieldLabel ? fieldLabel[isZh ? 0 : 1] : field;
                                return (
                                    <label key={field} className="deliverable-field deliverable-field--full">
                                        <span>{fieldName}</span>
                                        <input
                                            type="text"
                                            value={answers[field] ?? ''}
                                            onChange={(event) => setAnswers((current) => ({ ...current, [field]: event.target.value }))}
                                        />
                                    </label>
                                );
                            })}
                            <button
                                type="button"
                                className="btn btn-primary"
                                disabled={clarifySaving || Object.values(answers).every((value) => !value.trim())}
                                onClick={() => void submitClarification()}
                            >
                                {clarifySaving
                                    ? (isZh ? '保存中…' : 'Saving…')
                                    : (isZh ? '保存补充信息' : 'Save answers')}
                            </button>
                        </>
                    )}
                    {brief && brief.missing_fields.length === 0 && (
                        <small>{isZh ? '要素已补齐。' : 'All details are complete.'}</small>
                    )}
                    {clarifyError && <small role="alert">{clarifyError}</small>}
                </div>
            )}
        </div>
    );
}


function latestArtifacts(request: DeliverableRequest) {
    const latest = new Map<string, DeliverableRequest['artifacts'][number]>();
    for (const artifact of request.artifacts) {
        const current = latest.get(artifact.artifact_key);
        if (!current || artifact.revision_number > current.revision_number) {
            latest.set(artifact.artifact_key, artifact);
        }
    }
    return Array.from(latest.values()).sort((left, right) => left.artifact_key.localeCompare(right.artifact_key));
}

function reviewerReasonLabel(reason: string | null, isZh: boolean) {
    const labels: Record<string, [string, string]> = {
        deliverable_creator_cannot_review: ['任务创建者不能参与本次检查', 'The task creator cannot review this delivery'],
        reviewer_identity_unavailable: ['该成员尚未完成可验证账号设置', 'This member does not have a verified account identity'],
    };
    const label = reason ? labels[reason] : undefined;
    return label ? label[isZh ? 0 : 1] : (isZh ? '暂不可参与检查' : 'Not currently eligible');
}

function artifactActionLabel(artifactType: string, isZh: boolean) {
    const labels: Record<string, [string, string]> = {
        pdf: ['在线预览', 'Preview online'],
        pptx: ['下载 PPTX', 'Download PPTX'],
        mp4: ['查看视频', 'View video'],
        png: ['查看图片', 'View image'],
        jpg: ['查看图片', 'View image'],
        jpeg: ['查看图片', 'View image'],
    };
    const label = labels[artifactType.toLowerCase()];
    return label ? label[isZh ? 0 : 1] : (isZh ? '下载文件' : 'Download file');
}

function previewPriority(artifactType: string) {
    const priorities: Record<string, number> = {
        pdf: 0,
        mp4: 1,
        png: 2,
        jpg: 2,
        jpeg: 2,
    };
    return priorities[artifactType.toLowerCase()] ?? Number.MAX_SAFE_INTEGER;
}


function revisionUnitLabel(unitKey: string, isZh: boolean) {
    const match = /^(slide|candidate|shot)-(\d+)$/.exec(unitKey);
    if (!match) return unitKey;
    const index = Number.parseInt(match[2], 10);
    const labels = {
        slide: isZh ? `第 ${index} 页` : `Slide ${index}`,
        candidate: isZh ? `方案 ${index}` : `Option ${index}`,
        shot: isZh ? `镜头 ${index}` : `Shot ${index}`,
    };
    return labels[match[1] as keyof typeof labels];
}


function uniqueRevisionUnits(units: DeliverableExecutionUnit[]) {
    const byKey = new Map<string, DeliverableExecutionUnit>();
    for (const unit of units) {
        if (/^(slide|candidate|shot)-\d+$/.test(unit.unit_key) && !byKey.has(unit.unit_key)) {
            byKey.set(unit.unit_key, unit);
        }
    }
    return [...byKey.values()].sort((left, right) => left.unit_key.localeCompare(right.unit_key));
}


export function DeliverableReviewCard({ request, onUpdated }: DeliverableReviewCardProps) {
    const { i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const toast = useToast();
    const [acting, setActing] = useState<'approve' | 'request_changes' | null>(null);
    const [qualityReview, setQualityReview] = useState<DeliverableQualityReview | null>(null);
    const [qualityReviewLoading, setQualityReviewLoading] = useState(false);
    const [qualityReviewSetupOpen, setQualityReviewSetupOpen] = useState(false);
    const [qualityReviewers, setQualityReviewers] = useState<DeliverableQualityReviewer[]>([]);
    const [selectedReviewerIds, setSelectedReviewerIds] = useState<string[]>([]);
    const [qualityReviewError, setQualityReviewError] = useState('');
    const [creatingQualityReview, setCreatingQualityReview] = useState(false);
    const [detailsOpen, setDetailsOpen] = useState(false);
    const [executions, setExecutions] = useState<DeliverableExecution[]>([]);
    const [executionsLoading, setExecutionsLoading] = useState(false);
    const [executionsError, setExecutionsError] = useState('');
    const [revisionOpen, setRevisionOpen] = useState(false);
    const [revisionInstruction, setRevisionInstruction] = useState('');
    const [selectedRevisionUnits, setSelectedRevisionUnits] = useState<string[]>([]);
    const [videoPreviewError, setVideoPreviewError] = useState(false);
    const detailsTriggerRef = useRef<HTMLButtonElement>(null);
    const detailsDrawerRef = useRef<HTMLElement>(null);
    const approvalActionIdRef = useRef(crypto.randomUUID());
    const revisionActionRef = useRef<{ fingerprint: string; id: string } | null>(null);
    const artifacts = latestArtifacts(request);
    const previewArtifact = [...artifacts]
        .sort((left, right) => previewPriority(left.artifact_type) - previewPriority(right.artifact_type))
        .find((artifact) => previewPriority(artifact.artifact_type) < Number.MAX_SAFE_INTEGER);
    const previewArtifactType = previewArtifact?.artifact_type.toLowerCase();
    const previewArtifactUrl = previewArtifact
        ? deliverableApi.artifactDownloadUrl(previewArtifact.id, { inline: true })
        : '';
    useEffect(() => {
        setVideoPreviewError(false);
    }, [previewArtifactUrl]);
    const awaitingReview = request.status === 'waiting_approval' && request.current_stage === 'output_review';
    const failedRequest = request.status === 'failed';
    const storyboardReview = request.workflow_id === 'builtin.video.v2'
        && request.status === 'waiting_approval'
        && request.current_stage === 'storyboard_review';
    const shotReview = request.workflow_id === 'builtin.video.v2'
        && request.status === 'ready'
        && request.current_stage === 'shot_review';
    const composeReady = request.workflow_id === 'builtin.video.v2'
        && request.status === 'ready'
        && request.current_stage === 'compose_ready';
    const storyboardApproved = request.workflow_id === 'builtin.video.v2'
        && request.status === 'ready'
        && request.current_stage === 'storyboard_approved';
    const isPresentationV2 = request.workflow_id === 'builtin.presentation.v2';
    const outlineReview = isPresentationV2
        && request.status === 'waiting_approval'
        && request.current_stage === 'outline_review';
    const outlineApproved = isPresentationV2
        && request.status === 'ready'
        && request.current_stage === 'outline_approved';
    const approvalBlocked = deliverableApprovalBlocked(request);
    // FR-P7: font substitutions recorded on the latest deck artifact, shown
    // during output review so a viewer-side fallback is never a surprise.
    const fontSubstitutions = (() => {
        const deck = artifacts.find((artifact) => artifact.artifact_type === 'pptx');
        const facts = deck?.evaluation?.facts;
        if (!facts || typeof facts !== 'object') return [];
        const entries = (facts as Record<string, unknown>).font_substitutions;
        if (!Array.isArray(entries)) return [];
        return entries
            .map((entry) => (entry && typeof entry === 'object' ? entry as Record<string, unknown> : null))
            .filter((entry): entry is Record<string, unknown> => Boolean(entry));
    })();
    const managedReviewRequired = Boolean(
        awaitingReview && request.approval_readiness?.quality_gate_required,
    );
    const workTypeLabel = {
        presentation: isZh ? 'PPT 交付任务' : 'Presentation delivery',
        poster: isZh ? '图片交付任务' : 'Image delivery',
        video: isZh ? '视频交付任务' : 'Video delivery',
        report: isZh ? '报告交付任务' : 'Report delivery',
        spreadsheet: isZh ? '表格交付任务' : 'Spreadsheet delivery',
    }[request.work_type];
    const eligibleReviewerCount = qualityReviewers.filter((reviewer) => reviewer.eligible).length;
    const qualityStatus = qualityReview?.status;
    const currentExecution = executions.find((item) => item.id === request.current_execution_id)
        || executions[0];
    const revisionUnits = uniqueRevisionUnits(currentExecution?.units || []);
    const failedShotKeys = [...new Set(
        (currentExecution?.units || [])
            .filter((unit) => (
                unit.status === 'failed'
                && (unit.stage_key === 'shot_generate' || unit.stage_key === 'shot_qa')
                && /^shot-\d+$/.test(unit.unit_key)
            ))
            .map((unit) => unit.unit_key),
    )].sort();
    const selectableRevisionUnits = storyboardReview || outlineReview
        ? []
        : shotReview
            ? revisionUnits.filter((unit) => failedShotKeys.includes(unit.unit_key))
            : revisionUnits;
    const candidateWallUnits = (currentExecution?.units || []).filter(
        (unit) => unit.stage_key === 'candidate_generate',
    );
    const candidateQaByKey = new Map(
        (currentExecution?.units || [])
            .filter((unit) => unit.stage_key === 'candidate_qa')
            .map((unit) => [unit.unit_key, unit]),
    );
    const showCandidateWall = request.workflow_id === 'builtin.poster.v2' && candidateWallUnits.length > 0;
    // FR-I6: the recorded selection (receipt, else the selection unit's
    // snapshot) marks which candidate the delivery will ship.
    const selectedCandidateKey = (() => {
        const receipts = currentExecution?.selections || [];
        if (receipts.length > 0) return receipts[receipts.length - 1].selected_unit_key;
        const selectionUnit = (currentExecution?.units || []).find((unit) => unit.stage_key === 'selection');
        const key = selectionUnit?.result_snapshot?.selected_unit_key;
        return typeof key === 'string' && key ? key : '';
    })();
    const isVideoV2 = request.workflow_id === 'builtin.video.v2';
    const shotTimelineUnits = isVideoV2
        ? uniqueRevisionUnits(currentExecution?.units || []).filter((unit) => /^shot-\d+$/.test(unit.unit_key))
        : [];
    const shotStageByKey = new Map<string, Record<string, DeliverableExecutionUnit['status']>>();
    if (isVideoV2) {
        for (const unit of currentExecution?.units || []) {
            if (!/^shot-\d+$/.test(unit.unit_key)) continue;
            const stages = shotStageByKey.get(unit.unit_key) || {};
            stages[unit.stage_key] = unit.status;
            shotStageByKey.set(unit.unit_key, stages);
        }
    }
    const shotQaByKey = new Map(
        (currentExecution?.units || [])
            .filter((unit) => unit.stage_key === 'shot_qa')
            .map((unit) => [unit.unit_key, unit]),
    );
    const storyboardUnit = isVideoV2
        ? (currentExecution?.units || []).find((unit) => unit.stage_key === 'storyboard')
        : undefined;
    const storyboardShots = (() => {
        const payload = storyboardUnit?.result_snapshot?.storyboard;
        if (!payload || typeof payload !== 'object') return [];
        const shots = (payload as { shots?: unknown }).shots;
        if (!Array.isArray(shots)) return [];
        return shots
            .map((shot) => (shot && typeof shot === 'object' ? shot as Record<string, unknown> : null))
            .filter((shot): shot is Record<string, unknown> => Boolean(shot));
    })();
    const outlineUnit = isPresentationV2
        ? (currentExecution?.units || []).find((unit) => unit.stage_key === 'outline')
        : undefined;
    const outlineSlides = (() => {
        const payload = outlineUnit?.result_snapshot?.outline;
        if (!payload || typeof payload !== 'object') return [];
        const slides = (payload as { slides?: unknown }).slides;
        if (!Array.isArray(slides)) return [];
        return slides
            .map((slide) => (slide && typeof slide === 'object' ? slide as Record<string, unknown> : null))
            .filter((slide): slide is Record<string, unknown> => Boolean(slide));
    })();
    const outlineClaim = (() => {
        const payload = outlineUnit?.result_snapshot?.outline;
        if (!payload || typeof payload !== 'object') return '';
        const claim = (payload as { one_sentence_claim?: unknown }).one_sentence_claim;
        return typeof claim === 'string' ? claim : '';
    })();
    const unitProgress = (currentExecution?.units || []).reduce(
        (summary, unit) => {
            summary.total += 1;
            if (unit.status === 'succeeded') summary.complete += 1;
            if (unit.status === 'running' || unit.status === 'reconciling') summary.active += 1;
            if (unit.status === 'blocked' || unit.status === 'failed') summary.blocked += 1;
            return summary;
        },
        { total: 0, complete: 0, active: 0, blocked: 0 },
    );
    const presentation = (() => {
        if (request.status === 'running') {
            return {
                title: isZh ? '正在生成交付文件' : 'Creating your deliverables',
                description: isZh ? '完成后可在这里预览和下载' : 'Preview and download the files here when ready',
                step: 0,
            };
        }
        if (storyboardReview) {
            return {
                title: isZh ? '分镜待批准' : 'Storyboard awaiting approval',
                description: isZh ? '批准只解除制作门禁；再发送一条聊天消息后才开始付费生成' : 'Approval only releases the gate; paid generation starts after your next chat message',
                step: 1,
            };
        }
        if (outlineReview) {
            return {
                title: isZh ? '大纲待批准' : 'Outline awaiting approval',
                description: isZh ? '批准只解除制作门禁；再发送一条聊天消息后才开始排版制作' : 'Approval only releases the gate; production starts after your next chat message',
                step: 1,
            };
        }
        if (outlineApproved) {
            return {
                title: isZh ? '大纲已批准' : 'Outline approved',
                description: isZh ? '发送聊天消息开始制作演示文稿' : 'Send a chat message to start building the deck',
                step: 1,
            };
        }
        if (storyboardApproved) {
            return {
                title: isZh ? '分镜已批准' : 'Storyboard approved',
                description: isZh ? '发送聊天消息开始逐镜头制作，此时才会产生费用' : 'Send a chat message to start per-shot production; spend starts then',
                step: 1,
            };
        }
        if (shotReview) {
            return {
                title: isZh ? '部分镜头需要重做' : 'Some shots need a redo',
                description: isZh ? '只有失败镜头会重新计费，已通过镜头保持不变' : 'Only failed shots are re-billed; completed shots are kept',
                step: 0,
            };
        }
        if (composeReady) {
            return {
                title: isZh ? '镜头已全部完成' : 'All shots are complete',
                description: isZh ? '发送聊天消息继续合成最终成片' : 'Send a chat message to assemble the final video',
                step: 1,
            };
        }
        if (request.status === 'failed') {
            return {
                title: isZh ? '生成遇到问题' : 'Generation needs attention',
                description: isZh ? '任务内容已保留，可以检查后重新生成' : 'Your task is saved and can be regenerated after review',
                step: 0,
            };
        }
        if (request.status === 'succeeded') {
            return {
                title: isZh ? '交付已确认' : 'Delivery confirmed',
                description: isZh ? '文件已归档，仍可随时下载' : 'Files are archived and remain available to download',
                step: 2,
            };
        }
        if (awaitingReview && qualityStatus === 'blocked') {
            return {
                title: isZh ? '检查发现需要修改的问题' : 'Changes are needed',
                description: isZh ? '查看检查结果后退回修改，再提交新版本' : 'Review the findings and return the work for revision',
                step: 1,
            };
        }
        if (awaitingReview && qualityStatus === 'passed' && !approvalBlocked) {
            return {
                title: isZh ? '文件已通过检查，可以交付' : 'Quality checks passed',
                description: isZh ? '请预览最终文件，然后确认交付' : 'Preview the final files, then confirm delivery',
                step: 2,
            };
        }
        if (awaitingReview && qualityReview) {
            return {
                title: isZh ? '质量检查进行中' : 'Quality check in progress',
                description: isZh
                    ? `已完成 ${qualityReview.submitted_reviewer_count}/${qualityReview.assigned_reviewer_count} 份独立检查`
                    : `${qualityReview.submitted_reviewer_count}/${qualityReview.assigned_reviewer_count} independent checks completed`,
                step: 1,
            };
        }
        if (awaitingReview && !managedReviewRequired) {
            return {
                title: approvalBlocked
                    ? (isZh ? '交付检查尚未完成' : 'Delivery checks are incomplete')
                    : (isZh ? '文件已生成，等待确认' : 'Files are ready for confirmation'),
                description: deliverableApprovalStatusMessage(request, isZh),
                step: approvalBlocked ? 1 : 2,
            };
        }
        if (awaitingReview) {
            return {
                title: isZh ? '文件已生成，等待质量检查' : 'Files are ready for quality review',
                description: qualityReviewLoading
                    ? (isZh ? '正在读取检查进度…' : 'Loading review progress…')
                    : (isZh ? '安排 3 位同事独立检查后即可确认交付' : 'Assign three colleagues to review before delivery'),
                step: 1,
            };
        }
        return {
            title: workTypeLabel,
            description: isZh ? '正在处理交付任务' : 'Processing delivery task',
            step: 0,
        };
    })();
    const hasPartialArtifacts = artifacts.length > 0 && request.status === 'failed';
    const compactTitle = hasPartialArtifacts
        ? (isZh ? '部分文件已生成' : 'Some files are ready')
        : artifacts.length > 0
            ? {
                presentation: isZh ? 'PPT 已生成' : 'Presentation ready',
                poster: isZh ? '图片已生成' : 'Image ready',
                video: isZh ? '视频已生成' : 'Video ready',
                report: isZh ? '报告已生成' : 'Report ready',
                spreadsheet: isZh ? '表格已生成' : 'Spreadsheet ready',
            }[request.work_type]
            : presentation.title;
    const compactDescription = hasPartialArtifacts
        ? (isZh ? '仍有交付项未完成，请查看详情' : 'Some deliverables still need attention; view details')
        : compactTitle === presentation.title
            ? presentation.description
            : presentation.title;

    const closeDetails = useCallback(() => {
        setDetailsOpen(false);
        window.setTimeout(() => detailsTriggerRef.current?.focus(), 0);
    }, []);

    const approveDelivery = async () => {
        setActing('approve');
        try {
            const updated = await deliverableApi.approval(request.id, {
                expected_version: request.version,
                client_action_id: approvalActionIdRef.current,
                stage: 'final',
                action: 'approve',
            });
            onUpdated(updated);
            toast.success(isZh ? '交付已确认' : 'Delivery confirmed');
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            toast.error(isZh ? '交付操作失败' : 'Deliverable action failed', { details: message });
        } finally {
            setActing(null);
        }
    };

    // FR-I6: re-selecting another QA-passed candidate is the same idempotent
    // approval action carrying that candidate as its target unit.
    const selectCandidateAndApprove = async (unitKey: string) => {
        setActing('approve');
        try {
            const updated = await deliverableApi.approval(request.id, {
                expected_version: request.version,
                client_action_id: approvalActionIdRef.current,
                stage: 'final',
                action: 'approve',
                target_units: [unitKey],
            });
            onUpdated(updated);
            toast.success(isZh ? '已按所选方案确认交付' : 'Delivery confirmed with the selected candidate');
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            toast.error(isZh ? '候选改选失败' : 'Candidate re-selection failed', { details: message });
        } finally {
            setActing(null);
        }
    };

    const approveStoryboard = async () => {
        setActing('approve');
        try {
            const updated = await deliverableApi.approval(request.id, {
                expected_version: request.version,
                client_action_id: approvalActionIdRef.current,
                stage: 'storyboard',
                action: 'approve',
            });
            onUpdated(updated);
            toast.success(
                isZh
                    ? '分镜已批准，发送消息即可开始逐镜头制作'
                    : 'Storyboard approved; send the message to start per-shot production',
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            toast.error(isZh ? '分镜审批失败' : 'Storyboard approval failed', { details: message });
        } finally {
            setActing(null);
        }
    };

    const approveOutline = async () => {
        setActing('approve');
        try {
            const updated = await deliverableApi.approval(request.id, {
                expected_version: request.version,
                client_action_id: approvalActionIdRef.current,
                stage: 'outline',
                action: 'approve',
            });
            onUpdated(updated);
            toast.success(
                isZh
                    ? '大纲已批准，发送消息即可开始制作'
                    : 'Outline approved; send the message to start production',
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            toast.error(isZh ? '大纲审批失败' : 'Outline approval failed', { details: message });
        } finally {
            setActing(null);
        }
    };

    const submitRevision = async () => {
        const instruction = revisionInstruction.trim();
        if (instruction.length < 3) {
            toast.error(isZh ? '请说明需要修改的内容' : 'Describe the requested changes');
            return;
        }
        const revisionTargets = storyboardReview || outlineReview
            ? []
            : selectedRevisionUnits;
        if (shotReview && revisionTargets.length === 0) {
            toast.error(isZh ? '请选择至少一个失败镜头' : 'Select at least one failed shot');
            return;
        }
        const fingerprint = JSON.stringify([instruction, revisionTargets]);
        if (revisionActionRef.current?.fingerprint !== fingerprint) {
            revisionActionRef.current = { fingerprint, id: crypto.randomUUID() };
        }
        setActing('request_changes');
        try {
            const updated = await deliverableApi.approval(request.id, {
                expected_version: request.version,
                client_action_id: revisionActionRef.current.id,
                // Storyboard/outline and shot reviews post their own stage so
                // the server can keep v1 final-review semantics untouched.
                stage: storyboardReview ? 'storyboard' : outlineReview ? 'outline' : 'final',
                action: 'request_changes',
                instruction,
                target_units: revisionTargets,
            });
            onUpdated(updated);
            setRevisionOpen(false);
            setRevisionInstruction('');
            setSelectedRevisionUnits([]);
            revisionActionRef.current = null;
            toast.success(
                isZh
                    ? '已创建修订版本，原文件、工作说明和检查记录均已保留'
                    : 'A revision was created; prior files, brief, and review records were preserved',
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            toast.error(isZh ? '无法创建修订版本' : 'Could not create revision', { details: message });
        } finally {
            setActing(null);
        }
    };

    const toggleRevisionForm = () => {
        if (revisionOpen) {
            setRevisionOpen(false);
            return;
        }
        setSelectedRevisionUnits(shotReview ? failedShotKeys : []);
        setRevisionOpen(true);
    };

    useEffect(() => {
        approvalActionIdRef.current = crypto.randomUUID();
        revisionActionRef.current = null;
    }, [request.id, request.version]);

    useEffect(() => {
        if (!detailsOpen) return;
        let active = true;
        setExecutionsLoading(true);
        deliverableApi.executions(request.id)
            .then((items) => {
                if (!active) return;
                setExecutions(items);
                setExecutionsError('');
            })
            .catch((error) => {
                if (!active) return;
                setExecutionsError(error instanceof Error ? error.message : String(error));
            })
            .finally(() => {
                if (active) setExecutionsLoading(false);
            });
        return () => { active = false; };
    }, [detailsOpen, request.id, request.version]);

    useEffect(() => {
        if (!managedReviewRequired) {
            setQualityReview(null);
            return;
        }
        let active = true;
        setQualityReviewLoading(true);
        deliverableApi.latestQualityReview(request.id)
            .then((review) => {
                if (!active) return;
                setQualityReview(review);
                setQualityReviewError('');
            })
            .catch((error) => {
                if (!active) return;
                setQualityReviewError(error instanceof Error ? error.message : String(error));
            })
            .finally(() => {
                if (active) setQualityReviewLoading(false);
            });
        return () => { active = false; };
    }, [managedReviewRequired, request.id, request.version]);

    useEffect(() => {
        if (!detailsOpen) return;
        const drawer = detailsDrawerRef.current;
        const focusableSelector = [
            'button:not([disabled])',
            'input:not([disabled])',
            'select:not([disabled])',
            'textarea:not([disabled])',
            '[href]',
            '[tabindex]:not([tabindex="-1"])',
        ].join(',');
        const animationFrame = window.requestAnimationFrame(() => {
            drawer?.querySelector<HTMLElement>(focusableSelector)?.focus();
        });
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                closeDetails();
                return;
            }
            if (event.key !== 'Tab' || !drawer) return;
            const focusable = Array.from(drawer.querySelectorAll<HTMLElement>(focusableSelector));
            if (focusable.length === 0) {
                event.preventDefault();
                drawer.focus();
                return;
            }
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };
        document.addEventListener('keydown', onKeyDown);
        return () => {
            window.cancelAnimationFrame(animationFrame);
            document.removeEventListener('keydown', onKeyDown);
        };
    }, [closeDetails, detailsOpen]);

    const openQualityReviewSetup = async () => {
        setQualityReviewSetupOpen(true);
        setQualityReviewError('');
        try {
            const reviewers = await deliverableApi.qualityReviewers(request.id);
            setQualityReviewers(reviewers);
            setSelectedReviewerIds(
                reviewers.filter((item) => item.eligible).slice(0, 3).map((item) => item.user_id),
            );
        } catch (error) {
            setQualityReviewError(error instanceof Error ? error.message : String(error));
        }
    };

    const createQualityReview = async () => {
        if (selectedReviewerIds.length < 3) return;
        setCreatingQualityReview(true);
        setQualityReviewError('');
        try {
            const review = await deliverableApi.createQualityReview(request.id, {
                client_review_id: crypto.randomUUID(),
                expected_request_version: request.version,
                reviewer_user_ids: selectedReviewerIds,
            });
            setQualityReview(review);
            setQualityReviewSetupOpen(false);
            toast.success(
                isZh ? '质量检查已安排，评审人现在可以开始检查' : 'Quality review assigned; reviewers can now begin',
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setQualityReviewError(message);
            toast.error(isZh ? '无法创建质量评审' : 'Could not create quality review', { details: message });
        } finally {
            setCreatingQualityReview(false);
        }
    };

    const detailsDrawer = detailsOpen ? createPortal(
        <div className="deliverable-drawer-layer" aria-hidden={false}>
            <button
                type="button"
                className="deliverable-drawer-backdrop"
                aria-label={isZh ? '关闭交付详情' : 'Close delivery details'}
                onClick={closeDetails}
                tabIndex={-1}
            />
            <section
                ref={detailsDrawerRef}
                className="deliverable-drawer deliverable-detail-drawer"
                role="dialog"
                aria-modal="true"
                aria-labelledby={`deliverable-detail-title-${request.id}`}
                tabIndex={-1}
            >
                <header className="deliverable-drawer__header">
                    <div>
                        <span className="deliverable-drawer__eyebrow">{workTypeLabel}</span>
                        <h2 id={`deliverable-detail-title-${request.id}`}>{presentation.title}</h2>
                        <p>{presentation.description}</p>
                    </div>
                    <button
                        type="button"
                        className="deliverable-icon-button"
                        onClick={closeDetails}
                        aria-label={isZh ? '关闭' : 'Close'}
                    >
                        <IconX size={18} stroke={1.75} />
                    </button>
                </header>
                <div className="deliverable-drawer__body deliverable-detail-drawer__body">
        <section className="deliverable-review-card deliverable-review-card--drawer" data-status={request.status} aria-live="polite">
            <ol className="deliverable-review-card__steps" aria-label={isZh ? '交付进度' : 'Delivery progress'}>
                {[
                    isZh ? '预览文件' : 'Preview files',
                    isZh ? '质量检查' : 'Quality check',
                    isZh ? '确认交付' : 'Confirm delivery',
                ].map((label, index) => (
                    <li
                        key={label}
                        data-state={index < presentation.step ? 'complete' : index === presentation.step ? 'current' : 'upcoming'}
                    >
                        <span>{index < presentation.step ? <IconCheck size={13} /> : index + 1}</span>
                        {label}
                    </li>
                ))}
            </ol>
            <section className="deliverable-execution-progress" aria-label={isZh ? '本版制作进度' : 'Current revision progress'}>
                <header>
                    <div>
                        <strong>
                            {isZh ? `第 ${currentExecution?.execution_number || request.contract_revision || 1} 版` : `Revision ${currentExecution?.execution_number || request.contract_revision || 1}`}
                        </strong>
                        <small>
                            {executionsLoading
                                ? (isZh ? '正在读取制作进度…' : 'Loading production progress…')
                                : unitProgress.total > 0
                                    ? (isZh ? `${unitProgress.complete}/${unitProgress.total} 个制作步骤已完成` : `${unitProgress.complete}/${unitProgress.total} production steps complete`)
                                    : (isZh ? '制作记录会随任务推进自动更新' : 'Production records update as the task advances')}
                        </small>
                    </div>
                    {executions.length > 1 && (
                        <span>{isZh ? `保留 ${executions.length} 个版本` : `${executions.length} versions retained`}</span>
                    )}
                </header>
                {unitProgress.total > 0 && (
                    <div className="deliverable-execution-progress__bar" aria-hidden="true">
                        <span style={{ width: `${Math.round((unitProgress.complete / unitProgress.total) * 100)}%` }} />
                    </div>
                )}
                {(unitProgress.active > 0 || unitProgress.blocked > 0) && (
                    <small className="deliverable-execution-progress__status">
                        {unitProgress.active > 0 && (isZh ? `${unitProgress.active} 项正在制作` : `${unitProgress.active} active`)}
                        {unitProgress.active > 0 && unitProgress.blocked > 0 ? ' · ' : ''}
                        {unitProgress.blocked > 0 && (isZh ? `${unitProgress.blocked} 项需要处理` : `${unitProgress.blocked} need attention`)}
                    </small>
                )}
                {executionsError && (
                    <small className="deliverable-execution-progress__error">
                        {isZh ? '暂时无法读取详细进度，文件预览和交付操作不受影响。' : 'Detailed progress is temporarily unavailable; preview and delivery actions still work.'}
                    </small>
                )}
            </section>
            {showCandidateWall && (
                <section className="deliverable-candidate-wall" aria-label={isZh ? '候选方案' : 'Candidates'}>
                    <header>
                        <strong>{isZh ? `候选方案（${candidateWallUnits.length}）` : `Candidates (${candidateWallUnits.length})`}</strong>
                    </header>
                    <div className="deliverable-candidate-wall__grid">
                        {candidateWallUnits.map((unit) => {
                            const qaUnit = candidateQaByKey.get(unit.unit_key);
                            const qa = qaUnit?.qa_summary;
                            const isSelected = selectedCandidateKey === unit.unit_key;
                            const canSelect = awaitingReview
                                && !approvalBlocked
                                && !isSelected
                                && qa?.status === 'passed';
                            return (
                                <div key={unit.unit_key} className="deliverable-candidate-card" data-status={unit.status}>
                                    <strong>
                                        {revisionUnitLabel(unit.unit_key, isZh)}
                                        {isSelected && (
                                            <small>{isZh ? ' · 当前选择' : ' · selected'}</small>
                                        )}
                                    </strong>
                                    <small>{unit.status}</small>
                                    {qa && (
                                        <small title={qa.artifact_sha256 || undefined}>
                                            {isZh ? '自动 QA：' : 'Automated QA: '}
                                            {qa.status ?? '—'}
                                            {typeof qa.score === 'number' ? ` · ${qa.score}` : ''}
                                            {qa.artifact_sha256 ? ` · #${qa.artifact_sha256.slice(0, 8)}` : ''}
                                        </small>
                                    )}
                                    {canSelect && (
                                        <button
                                            type="button"
                                            className="btn btn-secondary"
                                            disabled={acting !== null}
                                            onClick={() => void selectCandidateAndApprove(unit.unit_key)}
                                        >
                                            {isZh ? '选此方案交付' : 'Deliver this candidate'}
                                        </button>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </section>
            )}
            {isVideoV2 && shotTimelineUnits.length > 0 && (
                <section className="deliverable-candidate-wall" aria-label={isZh ? '逐镜头时间线' : 'Shot timeline'}>
                    <header>
                        <strong>{isZh ? `逐镜头时间线（${shotTimelineUnits.length}）` : `Shot timeline (${shotTimelineUnits.length})`}</strong>
                    </header>
                    <div className="deliverable-candidate-wall__grid">
                        {shotTimelineUnits.map((unit) => {
                            const stages = shotStageByKey.get(unit.unit_key) || {};
                            const qa = shotQaByKey.get(unit.unit_key)?.qa_summary;
                            const shotStatus = stages.shot_generate || unit.status;
                            return (
                                <div key={unit.unit_key} className="deliverable-candidate-card" data-status={shotStatus}>
                                    <strong>{revisionUnitLabel(unit.unit_key, isZh)}</strong>
                                    <small>
                                        {(isZh ? '首帧：' : 'keyframe: ') + (stages.keyframe_pack || '—')}
                                        {' · '}
                                        {(isZh ? '镜头：' : 'clip: ') + shotStatus}
                                    </small>
                                    {qa && (
                                        <small title={qa.artifact_sha256 || undefined}>
                                            {isZh ? '镜头 QA：' : 'Shot QA: '}
                                            {qa.status ?? '—'}
                                            {typeof qa.score === 'number' ? ` · ${qa.score}` : ''}
                                            {qa.artifact_sha256 ? ` · #${qa.artifact_sha256.slice(0, 8)}` : ''}
                                        </small>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </section>
            )}
            {storyboardReview && (
                <section className="deliverable-candidate-wall" aria-label={isZh ? '分镜审批' : 'Storyboard review'}>
                    <header>
                        <strong>{isZh ? '分镜待批准' : 'Storyboard awaiting approval'}</strong>
                        <small>
                            {isZh
                                ? '批准前不会产生任何生成费用；批准后发送消息开始逐镜头制作。'
                                : 'No generation spend happens before approval; send the message after approving to start per-shot production.'}
                        </small>
                    </header>
                    <div className="deliverable-candidate-wall__grid">
                        {storyboardShots.map((shot, index) => (
                            <div key={String(shot.shot_id || index)} className="deliverable-candidate-card">
                                <strong>{String(shot.shot_id || `shot-${index + 1}`)}</strong>
                                <small>{`${String(shot.duration_seconds ?? '—')}s`}</small>
                                <small>{String(shot.visual || '')}</small>
                                {Boolean(shot.caption) && <small>{isZh ? `字幕：${String(shot.caption)}` : `Caption: ${String(shot.caption)}`}</small>}
                                {Boolean(shot.dialogue) && <small>{isZh ? `对白：${String(shot.dialogue)}` : `Dialogue: ${String(shot.dialogue)}`}</small>}
                            </div>
                        ))}
                    </div>
                </section>
            )}
            {outlineReview && (
                <section className="deliverable-candidate-wall" aria-label={isZh ? '大纲审批' : 'Outline review'}>
                    <header>
                        <strong>{isZh ? '大纲待批准' : 'Outline awaiting approval'}</strong>
                        <small>
                            {isZh
                                ? '批准前不会排版或产生任何费用；事实断言必须可溯源或标注为假设。'
                                : 'No rendering or spend happens before approval; fact assertions must be sourced or labelled as assumptions.'}
                        </small>
                    </header>
                    {outlineClaim && (
                        <small>
                            {isZh ? `一句话主张：${outlineClaim}` : `Core claim: ${outlineClaim}`}
                        </small>
                    )}
                    <div className="deliverable-candidate-wall__grid">
                        {outlineSlides.map((slide, index) => (
                            <div key={String(slide.slide_id || index)} className="deliverable-candidate-card">
                                <strong>{String(slide.slide_id || `slide-${index + 1}`)}</strong>
                                <small>{String(slide.headline || '')}</small>
                                <small>{String(slide.purpose || '')}</small>
                            </div>
                        ))}
                    </div>
                </section>
            )}
            {previewArtifact && (
                <section className="deliverable-review-card__preview" aria-label={isZh ? '交付文件预览' : 'Deliverable preview'}>
                    {previewArtifactType === 'mp4' && (
                        <>
                            <video
                                controls
                                playsInline
                                preload="metadata"
                                src={previewArtifactUrl}
                                onError={() => setVideoPreviewError(true)}
                            />
                            {videoPreviewError && (
                                <p role="status">
                                    {isZh ? '当前浏览器无法播放此视频，请下载 MP4 审核。' : 'This browser cannot play the video; download the MP4 to review it.'}
                                </p>
                            )}
                        </>
                    )}
                    {['png', 'jpg', 'jpeg'].includes(previewArtifactType || '') && (
                        <img
                            src={previewArtifactUrl}
                            alt={isZh ? '交付图片预览' : 'Deliverable image preview'}
                        />
                    )}
                    {previewArtifactType === 'pdf' && (
                        <iframe
                            src={previewArtifactUrl}
                            title={isZh ? 'PPT 逐页预览' : 'Presentation page preview'}
                        />
                    )}
                </section>
            )}
            {artifacts.length > 0 && (
                <div className="deliverable-review-card__artifacts">
                    {artifacts.map((artifact) => (
                        <a
                            key={artifact.id}
                            href={deliverableApi.artifactDownloadUrl(artifact.id, { inline: artifact.artifact_type === 'pdf' })}
                            target={artifact.artifact_type === 'pdf' ? '_blank' : undefined}
                            rel={artifact.artifact_type === 'pdf' ? 'noreferrer' : undefined}
                            download={artifact.artifact_type === 'pdf' ? undefined : true}
                        >
                            <IconDownload size={15} />
                            <span>{artifactActionLabel(artifact.artifact_type, isZh)}</span>
                            <small>{artifact.artifact_type.toUpperCase()}</small>
                        </a>
                    ))}
                </div>
            )}
            {fontSubstitutions.length > 0 && (
                <div className="deliverable-review-card__notice" role="status">
                    <IconAlertCircle size={16} />
                    <span>
                        {isZh ? '字体替换：' : 'Font substitutions: '}
                        {fontSubstitutions
                            .map((entry) => `${String(entry.requested)} → ${String(entry.actual)}`)
                            .join('；')}
                    </span>
                </div>
            )}
            {request.status === 'failed' && request.last_error_code && (
                <div className="deliverable-review-card__error">
                    {deliverableErrorLabel(request.last_error_code, isZh)}
                </div>
            )}
            {managedReviewRequired && (
                <div className="deliverable-review-card__quality" data-status={qualityStatus || 'not_started'}>
                    <span className="deliverable-review-card__quality-icon">
                        {qualityStatus === 'passed'
                            ? <IconCheck size={18} />
                            : qualityStatus === 'blocked'
                                ? <IconAlertCircle size={18} />
                                : <IconClock size={18} />}
                    </span>
                    <div>
                        <strong>
                            {qualityStatus === 'passed'
                                ? (isZh ? '质量检查已通过' : 'Quality check passed')
                                : qualityStatus === 'blocked'
                                    ? (isZh ? '质量检查发现问题' : 'Quality check found issues')
                                    : qualityReview
                                        ? (isZh ? '等待评审人完成检查' : 'Waiting for reviewers')
                                        : (isZh ? '安排质量检查' : 'Arrange quality review')}
                        </strong>
                        <small>
                            {qualityReviewLoading
                                ? (isZh ? '正在读取检查进度…' : 'Loading review progress…')
                                : qualityReview
                                    ? (isZh
                                        ? `${qualityReview.submitted_reviewer_count}/${qualityReview.assigned_reviewer_count} 位评审人已完成`
                                        : `${qualityReview.submitted_reviewer_count}/${qualityReview.assigned_reviewer_count} reviewers completed`)
                                    : (isZh ? '选择 3 位同事独立检查，系统会自动汇总结果' : 'Choose three colleagues; results are combined automatically')}
                        </small>
                    </div>
                    {qualityReview ? (
                        <a
                            className="btn btn-secondary"
                            href={`/quality-reviews/${qualityReview.id}`}
                        >
                            {qualityReview.current_user_can_submit
                                ? (isZh ? '开始我的检查' : 'Start my review')
                                : qualityStatus === 'blocked'
                                    ? (isZh ? '查看问题' : 'View issues')
                                    : (isZh ? '查看检查进度' : 'View progress')}
                            <IconChevronRight size={15} />
                        </a>
                    ) : (
                        <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={qualityReviewLoading}
                            onClick={() => void openQualityReviewSetup()}
                        >
                            {isZh ? '选择评审人' : 'Choose reviewers'}
                        </button>
                    )}
                </div>
            )}
            {managedReviewRequired && qualityReviewSetupOpen && !qualityReview && (
                <div className="deliverable-review-card__quality-setup">
                    <strong>{isZh ? '选择 3 位评审人' : 'Choose three reviewers'}</strong>
                    <small>
                        {isZh
                            ? '为避免自己检查自己的工作，任务创建者不能参与本次检查。'
                            : 'The task creator cannot participate so the review remains independent.'}
                    </small>
                    <div className="deliverable-review-card__reviewers">
                        {qualityReviewers.map((reviewer) => {
                            const checked = selectedReviewerIds.includes(reviewer.user_id);
                            return (
                                <label key={reviewer.user_id} data-disabled={!reviewer.eligible}>
                                    <input
                                        type="checkbox"
                                        checked={checked}
                                        disabled={!reviewer.eligible}
                                        onChange={(event) => {
                                            setSelectedReviewerIds((current) => (
                                                event.target.checked
                                                    ? [...current, reviewer.user_id]
                                                    : current.filter((id) => id !== reviewer.user_id)
                                            ));
                                        }}
                                    />
                                    <span>{reviewer.display_name}</span>
                                    <small>
                                        {reviewer.eligible
                                            ? (isZh ? '可以参与' : 'Available')
                                            : reviewerReasonLabel(reviewer.ineligible_reason, isZh)}
                                    </small>
                                </label>
                            );
                        })}
                    </div>
                    {qualityReviewers.length > 0 && eligibleReviewerCount < 3 && (
                        <div className="deliverable-review-card__notice">
                            <IconAlertCircle size={16} />
                            <span>
                                {isZh
                                    ? `目前只有 ${eligibleReviewerCount} 位可用评审人，还差 ${3 - eligibleReviewerCount} 位。`
                                    : `Only ${eligibleReviewerCount} eligible reviewers are available; ${3 - eligibleReviewerCount} more required.`}
                                {' '}
                                <a href="/enterprise#org">{isZh ? '管理企业成员' : 'Manage organization members'}</a>
                            </span>
                        </div>
                    )}
                    <div className="deliverable-review-card__actions">
                        <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={creatingQualityReview}
                            onClick={() => setQualityReviewSetupOpen(false)}
                        >
                            {isZh ? '取消' : 'Cancel'}
                        </button>
                        <button
                            type="button"
                            className="btn btn-primary"
                            disabled={creatingQualityReview || selectedReviewerIds.length < 3}
                            onClick={() => void createQualityReview()}
                        >
                            {creatingQualityReview
                                ? (isZh ? '正在安排…' : 'Assigning…')
                                : `${isZh ? '开始质量检查' : 'Start quality review'} (${selectedReviewerIds.length}/3)`}
                        </button>
                    </div>
                </div>
            )}
            {qualityReviewError && (
                <div className="deliverable-review-card__error" title={qualityReviewError}>
                    {isZh ? '暂时无法读取质量检查，请稍后重试。' : 'Quality review is temporarily unavailable. Please try again.'}
                </div>
            )}
            {(awaitingReview || storyboardReview || shotReview || outlineReview || failedRequest) && revisionOpen && (
                <section className="deliverable-revision-form" aria-label={isZh ? '创建修订版本' : 'Create revision'}>
                    <div>
                        <strong>{isZh ? '说明需要修改的内容' : 'Describe the requested changes'}</strong>
                        <small>
                            {isZh
                                ? '系统会创建新版本；当前文件、原工作说明和质量检查记录不会被覆盖。'
                                : 'A new revision will be created; current files, the original brief, and quality records stay intact.'}
                        </small>
                    </div>
                    <textarea
                        value={revisionInstruction}
                        maxLength={4000}
                        rows={4}
                        placeholder={isZh ? '例如：第 3 页减少文字并突出核心数据，第 5 页更换人物主视觉。' : 'For example: simplify slide 3 and emphasize the key metric; replace the hero image on slide 5.'}
                        onChange={(event) => setRevisionInstruction(event.target.value)}
                    />
                    {selectableRevisionUnits.length > 0 && (
                        <fieldset>
                            <legend>
                                {shotReview
                                    ? (isZh ? '选择需要重做的失败镜头（必选）' : 'Select failed shots to redo (required)')
                                    : (isZh ? '只修改指定部分（可选）' : 'Limit changes to selected items (optional)')}
                            </legend>
                            <div className="deliverable-revision-form__units">
                                {selectableRevisionUnits.map((unit) => {
                                    const checked = selectedRevisionUnits.includes(unit.unit_key);
                                    return (
                                        <label key={unit.unit_key}>
                                            <input
                                                type="checkbox"
                                                checked={checked}
                                                onChange={(event) => {
                                                    setSelectedRevisionUnits((current) => (
                                                        event.target.checked
                                                            ? [...current, unit.unit_key]
                                                            : current.filter((key) => key !== unit.unit_key)
                                                    ));
                                                }}
                                            />
                                            <span>{revisionUnitLabel(unit.unit_key, isZh)}</span>
                                        </label>
                                    );
                                })}
                            </div>
                            <small>
                                {selectedRevisionUnits.length > 0
                                    ? (isZh ? `已选择 ${selectedRevisionUnits.length} 项，其余内容沿用当前版本。` : `${selectedRevisionUnits.length} selected; other content carries forward.`)
                                    : shotReview
                                        ? (isZh ? '必须选择失败镜头；已通过镜头不会被重新提交或计费。' : 'A failed shot is required; passed shots will not be resubmitted or re-billed.')
                                        : (isZh ? '不选择则按修改说明更新整份交付。' : 'Leave empty to revise the whole deliverable according to the instruction.')}
                            </small>
                        </fieldset>
                    )}
                    <div className="deliverable-review-card__actions">
                        <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={acting !== null}
                            onClick={() => setRevisionOpen(false)}
                        >
                            {isZh ? '取消' : 'Cancel'}
                        </button>
                        <button
                            type="button"
                            className="btn btn-primary"
                            disabled={acting !== null || revisionInstruction.trim().length < 3 || (shotReview && selectedRevisionUnits.length === 0)}
                            onClick={() => void submitRevision()}
                        >
                            {acting === 'request_changes'
                                ? (isZh ? '正在创建新版本…' : 'Creating revision…')
                                : failedRequest
                                    ? (isZh ? '创建重试版本' : 'Create retry revision')
                                    : (isZh ? '创建修订版本' : 'Create revision')}
                        </button>
                    </div>
                </section>
            )}
            {awaitingReview && (
                <div className="deliverable-review-card__actions">
                    <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={acting !== null}
                        onClick={toggleRevisionForm}
                    >
                        {revisionOpen
                            ? (isZh ? '收起修改说明' : 'Hide revision form')
                            : (isZh ? '提出修改' : 'Request changes')}
                    </button>
                    {!approvalBlocked && (
                        <button
                            type="button"
                            className="btn btn-primary"
                            disabled={acting !== null}
                            onClick={() => void approveDelivery()}
                        >
                            {acting === 'approve' ? (isZh ? '正在确认…' : 'Confirming…') : (isZh ? '确认交付' : 'Confirm delivery')}
                        </button>
                    )}
                </div>
            )}
            {storyboardReview && (
                <div className="deliverable-review-card__actions">
                    <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={acting !== null}
                        onClick={toggleRevisionForm}
                    >
                        {revisionOpen
                            ? (isZh ? '收起修改说明' : 'Hide revision form')
                            : (isZh ? '提出修改' : 'Request changes')}
                    </button>
                    <button
                        type="button"
                        className="btn btn-primary"
                        disabled={acting !== null}
                        onClick={() => void approveStoryboard()}
                    >
                        {acting === 'approve'
                            ? (isZh ? '正在批准…' : 'Approving…')
                            : (isZh ? '批准分镜（下一步发送消息）' : 'Approve storyboard (send next)')}
                    </button>
                </div>
            )}
            {outlineReview && (
                <div className="deliverable-review-card__actions">
                    <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={acting !== null}
                        onClick={toggleRevisionForm}
                    >
                        {revisionOpen
                            ? (isZh ? '收起修改说明' : 'Hide revision form')
                            : (isZh ? '提出修改' : 'Request changes')}
                    </button>
                    <button
                        type="button"
                        className="btn btn-primary"
                        disabled={acting !== null}
                        onClick={() => void approveOutline()}
                    >
                        {acting === 'approve'
                            ? (isZh ? '正在批准…' : 'Approving…')
                            : (isZh ? '批准大纲（下一步发送消息）' : 'Approve outline (send next)')}
                    </button>
                </div>
            )}
            {shotReview && (
                <div className="deliverable-review-card__actions">
                    <button
                        type="button"
                        className="btn btn-primary"
                        disabled={acting !== null || failedShotKeys.length === 0}
                        onClick={toggleRevisionForm}
                    >
                        {revisionOpen
                            ? (isZh ? '收起修改说明' : 'Hide revision form')
                            : (isZh ? '重做失败镜头' : 'Redo failed shots')}
                    </button>
                </div>
            )}
            {failedRequest && (
                <div className="deliverable-review-card__actions">
                    <button
                        type="button"
                        className="btn btn-primary"
                        disabled={acting !== null}
                        onClick={toggleRevisionForm}
                    >
                        {revisionOpen
                            ? (isZh ? '收起重试说明' : 'Hide retry form')
                            : (isZh ? '重新生成' : 'Regenerate')}
                    </button>
                </div>
            )}
            {(storyboardApproved || composeReady) && (
                <div className="deliverable-review-card__actions">
                    <small>
                        {storyboardApproved
                            ? (isZh ? '分镜已批准，发送聊天消息即可开始逐镜头制作。' : 'Storyboard approved; send a chat message to start per-shot production.')
                            : (isZh ? '全部镜头已完成，发送聊天消息继续合成成片。' : 'All shots are complete; send a chat message to assemble the final video.')}
                    </small>
                </div>
            )}
            {outlineApproved && (
                <div className="deliverable-review-card__actions">
                    <small>
                        {isZh
                            ? '大纲已批准，发送聊天消息即可开始制作演示文稿。'
                            : 'Outline approved; send a chat message to start building the deck.'}
                    </small>
                </div>
            )}
        </section>
                </div>
            </section>
        </div>,
        document.body,
    ) : null;

    return (
        <>
            <section className="deliverable-summary-card" data-status={request.status} aria-live="polite">
                <span className="deliverable-summary-card__icon">
                    {request.status === 'running'
                        ? <IconLoader2 className="deliverable-spin" size={18} />
                        : (WORK_TYPE_ICONS[request.work_type] || <IconSparkles size={18} />)}
                </span>
                <div className="deliverable-summary-card__body">
                    <strong>{compactTitle}</strong>
                    <small>{compactDescription}</small>
                </div>
                <div className="deliverable-summary-card__actions">
                    {artifacts.slice(0, 2).map((artifact) => (
                        <a
                            key={artifact.id}
                            href={deliverableApi.artifactDownloadUrl(artifact.id, { inline: artifact.artifact_type === 'pdf' })}
                            target={artifact.artifact_type === 'pdf' ? '_blank' : undefined}
                            rel={artifact.artifact_type === 'pdf' ? 'noreferrer' : undefined}
                            download={artifact.artifact_type === 'pdf' ? undefined : true}
                        >
                            <IconDownload size={14} />
                            {artifactActionLabel(artifact.artifact_type, isZh)}
                        </a>
                    ))}
                    <button
                        ref={detailsTriggerRef}
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => setDetailsOpen(true)}
                    >
                        {isZh ? '查看交付详情' : 'View delivery details'}
                        <IconChevronRight size={15} />
                    </button>
                </div>
            </section>
            {detailsDrawer}
        </>
    );
}
