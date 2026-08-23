import { useEffect, useMemo, useState } from 'react';
import {
    IconArrowLeft,
    IconArrowRight,
    IconCheck,
    IconEye,
    IconLoader2,
    IconLock,
    IconShieldCheck,
} from '@tabler/icons-react';
import { useNavigate, useParams } from 'react-router';
import { useTranslation } from 'react-i18next';

import { useToast } from '../components/Toast/ToastProvider';
import {
    deliverableApi,
    type DeliverableQualityReview,
} from '../services/api';
import { createRandomUUID } from '../utils/randomUUID';


type GateDraft = Record<string, { passed: boolean | null; evidence: string }>;
type DimensionDraft = Record<string, { score: string; evidence: string }>;
type EvidenceDraft = Record<string, {
    status: 'complete' | 'partial' | 'unavailable';
    findings: string;
}>;

const HUMAN_EVIDENCE = new Set([
    'human_visual',
    'human_audio',
    'human_av_sync',
    'document_semantic',
]);

const LABELS_ZH: Record<string, string> = {
    artifact_decodable: '文件可正常解码',
    aspect_ratio_match: '画幅符合确认要求',
    fact_safety: '事实与产品属性安全',
    reference_identity_when_required: '参考主体身份一致',
    no_unrequested_watermark: '无未要求的平台水印',
    duration_and_aspect_match: '时长与画幅符合要求',
    audio_contract_match: '音频模式符合合同',
    pptx_and_preview_valid: 'PPTX 与预览文件有效',
    page_count_and_aspect_match: '页数与画幅符合要求',
    no_text_overflow: '无文字溢出或截断',
    source_traceability: '关键事实与数字可追溯',
    editability: '可编辑性与承诺一致',
    brief_adherence: '需求遵循',
    visual_hierarchy: '视觉层级',
    subject_quality: '主体质量',
    brand_and_style_fit: '品牌与风格匹配',
    commercial_readiness: '商用就绪度',
    story_and_pacing: '故事与节奏',
    character_and_motion_consistency: '人物与运动连续性',
    audio_visual_coherence: '音画一致性',
    narrative_quality: '叙事质量',
    information_design: '信息设计',
    visual_system_consistency: '视觉系统一致性',
    human_visual: '人工视觉检查',
    human_audio: '人工听音检查',
    human_av_sync: '人工口型/音画同步检查',
    document_semantic: '文档事实与语义检查',
    ocr: '图片 OCR 证据',
    frame_ocr: '视频逐帧 OCR 证据',
};

function labelFor(key: string, isZh: boolean) {
    if (isZh && LABELS_ZH[key]) return LABELS_ZH[key];
    return key.split('_').join(' ');
}

function createDrafts(review: DeliverableQualityReview) {
    const gates = Object.fromEntries(
        review.hard_gates.map((key) => [key, { passed: null, evidence: '' }]),
    ) as GateDraft;
    const dimensions = Object.fromEntries(
        review.quality_dimensions.map((key) => [key, { score: '', evidence: '' }]),
    ) as DimensionDraft;
    const evidence = Object.fromEntries(
        review.required_evidence_kinds
            .filter((key) => HUMAN_EVIDENCE.has(key))
            .map((key) => [key, { status: 'complete', findings: '' }]),
    ) as EvidenceDraft;
    return { gates, dimensions, evidence };
}

