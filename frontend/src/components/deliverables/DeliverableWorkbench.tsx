import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
    IconAlertTriangle,
    IconCheck,
    IconChevronRight,
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
    type DeliverableRequest,
    type DeliverableWorkflow,
    type DeliverableWorkType,
} from '../../services/api';
import type { SaasTier } from '../TierSelector';


export interface DeliverableAttachmentInput {
    name: string;
    path?: string;
}

interface DeliverableLauncherProps {
    agentId: string;
    sessionId?: string;
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

const REASON_LABELS: Record<string, { zh: string; en: string }> = {
    no_route: { zh: '当前档位缺少可用的理解模型', en: 'No understanding route is available for this tier' },
    model_tier: { zh: '当前套餐不包含此档位', en: 'This tier is not included in the current plan' },
    model_modality: { zh: '当前套餐不包含所需能力', en: 'The required capability is not included in the current plan' },
    presentation_tool_unavailable: { zh: '该数字员工尚未启用 PPT 转换能力', en: 'This Agent does not have presentation conversion enabled' },
    plan_denied: { zh: '当前套餐尚未开放此生成能力', en: 'The current plan does not include this generation capability' },
    agent_tool_disabled: { zh: '该数字员工尚未启用所需工具', en: 'The required Agent tool is disabled' },
    pool_unavailable: { zh: '平台生成能力暂时不可用', en: 'The platform generation capability is temporarily unavailable' },
    media_capability_unavailable: { zh: '媒体生成能力暂时不可用', en: 'Media generation is temporarily unavailable' },
    workflow_execution_not_enabled: { zh: '当前阶段只保存工作说明，不会启动生成', en: 'This phase saves the brief without starting generation' },
};

function workflowLabel(workflow: DeliverableWorkflow, isZh: boolean) {
    return isZh ? workflow.label_zh : workflow.label_en;
}

function initialSpec(workflow?: DeliverableWorkflow): Record<string, string | number> {
    if (!workflow) return {};
    return Object.fromEntries(
        workflow.fields
            .filter((field) => field.default !== null && field.default !== undefined)
            .map((field) => [field.key, field.default as string | number]),
    );
}

function creditText(preflight: DeliverablePreflight, isZh: boolean) {
    const estimate = preflight.credit_estimate;
    if (estimate.mode === 'usage_based' || estimate.minimum === null) {
        return isZh ? '按实际用量结算' : 'Settled from actual usage';
    }
    if (estimate.maximum === estimate.minimum) {
        return isZh ? `预计 ${estimate.minimum} Credits` : `Estimated ${estimate.minimum} Credits`;
    }
    return isZh
        ? `预计 ${estimate.minimum}–${estimate.maximum} Credits`
        : `Estimated ${estimate.minimum}–${estimate.maximum} Credits`;
}

export function DeliverableLauncher({
    agentId,
    sessionId,
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
    const [selectedType, setSelectedType] = useState<DeliverableWorkType>('presentation');
    const [goal, setGoal] = useState('');
    const [spec, setSpec] = useState<Record<string, string | number>>({});
    const [preflight, setPreflight] = useState<DeliverablePreflight | null>(null);
    const [checking, setChecking] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [clientRequestId, setClientRequestId] = useState(() => crypto.randomUUID());

    const selectedWorkflow = useMemo(
        () => workflows.find((workflow) => workflow.work_type === selectedType),
        [selectedType, workflows],
    );

    useEffect(() => {
        if (!open || workflows.length > 0) return;
        let active = true;
        setLoadingWorkflows(true);
        deliverableApi.workflows()
            .then((response) => {
                if (!active) return;
                setLoadingWorkflows(false);
                setWorkflows(response.workflows);
                const presentation = response.workflows.find((item) => item.work_type === 'presentation');
                setSpec(initialSpec(presentation || response.workflows[0]));
            })
            .catch((nextError) => {
                if (!active) return;
                setLoadingWorkflows(false);
                const message = nextError instanceof Error ? nextError.message : String(nextError);
                setError(message);
                toast.error(isZh ? '无法加载交付物工作流' : 'Could not load deliverable workflows', { details: message });
            });
        return () => { active = false; };
    }, [isZh, open, toast, workflows.length]);

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
        setPreflight(null);
        setError('');
        setClientRequestId(crypto.randomUUID());
    };

    const updateField = (key: string, value: string | number) => {
        setSpec((current) => ({ ...current, [key]: value }));
        setPreflight(null);
        setError('');
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
            setPreflight(result);
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
                result.launchable
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
                        <h2 id="deliverable-drawer-title">{isZh ? '确认工作说明' : 'Confirm the work brief'}</h2>
                        <p>{isZh ? '先保存结构化需求，再由后端检查能力、路由和费用。' : 'Save a structured request before capability, routing, and cost checks.'}</p>
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
                    <section className="deliverable-section" aria-labelledby="deliverable-type-heading">
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
                    </section>

                    {selectedWorkflow && (
                        <section className="deliverable-section" aria-labelledby="deliverable-brief-heading">
                            <div className="deliverable-section__heading">
                                <span className="deliverable-step">2</span>
                                <div>
                                    <h3 id="deliverable-brief-heading">{isZh ? '填写工作说明' : 'Complete the brief'}</h3>
                                    <p>{isZh ? `当前档位：${tier.toUpperCase()}` : `Current tier: ${tier.toUpperCase()}`}</p>
                                </div>
                            </div>
                            <div className="deliverable-form">
                                <label className="deliverable-field deliverable-field--full">
                                    <span>{isZh ? '交付目标' : 'Outcome'} <em>*</em></span>
                                    <textarea
                                        value={goal}
                                        onChange={(event) => { setGoal(event.target.value); setError(''); }}
                                        placeholder={isZh ? '例如：为潜在投资人制作一份 8 页融资汇报' : 'e.g. Create an 8-slide fundraising deck for prospective investors'}
                                        rows={3}
                                    />
                                </label>
                                {selectedWorkflow.fields.map((field) => {
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
                                                    {field.options.map((option) => <option key={option} value={option}>{option}</option>)}
                                                </select>
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
                                })}
                            </div>
                            <div className="deliverable-input-summary">
                                <span>{isZh ? '已附加资料' : 'Attached references'}</span>
                                <strong>{attachments.filter((item) => item.path).length}</strong>
                                <small>{isZh ? '仅保存 workspace 引用，不复制文件内容。' : 'Only workspace references are saved; file contents are not copied.'}</small>
                            </div>
                        </section>
                    )}

                    <section className="deliverable-section" aria-labelledby="deliverable-preflight-heading">
                        <div className="deliverable-section__heading">
                            <span className="deliverable-step">3</span>
                            <div>
                                <h3 id="deliverable-preflight-heading">{isZh ? '能力与费用预检' : 'Capability and cost preflight'}</h3>
                                <p>{isZh ? '预检和保存不会预留或扣除 Credits。' : 'Preflight and saving do not reserve or spend Credits.'}</p>
                            </div>
                        </div>
                        {preflight ? (
                            <div className={`deliverable-preflight ${preflight.available ? 'is-ready' : 'is-blocked'}`} aria-live="polite">
                                <span className="deliverable-preflight__icon">
                                    {preflight.available ? <IconCheck size={18} /> : <IconAlertTriangle size={18} />}
                                </span>
                                <div>
                                    <strong>{preflight.available ? (isZh ? '基础能力可用' : 'Base capability available') : (isZh ? '暂时无法使用' : 'Currently unavailable')}</strong>
                                    <p>{creditText(preflight, isZh)}</p>
                                    {preflight.reasons.map((reason) => (
                                        <small key={reason}>{(REASON_LABELS[reason]?.[isZh ? 'zh' : 'en']) || reason}</small>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <button type="button" className="btn btn-secondary deliverable-preflight-button" disabled={checking || !selectedWorkflow} onClick={() => void checkCapability()}>
                                {checking ? (isZh ? '正在检查…' : 'Checking…') : (isZh ? '检查当前配置' : 'Check current setup')}
                            </button>
                        )}
                        {error && <div className="deliverable-error" role="alert">{error}</div>}
                    </section>
                </div>

                <footer className="deliverable-drawer__footer">
                    <div>
                        <strong>{isZh ? '不会直接调用具体模型' : 'No direct model invocation'}</strong>
                        <small>{isZh ? '请求由平台统一路由，并在真实执行时按实际用量结算。' : 'The platform routes execution and settles actual usage.'}</small>
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

    return (
        <>
            <button
                ref={triggerRef}
                type="button"
                className="chat-composer-btn deliverable-launcher"
                onClick={() => setOpen(true)}
                disabled={disabled || !sessionId}
                aria-label={isZh ? '打开交付物工作台' : 'Open deliverable workbench'}
                title={isZh ? 'PPT、海报和短视频工作说明' : 'Presentation, poster, and video briefs'}
            >
                <IconSparkles size={16} stroke={1.75} />
                <span>{isZh ? '交付物' : 'Create'}</span>
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


export function DeliverableReviewCard({ request, onUpdated }: DeliverableReviewCardProps) {
    const { i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const toast = useToast();
    const [acting, setActing] = useState<'approve' | 'request_changes' | null>(null);
    const artifacts = latestArtifacts(request);
    const awaitingReview = request.status === 'waiting_approval' && request.current_stage === 'output_review';
    const statusText = (() => {
        if (request.status === 'running') return isZh ? '正在生成并校验交付文件' : 'Generating and validating deliverables';
        if (awaitingReview) return isZh ? '文件已通过结构校验，请确认交付' : 'Files passed structural validation and await approval';
        if (request.status === 'succeeded') return isZh ? '已批准并完成交付' : 'Approved and delivered';
        if (request.status === 'failed') return isZh ? '交付失败，需要重新检查' : 'Delivery failed and needs review';
        return isZh ? '正在处理交付请求' : 'Processing deliverable request';
    })();

    const applyAction = async (action: 'approve' | 'request_changes') => {
        setActing(action);
        try {
            const updated = await deliverableApi.action(request.id, action, request.version);
            onUpdated(updated);
            toast.success(
                action === 'approve'
                    ? (isZh ? '交付文件已批准' : 'Deliverable files approved')
                    : (isZh ? '已退回；请重新创建工作说明后再次生成' : 'Returned; create a new brief before regenerating'),
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            toast.error(isZh ? '交付操作失败' : 'Deliverable action failed', { details: message });
        } finally {
            setActing(null);
        }
    };

    return (
        <section className="deliverable-review-card" data-status={request.status} aria-live="polite">
            <div className="deliverable-review-card__header">
                <span className="deliverable-review-card__icon">
                    {request.status === 'running' ? <IconLoader2 className="deliverable-spin" size={18} /> : <IconFileTypePpt size={18} />}
                </span>
                <div>
                    <strong>{isZh ? 'PPT 交付任务' : 'Presentation delivery'}</strong>
                    <small>{statusText}</small>
                </div>
                <em>{request.tier.toUpperCase()}</em>
            </div>
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
                            <span>{artifact.artifact_type.toUpperCase()}</span>
                            <small>v{artifact.revision_number} · {artifact.status}</small>
                        </a>
                    ))}
                </div>
            )}
            {request.status === 'failed' && request.last_error_code && (
                <div className="deliverable-review-card__error">{request.last_error_code}</div>
            )}
            {awaitingReview && (
                <div className="deliverable-review-card__actions">
                    <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={acting !== null}
                        onClick={() => void applyAction('request_changes')}
                    >
                        {acting === 'request_changes' ? (isZh ? '正在退回…' : 'Returning…') : (isZh ? '退回重做' : 'Request changes')}
                    </button>
                    <button
                        type="button"
                        className="btn btn-primary"
                        disabled={acting !== null}
                        onClick={() => void applyAction('approve')}
                    >
                        {acting === 'approve' ? (isZh ? '正在批准…' : 'Approving…') : (isZh ? '批准交付' : 'Approve delivery')}
                    </button>
                </div>
            )}
        </section>
    );
}
