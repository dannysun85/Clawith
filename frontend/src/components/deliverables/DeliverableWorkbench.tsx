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
    type DeliverablePreflight,
    type DeliverableQualityReview,
    type DeliverableQualityReviewer,
    type DeliverableRequest,
    type DeliverableWorkflow,
    type DeliverableWorkType,
} from '../../services/api';
import {
    deliverableApprovalBlocked,
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
    };
    const label = labels[reason];
    return label ? label[isZh ? 0 : 1] : reason;
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
        setClientRequestId(crypto.randomUUID());
    };

    const updateField = (key: string, value: string | number) => {
        setSpec((current) => ({ ...current, [key]: value }));
        setError('');
    };

    const renderWorkflowField = (field: DeliverableWorkflow['fields'][number]) => {
        const label = isZh ? field.label_zh : field.label_en;
        const placeholder = isZh ? field.placeholder_zh : field.placeholder_en;
        const value = spec[field.key] ?? '';
        if (field.kind === 'textarea') {
            return (
                <label key={field.key} className="deliverable-field deliverable-field--full">
                    <span>{label} {field.required && <em>*</em>}</span>
                    <textarea value={value} onChange={(event) => updateField(field.key, event.target.value)} placeholder={placeholder} rows={3} />
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
                spec,
                tier,
            });
            setSpec(result.normalized_spec);
            if (!result.available) {
                const reasonText = result.reasons.length > 0
                    ? result.reasons.map((reason) => preflightReasonLabel(reason, isZh)).join(isZh ? '；' : '; ')
                    : (isZh ? '当前账号没有可用的执行路由' : 'No execution route is available for this account');
                setError(
                    isZh
                        ? `当前账号或档位暂不支持这项交付，未创建任务、未扣 Credits。原因：${reasonText}`
                        : `This account or tier cannot run this delivery. No task was created and no Credits were spent. Reason: ${reasonText}`,
                );
            }
            return result;
        } catch (nextError) {
            const message = nextError instanceof Error ? nextError.message : String(nextError);
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
            if (!result || !result.available) return;
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
            onCreated(request, result.launchable);
            setOpen(false);
            setClientRequestId(crypto.randomUUID());
            toast.success(
                result.capability_status === 'degraded'
                    ? result.launchable
                        ? (isZh ? '已按你确认的应急质量策略保存，可在对话中启动' : 'Saved with your confirmed emergency-quality policy and ready to launch in chat')
                        : (isZh ? '工作说明已保存；当前只有应急质量线路，正式执行将等待主线路' : 'Brief saved; only emergency quality is available, so formal execution will wait for the primary route')
                    : result.launchable
                        ? (isZh ? '工作说明已保存，可在对话中确认启动' : 'Brief saved and ready to launch in chat')
                        : (isZh ? '工作说明已保存；当前阶段不会调用生成服务' : 'Brief saved; generation is not started in this phase'),
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
                                        onChange={(event) => { setGoal(event.target.value); setError(''); }}
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

export function DeliverableRequestCard({ request, launchable, onRemove, onOpen }: DeliverableRequestCardProps) {
    const { i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const label = {
        presentation: isZh ? 'PPT 演示文稿' : 'Presentation',
        poster: isZh ? '海报 / 图片' : 'Poster / Image',
        video: isZh ? '短视频' : 'Short video',
        report: isZh ? '报告' : 'Report',
        spreadsheet: isZh ? '表格' : 'Spreadsheet',
    }[request.work_type];

    return (
        <div className="deliverable-request-card" data-status={request.status}>
            <span className="deliverable-request-card__icon">{WORK_TYPE_ICONS[request.work_type] || <IconSparkles size={18} />}</span>
            <button type="button" className="deliverable-request-card__body" onClick={onOpen}>
                <span>
                    <strong>{label}</strong>
                    <small>{launchable ? (isZh ? '工作说明已保存 · 发送后启动' : 'Brief saved · send to launch') : (isZh ? '工作说明已保存 · 暂不启动生成' : 'Brief saved · generation not started')}</small>
                </span>
                <em>{request.tier.toUpperCase()}</em>
            </button>
            <button type="button" className="deliverable-icon-button" onClick={onRemove} aria-label={isZh ? '从本次发送中移除' : 'Remove from this message'}>
                <IconX size={16} stroke={1.75} />
            </button>
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
    const detailsTriggerRef = useRef<HTMLButtonElement>(null);
    const detailsDrawerRef = useRef<HTMLElement>(null);
    const artifacts = latestArtifacts(request);
    const previewArtifact = [...artifacts]
        .sort((left, right) => previewPriority(left.artifact_type) - previewPriority(right.artifact_type))
        .find((artifact) => previewPriority(artifact.artifact_type) < Number.MAX_SAFE_INTEGER);
    const previewArtifactType = previewArtifact?.artifact_type.toLowerCase();
    const previewArtifactUrl = previewArtifact
        ? deliverableApi.artifactDownloadUrl(previewArtifact.id, { inline: true })
        : '';
    const awaitingReview = request.status === 'waiting_approval' && request.current_stage === 'output_review';
    const approvalBlocked = deliverableApprovalBlocked(request);
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
    const presentation = (() => {
        if (request.status === 'running') {
            return {
                title: isZh ? '正在生成交付文件' : 'Creating your deliverables',
                description: isZh ? '完成后可在这里预览和下载' : 'Preview and download the files here when ready',
                step: 0,
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
    const compactTitle = artifacts.length > 0
        ? {
            presentation: isZh ? 'PPT 已生成' : 'Presentation ready',
            poster: isZh ? '图片已生成' : 'Image ready',
            video: isZh ? '视频已生成' : 'Video ready',
            report: isZh ? '报告已生成' : 'Report ready',
            spreadsheet: isZh ? '表格已生成' : 'Spreadsheet ready',
        }[request.work_type]
        : presentation.title;

    const closeDetails = useCallback(() => {
        setDetailsOpen(false);
        window.setTimeout(() => detailsTriggerRef.current?.focus(), 0);
    }, []);

    const applyAction = async (action: 'approve' | 'request_changes') => {
        setActing(action);
        try {
            const updated = await deliverableApi.action(request.id, action, request.version);
            onUpdated(updated);
            toast.success(
                action === 'approve'
                    ? (isZh ? '交付已确认' : 'Delivery confirmed')
                    : (isZh ? '已退回修改，任务内容仍会保留' : 'Returned for changes; the task brief is preserved'),
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            toast.error(isZh ? '交付操作失败' : 'Deliverable action failed', { details: message });
        } finally {
            setActing(null);
        }
    };

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
            {previewArtifact && (
                <section className="deliverable-review-card__preview" aria-label={isZh ? '交付文件预览' : 'Deliverable preview'}>
                    {previewArtifactType === 'mp4' && (
                        <video controls playsInline preload="metadata" src={previewArtifactUrl}>
                            {isZh ? '当前浏览器无法播放此视频。' : 'This browser cannot play the video.'}
                        </video>
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
            {request.status === 'failed' && request.last_error_code && (
                <div className="deliverable-review-card__error">{request.last_error_code}</div>
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
            {awaitingReview && (
                <div className="deliverable-review-card__actions">
                    <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={acting !== null}
                        onClick={() => void applyAction('request_changes')}
                    >
                        {acting === 'request_changes' ? (isZh ? '正在退回…' : 'Returning…') : (isZh ? '退回修改' : 'Request changes')}
                    </button>
                    {!approvalBlocked && (
                        <button
                            type="button"
                            className="btn btn-primary"
                            disabled={acting !== null}
                            onClick={() => void applyAction('approve')}
                        >
                            {acting === 'approve' ? (isZh ? '正在确认…' : 'Confirming…') : (isZh ? '确认交付' : 'Confirm delivery')}
                        </button>
                    )}
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
                    <small>{compactTitle === presentation.title ? presentation.description : presentation.title}</small>
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