export default function QualityReview() {
    const { reviewId } = useParams();
    const navigate = useNavigate();
    const { i18n } = useTranslation();
    const isZh = i18n.language?.startsWith('zh');
    const toast = useToast();
    const [review, setReview] = useState<DeliverableQualityReview | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [gates, setGates] = useState<GateDraft>({});
    const [dimensions, setDimensions] = useState<DimensionDraft>({});
    const [humanEvidence, setHumanEvidence] = useState<EvidenceDraft>({});
    const [notes, setNotes] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [clientSubmissionId] = useState(createRandomUUID);
    const [evidenceKind, setEvidenceKind] = useState<'ocr' | 'frame_ocr'>('ocr');
    const [evidenceSource, setEvidenceSource] = useState('');
    const [evidenceFindings, setEvidenceFindings] = useState('');
    const [addingEvidence, setAddingEvidence] = useState(false);
    const [step, setStep] = useState<0 | 1 | 2>(0);

    useEffect(() => {
        if (!reviewId) return;
        let active = true;
        setLoading(true);
        deliverableApi.qualityReview(reviewId)
            .then((result) => {
                if (!active) return;
                setReview(result);
                const drafts = createDrafts(result);
                setGates(drafts.gates);
                setDimensions(drafts.dimensions);
                setHumanEvidence(drafts.evidence);
                const automatedKind = result.required_evidence_kinds.find(
                    (item) => item === 'ocr' || item === 'frame_ocr',
                );
                if (automatedKind) setEvidenceKind(automatedKind);
                setError('');
            })
            .catch((nextError) => {
                if (!active) return;
                setError(nextError instanceof Error ? nextError.message : String(nextError));
            })
            .finally(() => {
                if (active) setLoading(false);
            });
        return () => { active = false; };
    }, [reviewId]);

    const submissionComplete = useMemo(() => (
        review?.current_user_can_submit
        && Object.values(gates).every((item) => item.passed !== null && item.evidence.trim().length >= 3)
        && Object.values(dimensions).every((item) => Number(item.score) >= 1 && Number(item.score) <= 5 && item.evidence.trim().length >= 3)
        && Object.values(humanEvidence).every((item) => item.findings.trim().length >= 3)
    ), [dimensions, gates, humanEvidence, review?.current_user_can_submit]);
    const completedGateCount = Object.values(gates).filter(
        (item) => item.passed !== null && item.evidence.trim().length >= 3,
    ).length;
    const gatesComplete = completedGateCount === Object.keys(gates).length;
    const completedScoreCount = Object.values(dimensions).filter(
        (item) => Number(item.score) >= 1
            && Number(item.score) <= 5
            && item.evidence.trim().length >= 3,
    ).length;
    const completedEvidenceCount = Object.values(humanEvidence).filter(
        (item) => item.findings.trim().length >= 3,
    ).length;

    const submitReview = async () => {
        if (!review || !submissionComplete) return;
        setSubmitting(true);
        try {
            const updated = await deliverableApi.submitQualityReview(review.id, {
                client_submission_id: clientSubmissionId,
                expected_version: review.version,
                hard_gates: Object.fromEntries(
                    Object.entries(gates).map(([key, item]) => [
                        key,
                        { passed: item.passed as boolean, evidence: [item.evidence.trim()] },
                    ]),
                ),
                dimensions: Object.fromEntries(
                    Object.entries(dimensions).map(([key, item]) => [
                        key,
                        { score: Number(item.score), evidence: [item.evidence.trim()] },
                    ]),
                ),
                human_evidence: Object.fromEntries(
                    Object.entries(humanEvidence).map(([key, item]) => [
                        key,
                        { status: item.status, findings: [item.findings.trim()] },
                    ]),
                ),
                notes: notes.trim() ? [notes.trim()] : [],
            });
            setReview(updated);
            toast.success(
                isZh ? '你的独立评审已封存，不能再次修改' : 'Your independent review is sealed',
            );
        } catch (nextError) {
            const message = nextError instanceof Error ? nextError.message : String(nextError);
            setError(message);
            toast.error(isZh ? '评审提交失败' : 'Review submission failed', { details: message });
        } finally {
            setSubmitting(false);
        }
    };

    const addEvidence = async () => {
        if (!review || evidenceSource.trim().length < 3) return;
        setAddingEvidence(true);
        try {
            const updated = await deliverableApi.addQualityReviewEvidence(review.id, {
                client_evidence_id: createRandomUUID(),
                expected_version: review.version,
                kind: evidenceKind,
                status: 'complete',
                source_ref: evidenceSource.trim(),
                findings: evidenceFindings.split('\n').map((item) => item.trim()).filter(Boolean),
            });
            setReview(updated);
            setEvidenceSource('');
            setEvidenceFindings('');
            toast.success(isZh ? '受管证据已绑定到当前 Artifact hash' : 'Managed evidence bound to artifact hashes');
        } catch (nextError) {
            const message = nextError instanceof Error ? nextError.message : String(nextError);
            setError(message);
            toast.error(isZh ? '证据写入失败' : 'Evidence submission failed', { details: message });
        } finally {
            setAddingEvidence(false);
        }
    };

    if (loading) {
        return (
            <main className="quality-review-page quality-review-page--loading">
                <IconLoader2 className="deliverable-spin" size={24} />
                <span>{isZh ? '正在打开质量检查…' : 'Opening quality review…'}</span>
            </main>
        );
    }
    if (!review) {
        return (
            <main className="quality-review-page">
                <button type="button" className="btn btn-secondary" onClick={() => navigate(-1)}>
                    <IconArrowLeft size={16} />
                    {isZh ? '返回' : 'Back'}
                </button>
                <div className="quality-review-page__error">{error || (isZh ? '评审不存在或无权访问' : 'Review not found or inaccessible')}</div>
            </main>
        );
    }

    return (
        <main className="quality-review-page">
            <header className="quality-review-page__header">
                <button type="button" className="deliverable-icon-button" onClick={() => navigate(-1)} aria-label={isZh ? '返回' : 'Back'}>
                    <IconArrowLeft size={18} />
                </button>
                <div>
                    <span><IconShieldCheck size={18} /> {isZh ? '独立质量检查' : 'Independent quality review'}</span>
                    <h1>{isZh ? '检查这份交付物' : 'Review this delivery'}</h1>
                    <p>
                        {review.brief}
                    </p>
                </div>
            </header>

            {error && <div className="quality-review-page__error">{error}</div>}

            <section className="quality-review-page__summary">
                <div>
                    <strong>
                        {review.status === 'passed'
                            ? (isZh ? '质量检查已通过' : 'Quality review passed')
                            : review.status === 'blocked'
                                ? (isZh ? '检查发现需要修改的问题' : 'Changes are required')
                                : (isZh ? '质量检查进行中' : 'Quality review in progress')}
                    </strong>
                    <small>
                        {isZh
                            ? `${review.submitted_reviewer_count}/${review.assigned_reviewer_count} 位评审人已完成`
                            : `${review.submitted_reviewer_count}/${review.assigned_reviewer_count} reviewers completed`}
                    </small>
                </div>
                <div className="quality-review-page__progress">
                    {review.assignments.map((assignment) => (
                        <span key={assignment.reviewer_user_id} data-status={assignment.status}>
                            {assignment.reviewer_display_name || (assignment.is_current_user ? (isZh ? '你' : 'You') : (isZh ? '评审人' : 'Reviewer'))}
                            {' · '}
                            {assignment.status === 'submitted'
                                ? (isZh ? '已完成' : 'Completed')
                                : (isZh ? '待检查' : 'Pending')}
                        </span>
                    ))}
                </div>
            </section>

            {review.current_user_can_submit && (
                <>
                    <nav className="quality-review-page__steps" aria-label={isZh ? '检查步骤' : 'Review steps'}>
                        {[
                            isZh ? '查看文件' : 'Review files',
                            isZh ? '逐项检查' : 'Checklist',
                            isZh ? '评分并提交' : 'Score and submit',
                        ].map((label, index) => (
                            <button
                                key={label}
                                type="button"
                                data-state={index < step ? 'complete' : index === step ? 'current' : 'upcoming'}
                                disabled={(index === 1 && step === 0) || (index === 2 && !gatesComplete)}
                                onClick={() => {
                                    if (index === 0 || (index === 1 && step >= 1) || (index === 2 && gatesComplete)) {
                                        setStep(index as 0 | 1 | 2);
                                    }
                                }}
                            >
                                <span>{index < step ? <IconCheck size={14} /> : index + 1}</span>
                                {label}
                            </button>
                        ))}
                    </nav>

                    {step === 0 && (
                        <section className="quality-review-page__card">
                            <span className="quality-review-page__eyebrow">{isZh ? '第 1 步，共 3 步' : 'Step 1 of 3'}</span>
                            <h2>{isZh ? '先打开并查看全部文件' : 'Open and review every file'}</h2>
                            <p>{isZh ? '请核对内容、版式和任务要求。你的检查只针对当前显示的这一版文件。' : 'Check the content, layout, and requirements. Your review applies only to this file version.'}</p>
                            <div className="quality-review-page__artifacts">
                                {review.artifacts.map((artifact) => (
                                    <a key={artifact.id} href={artifact.download_url} target="_blank" rel="noreferrer">
                                        <IconEye size={16} />
                                        <span>
                                            {artifact.artifact_type === 'pdf'
                                                ? (isZh ? '在线预览 PDF' : 'Preview PDF')
                                                : `${isZh ? '打开' : 'Open'} ${artifact.artifact_type.toUpperCase()}`}
                                        </span>
                                        <small>{isZh ? `第 ${artifact.revision_number} 版` : `Version ${artifact.revision_number}`}</small>
                                    </a>
                                ))}
                            </div>
                            <div className="quality-review-page__requirements">
                                <h3>{isZh ? '本次需要满足' : 'Requirements for this delivery'}</h3>
                                <ul>{review.requirements.map((item) => <li key={item}>{item}</li>)}</ul>
                            </div>
                            <button type="button" className="btn btn-primary" onClick={() => setStep(1)}>
                                {isZh ? '我已查看文件，开始检查' : 'I reviewed the files'}
                                <IconArrowRight size={16} />
                            </button>
                        </section>
                    )}

                    {step === 1 && (
                        <section className="quality-review-page__card">
                            <span className="quality-review-page__eyebrow">{isZh ? '第 2 步，共 3 步' : 'Step 2 of 3'}</span>
                            <h2>{isZh ? '逐项确认是否符合要求' : 'Check each requirement'}</h2>
                            <p>
                                {isZh
                                    ? `已完成 ${completedGateCount}/${review.hard_gates.length} 项。每项都要选择结果，并写下你看到的具体情况。`
                                    : `${completedGateCount}/${review.hard_gates.length} complete. Choose a result and record what you observed.`}
                            </p>
                            <div className="quality-review-page__grid">
                                {review.hard_gates.map((key) => (
                                    <fieldset key={key}>
                                        <legend>{labelFor(key, isZh)}</legend>
                                        <div className="quality-review-page__choice">
                                            <label data-selected={gates[key]?.passed === true}>
                                                <input type="radio" name={`gate-${key}`} checked={gates[key]?.passed === true} onChange={() => setGates((current) => ({ ...current, [key]: { ...current[key], passed: true } }))} />
                                                {isZh ? '符合要求' : 'Meets requirement'}
                                            </label>
                                            <label data-selected={gates[key]?.passed === false}>
                                                <input type="radio" name={`gate-${key}`} checked={gates[key]?.passed === false} onChange={() => setGates((current) => ({ ...current, [key]: { ...current[key], passed: false } }))} />
                                                {isZh ? '发现问题' : 'Issue found'}
                                            </label>
                                        </div>
                                        <textarea value={gates[key]?.evidence || ''} onChange={(event) => setGates((current) => ({ ...current, [key]: { ...current[key], evidence: event.target.value } }))} placeholder={isZh ? '例如：第 3 页右侧文字被截断' : 'Example: text is clipped on the right side of page 3'} />
                                    </fieldset>
                                ))}
                            </div>
                            <div className="quality-review-page__actions">
                                <button type="button" className="btn btn-secondary" onClick={() => setStep(0)}>
                                    <IconArrowLeft size={16} />
                                    {isZh ? '返回查看文件' : 'Back to files'}
                                </button>
                                <button type="button" className="btn btn-primary" disabled={!gatesComplete} onClick={() => setStep(2)}>
                                    {isZh ? '下一步：评分' : 'Next: score'}
                                    <IconArrowRight size={16} />
                                </button>
                            </div>
                            {!gatesComplete && (
                                <small>{isZh ? '完成全部检查项后才能进入下一步。' : 'Complete every check before continuing.'}</small>
                            )}
                        </section>
                    )}

                    {step === 2 && (
                        <section className="quality-review-page__card">
                            <span className="quality-review-page__eyebrow">{isZh ? '第 3 步，共 3 步' : 'Step 3 of 3'}</span>
                            <h2>{isZh ? '评价整体质量并提交' : 'Score the overall quality and submit'}</h2>
                            <p>
                                {isZh
                                    ? `质量评分 ${completedScoreCount}/${review.quality_dimensions.length}，专项检查 ${completedEvidenceCount}/${Object.keys(humanEvidence).length}。`
                                    : `${completedScoreCount}/${review.quality_dimensions.length} scores and ${completedEvidenceCount}/${Object.keys(humanEvidence).length} specialist checks complete.`}
                            </p>
                            <div className="quality-review-page__grid">
                                {review.quality_dimensions.map((key) => (
                                    <fieldset key={key}>
                                        <legend>{labelFor(key, isZh)}</legend>
                                        <select value={dimensions[key]?.score || ''} onChange={(event) => setDimensions((current) => ({ ...current, [key]: { ...current[key], score: event.target.value } }))}>
                                            <option value="">{isZh ? '请选择评分' : 'Select a score'}</option>
                                            <option value="1">{isZh ? '1 · 无法使用' : '1 · Unusable'}</option>
                                            <option value="2">{isZh ? '2 · 需要较多修改' : '2 · Major changes needed'}</option>
                                            <option value="3">{isZh ? '3 · 基本可用' : '3 · Acceptable'}</option>
                                            <option value="4">{isZh ? '4 · 良好' : '4 · Good'}</option>
                                            <option value="5">{isZh ? '5 · 可直接使用' : '5 · Ready to use'}</option>
                                        </select>
                                        <textarea value={dimensions[key]?.evidence || ''} onChange={(event) => setDimensions((current) => ({ ...current, [key]: { ...current[key], evidence: event.target.value } }))} placeholder={isZh ? '用具体观察解释这个评分' : 'Explain the score with a concrete observation'} />
                                    </fieldset>
                                ))}
                                {Object.keys(humanEvidence).map((key) => (
                                    <fieldset key={key}>
                                        <legend>{labelFor(key, isZh)}</legend>
                                        <select value={humanEvidence[key].status} onChange={(event) => setHumanEvidence((current) => ({ ...current, [key]: { ...current[key], status: event.target.value as EvidenceDraft[string]['status'] } }))}>
                                            <option value="complete">{isZh ? '已完整检查' : 'Completed'}</option>
                                            <option value="partial">{isZh ? '只完成部分检查' : 'Partially checked'}</option>
                                            <option value="unavailable">{isZh ? '当前无法检查' : 'Unable to check'}</option>
                                        </select>
                                        <textarea value={humanEvidence[key].findings} onChange={(event) => setHumanEvidence((current) => ({ ...current, [key]: { ...current[key], findings: event.target.value } }))} placeholder={isZh ? '记录你看到或听到的具体结论' : 'Record what you saw or heard'} />
                                    </fieldset>
                                ))}
                            </div>
                            <label className="quality-review-page__notes">
                                <span>{isZh ? '给任务负责人的补充说明（可选）' : 'Additional note for the task owner (optional)'}</span>
                                <textarea value={notes} onChange={(event) => setNotes(event.target.value)} />
                            </label>
                            <div className="quality-review-page__actions">
                                <button type="button" className="btn btn-secondary" onClick={() => setStep(1)}>
                                    <IconArrowLeft size={16} />
                                    {isZh ? '返回检查项' : 'Back to checklist'}
                                </button>
                                <button type="button" className="btn btn-primary" disabled={!submissionComplete || submitting} onClick={() => void submitReview()}>
                                    {submitting ? <IconLoader2 className="deliverable-spin" size={16} /> : <IconLock size={16} />}
                                    {isZh ? '提交我的检查结果' : 'Submit my review'}
                                </button>
                            </div>
                            <small>{isZh ? '提交后不能修改。系统会与其他评审人的结果自动汇总。' : 'You cannot edit after submission. The system combines all reviewers automatically.'}</small>
                        </section>
                    )}
                </>
            )}

            {!review.current_user_can_submit && (
                <section className="quality-review-page__card">
                    <h2>
                        {review.assignments.some((assignment) => assignment.is_current_user && assignment.status === 'submitted')
                            ? (isZh ? '你的检查已提交' : 'Your review is submitted')
                            : review.status === 'open'
                                ? (isZh ? '当前不需要你填写检查' : 'No review action is assigned to you')
                                : (isZh ? '本轮质量检查已结束' : 'This quality review is complete')}
                    </h2>
                    <p>{isZh ? '这里会自动更新其他评审人的完成进度。' : 'Progress updates automatically as other reviewers finish.'}</p>
                </section>
            )}

            {review.current_user_can_add_evidence && review.status === 'open' && review.required_evidence_kinds.some((item) => item === 'ocr' || item === 'frame_ocr') && (
                <details className="quality-review-page__admin">
                    <summary>{isZh ? '管理员工具：添加自动检查结果' : 'Admin tools: add automated evidence'}</summary>
                    <div>
                        <p>{isZh ? '仅管理员使用。自动检查可以发现问题，但不能代替三位评审人的独立判断。' : 'Admin only. Automated checks can flag issues but cannot replace independent reviewers.'}</p>
                        <select value={evidenceKind} onChange={(event) => setEvidenceKind(event.target.value as 'ocr' | 'frame_ocr')}>
                            {review.required_evidence_kinds.filter((item) => item === 'ocr' || item === 'frame_ocr').map((item) => <option key={item} value={item}>{labelFor(item, isZh)}</option>)}
                        </select>
                        <input value={evidenceSource} onChange={(event) => setEvidenceSource(event.target.value)} placeholder={isZh ? '内部证据文件引用' : 'Internal evidence reference'} />
                        <textarea value={evidenceFindings} onChange={(event) => setEvidenceFindings(event.target.value)} placeholder={isZh ? '每行记录一个检查结果' : 'One finding per line'} />
                        <button type="button" className="btn btn-secondary" disabled={addingEvidence || evidenceSource.trim().length < 3} onClick={() => void addEvidence()}>
                            {addingEvidence ? <IconLoader2 className="deliverable-spin" size={16} /> : <IconShieldCheck size={16} />}
                            {isZh ? '保存自动检查结果' : 'Save automated evidence'}
                        </button>
                    </div>
                </details>
            )}

            {(review.current_user_can_manage || review.receipt_ref) && (
                <details className="quality-review-page__technical">
                    <summary>{isZh ? '技术审计信息' : 'Technical audit details'}</summary>
                    <div>
                        <p>{isZh ? '以下信息用于管理员审计，不影响日常检查操作。' : 'These details are for administrators and do not affect the review workflow.'}</p>
                        <ul>
                            {review.artifacts.map((artifact) => (
                                <li key={artifact.id}>
                                    <code>{artifact.artifact_type.toUpperCase()} · v{artifact.revision_number} · {artifact.content_hash}</code>
                                </li>
                            ))}
                        </ul>
                        {review.receipt_ref && <code>{review.receipt_ref}</code>}
                    </div>
                </details>
            )}
        </main>
    );
}
