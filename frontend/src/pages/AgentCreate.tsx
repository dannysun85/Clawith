import { useState, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { IconEye, IconSettings, IconTools } from '@tabler/icons-react';
import { agentApi, channelApi, skillApi, tenantApi } from '../services/api';
import { useAllowedTiers } from '../hooks/useLlmModels';
import {
    SUBSCRIPTION_UPGRADE_PATH,
    agentLimitMessage,
    useAgentCreationLimit,
} from '../hooks/useAgentCreationLimit';
import TierSelector, { type SaasTier } from '../components/TierSelector';
import ChannelConfig from '../components/ChannelConfig';
import LinearCopyButton from '../components/LinearCopyButton';
import {
    buildAgentChannelSetups,
    findIncompleteAgentChannels,
    shouldConfigureAgentChannels,
} from '../utils/agentChannelSetup';
import { buildOpenClawInstruction } from '../utils/openClawInstruction';
const STEPS = ['basicInfo', 'personality', 'skills', 'permissions', 'channel'] as const;
const OPENCLAW_STEPS = ['basicInfo', 'permissions'] as const;

export default function AgentCreate() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const location = useLocation();
    const queryClient = useQueryClient();
    const [step, setStep] = useState(0);
    const [error, setError] = useState('');
    const [upgradeUrl, setUpgradeUrl] = useState('');
    const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
    const [agentType, setAgentType] = useState<'native' | 'openclaw'>(() => {
        const params = new URLSearchParams(location.search);
        return params.get('type') === 'openclaw' ? 'openclaw' : 'native';
    });
    // Clear field error when user edits a field
    const clearFieldError = (field: string) => setFieldErrors(prev => { const n = { ...prev }; delete n[field]; return n; });
    const [createdApiKey, setCreatedApiKey] = useState('');
    // Current company (tenant) selection from layout sidebar
    const [currentTenant] = useState<string | null>(() => localStorage.getItem('current_tenant_id'));

    const [form, setForm] = useState({
        name: '',
        role_description: '',
        personality: '',
        boundaries: '',
        preferred_tier: 'lite' as SaasTier,
        preferred_modality: 'text',
        permission_scope_type: 'company',
        permission_access_level: 'use',
        max_tokens_per_day: '',
        max_tokens_per_month: '',
        skill_ids: [] as string[],
    });
    const [channelValues, setChannelValues] = useState<Record<string, string>>({});

    // Allowed tiers from tenant subscription entitlements
    const allowedTiers = useAllowedTiers();
    const agentCreationLimit = useAgentCreationLimit();
    const isChinese = i18n.language?.startsWith('zh') || false;

    useEffect(() => {
        if (!allowedTiers.length) return;
        if (!allowedTiers.includes(form.preferred_tier)) {
            setForm(prev => ({
                ...prev,
                preferred_tier: (allowedTiers[0] || 'lite') as SaasTier,
            }));
        }
    }, [allowedTiers, form.preferred_tier]);

    // Fetch global skills for step 3
    const { data: globalSkills = [] } = useQuery({
        queryKey: ['global-skills'],
        queryFn: skillApi.list,
    });

    // Auto-select default skills
    useEffect(() => {
        if (globalSkills.length > 0) {
            const defaultIds = globalSkills.filter((s: any) => s.is_default).map((s: any) => s.id);
            if (defaultIds.length > 0) {
                setForm(prev => ({
                    ...prev,
                    skill_ids: Array.from(new Set([...prev.skill_ids, ...defaultIds]))
                }));
            }
        }
    }, [globalSkills]);

    const createMutation = useMutation({
        mutationFn: async (data: any) => {
            const agent = await agentApi.create(data);
            return agent;
        },
        onSuccess: async (agent, variables) => {
            queryClient.invalidateQueries({ queryKey: ['agents'] });
            queryClient.invalidateQueries({ queryKey: ['subscription-seats'] });

            // Bind every completed channel form through its provider endpoint.
            // `/channel` is Feishu-only and must never receive another provider.
            const channelSetupFailures: string[] = [];
            const channelSetups = shouldConfigureAgentChannels(variables.agent_type)
                ? buildAgentChannelSetups(channelValues)
                : [];
            for (const setup of channelSetups) {
                try {
                    await channelApi.configure(agent.id, setup.endpoint, setup.payload);
                } catch (err) {
                    console.error(`Failed to bind ${setup.channel} channel:`, err);
                    channelSetupFailures.push(setup.channel);
                }
            }

            if (channelSetupFailures.length > 0) {
                navigate(`/agents/${agent.id}/settings#settings`, {
                    state: { channelSetupFailures },
                });
                return;
            }

            if (agent.api_key) {
                setCreatedApiKey(agent.api_key);
            } else {
                navigate(`/agents/${agent.id}`);
            }
        },
        onError: (err: any) => {
            setError(err.message);
            setUpgradeUrl(err?.detail?.details?.upgrade_url || err?.detail?.upgrade_url || (err?.status === 402 ? SUBSCRIPTION_UPGRADE_PATH : ''));
        },
    });

    const validateStep0 = (): boolean => {
        const errors: Record<string, string> = {};
        const name = form.name.trim();
        if (!name) {
            errors.name = t('wizard.errors.nameRequired', '智能体名称不能为空');
        } else if (name.length < 2) {
            errors.name = t('wizard.errors.nameTooShort', '名称至少需要 2 个字符');
        } else if (name.length > 100) {
            errors.name = t('wizard.errors.nameTooLong', '名称不能超过 100 个字符');
        }
        if (form.role_description.length > 500) {
            errors.role_description = t('wizard.errors.roleDescTooLong', '角色描述不能超过 500 个字符（当前 {{count}} 字符）').replace('{{count}}', String(form.role_description.length));
        }
        if (form.max_tokens_per_day && (isNaN(Number(form.max_tokens_per_day)) || Number(form.max_tokens_per_day) <= 0)) {
            errors.max_tokens_per_day = t('wizard.errors.tokenLimitInvalid', '请输入有效的正整数');
        }
        if (form.max_tokens_per_month && (isNaN(Number(form.max_tokens_per_month)) || Number(form.max_tokens_per_month) <= 0)) {
            errors.max_tokens_per_month = t('wizard.errors.tokenLimitInvalid', '请输入有效的正整数');
        }
        if (agentType === 'native' && !form.preferred_tier) {
            errors.preferred_tier = t('wizard.errors.tierRequired', '请选择一个模型档位');
        }
        setFieldErrors(errors);
        return Object.keys(errors).length === 0;
    };

    const handleNext = () => {
        setError('');
        setUpgradeUrl('');
        if (step === 0 && !validateStep0()) return;
        setStep(step + 1);
    };

    const handleFinish = () => {
        setError('');
        setUpgradeUrl('');
        if (step === 0 || agentType === 'openclaw') {
            if (!validateStep0()) return;
        }
        const incompleteChannels = shouldConfigureAgentChannels(agentType)
            ? findIncompleteAgentChannels(channelValues)
            : [];
        if (incompleteChannels.length > 0) {
            setError(
                `Complete or clear the partially configured channels before creating the Agent: ${incompleteChannels.join(', ')}`,
            );
            return;
        }
        createMutation.mutate({
            name: form.name,
            agent_type: agentType,
            role_description: form.role_description,
            personality: agentType === 'native' ? form.personality : undefined,
            boundaries: agentType === 'native' ? form.boundaries : undefined,
            preferred_tier: agentType === 'native' ? form.preferred_tier : undefined,
            preferred_modality: agentType === 'native' ? form.preferred_modality : undefined,
            permission_scope_type: form.permission_scope_type,
            max_tokens_per_day: form.max_tokens_per_day ? Number(form.max_tokens_per_day) : undefined,
            max_tokens_per_month: form.max_tokens_per_month ? Number(form.max_tokens_per_month) : undefined,
            skill_ids: agentType === 'native' ? form.skill_ids : [],
            permission_access_level: form.permission_access_level,
            tenant_id: currentTenant || undefined,
        });
    };

    const activeSteps = agentType === 'openclaw' ? OPENCLAW_STEPS : STEPS;

    const upgradeButton = upgradeUrl ? (
        <button
            type="button"
            className="btn btn-secondary"
            style={{ marginTop: '10px' }}
            onClick={() => navigate(upgradeUrl)}
        >
            {t('subscription.goToDetail', isChinese ? '去套餐详情' : 'Go to subscription')}
        </button>
    ) : null;

    // If OpenClaw agent just created, show success page with API key
    if (createdApiKey && createMutation.data) {
        const agent = createMutation.data;
        const setupInstruction = buildOpenClawInstruction(createdApiKey, !!i18n.language?.startsWith('zh'));
        return (
            <div>
                <div className="page-header">
                    <h1 className="page-title">{t('openclaw.created', 'OpenClaw Agent Created')}</h1>
                </div>
                <div className="card" style={{ maxWidth: '640px' }}>
                    <div style={{ textAlign: 'center', padding: '20px 0' }}>
                        <div style={{ fontSize: '32px', marginBottom: '12px' }}>&#x2713;</div>
                        <h3 style={{ fontWeight: 600, marginBottom: '8px' }}>{agent.name}</h3>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '24px' }}>
                            {t('openclaw.createdDesc2', 'Your OpenClaw agent has been registered. Copy the instruction below and send it to your OpenClaw agent to complete the setup.')}
                        </p>
                    </div>

                    {/* Setup Instruction — single block to send to OpenClaw */}
                    <div style={{ marginBottom: '20px' }}>
                        <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '6px', color: 'var(--text-secondary)' }}>
                            {t('openclaw.setupInstruction', 'Setup Instruction')}
                        </label>
                        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                            {t('openclaw.setupInstructionDesc', 'Copy and send this to your OpenClaw agent. It will configure itself automatically.')}
                        </p>
                        <div style={{ position: 'relative' }}>
                            <pre style={{
                                padding: '12px', background: 'var(--bg-secondary)', borderRadius: '6px',
                                fontSize: '11px', lineHeight: 1.6, overflow: 'auto', maxHeight: '280px',
                                border: '1px solid var(--border-default)', whiteSpace: 'pre-wrap',
                            }}>{setupInstruction}</pre>
                                    <LinearCopyButton
                                        className="btn btn-ghost"
                                        style={{ position: 'absolute', top: '4px', right: '4px', fontSize: '11px', minWidth: '60px' }}
                                        textToCopy={setupInstruction}
                                        label={t('common.copy', 'Copy')}
                                        copiedLabel="Copied"
                                    />
                                </div>
                    </div>

                    {/* API Key — collapsed by default */}
                    <details style={{ marginBottom: '24px' }}>
                        <summary style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', cursor: 'pointer', userSelect: 'none' }}>
                            API Key
                        </summary>
                        <div style={{ marginTop: '8px' }}>
                            <div style={{ display: 'flex', gap: '8px' }}>
                                <code style={{
                                    flex: 1, padding: '10px 12px', background: 'var(--bg-secondary)', borderRadius: '6px',
                                    fontSize: '13px', fontFamily: 'monospace', wordBreak: 'break-all',
                                    border: '1px solid var(--border-default)',
                                }}>{createdApiKey}</code>
                                <LinearCopyButton
                                    className="btn btn-secondary"
                                    style={{ fontSize: '11px', padding: '4px 12px', minWidth: '70px', height: 'fit-content' }}
                                    textToCopy={createdApiKey}
                                    label={t('common.copy', 'Copy')}
                                    copiedLabel="Copied"
                                />
                            </div>
                            <p style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '6px' }}>
                                {t('openclaw.keyNote', 'This key is already embedded in the instruction above. Save it separately if needed for manual configuration.')}
                            </p>
                        </div>
                    </details>

                    <button className="btn btn-primary" style={{ width: '100%' }} onClick={() => navigate(`/agents/${agent.id}`)}>
                        {t('openclaw.goToAgent', 'Go to Agent Page')}
                    </button>
                </div>
            </div>
        );
    }

    if (!agentCreationLimit.isLoading && agentCreationLimit.isLimited) {
        const limitText = agentLimitMessage(
            isChinese,
            agentCreationLimit.activeCount,
            agentCreationLimit.maxAgents,
        );
        return (
            <div>
                <div className="page-header">
                    <h1 className="page-title">{t('nav.newAgent')}</h1>
                </div>
                <div className="card" style={{ maxWidth: '640px' }}>
                    <h3 style={{ margin: '0 0 8px', fontWeight: 600, fontSize: '16px' }}>
                        {t('agent.limit.title', isChinese ? '智能体数量已达上限' : 'Agent limit reached')}
                    </h3>
                    <p style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.6, margin: '0 0 20px' }}>
                        {limitText}
                    </p>
                    <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => navigate(SUBSCRIPTION_UPGRADE_PATH)}
                    >
                        {t('subscription.goToDetail', isChinese ? '去套餐详情' : 'Go to subscription')}
                    </button>
                </div>
            </div>
        );
    }

    // ── Type Selector (shared between both modes) ──
    const typeSelector = (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', maxWidth: '640px', marginBottom: '24px' }}>
            <div
                onClick={() => { setAgentType('native'); setStep(0); }}
                style={{
                    padding: '16px', borderRadius: '8px', cursor: 'pointer',
                    border: `1.5px solid ${agentType === 'native' ? 'var(--accent-primary)' : 'var(--border-default)'}`,
                    background: agentType === 'native' ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                }}
            >
                <div style={{ fontWeight: 600, fontSize: '14px', marginBottom: '4px' }}>{t('openclaw.nativeTitle', 'Platform Hosted')}</div>
                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('openclaw.nativeDesc', 'Full agent running on Astra platform')}</div>
            </div>
            <div
                onClick={() => {
                    setAgentType('openclaw');
                    setStep(0);
                    setChannelValues({});
                }}
                style={{
                    padding: '16px', borderRadius: '8px', cursor: 'pointer', position: 'relative',
                    border: `1.5px solid ${agentType === 'openclaw' ? 'var(--accent-primary)' : 'var(--border-default)'}`,
                    background: agentType === 'openclaw' ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                }}
            >
                <span style={{
                    position: 'absolute', top: '8px', right: '8px',
                    fontSize: '10px', padding: '2px 6px', borderRadius: '4px',
                    background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', fontWeight: 600,
                    letterSpacing: '0.5px',
                }}>Lab</span>
                <div style={{ fontWeight: 600, fontSize: '14px', marginBottom: '4px' }}>{t('openclaw.openclawTitle', 'Link OpenClaw')}</div>
                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('openclaw.openclawDesc', 'Connect your existing OpenClaw agent')}</div>
            </div>
        </div>
    );

    // ── OpenClaw mode: completely separate page ──
    if (agentType === 'openclaw') {
        return (
            <div>
                <div className="page-header">
                    <h1 className="page-title">{t('nav.newAgent')}</h1>
                </div>

                {typeSelector}

                {error && (
                    <div style={{ background: 'var(--error-subtle)', color: 'var(--error)', padding: '8px 12px', borderRadius: '6px', fontSize: '13px', marginBottom: '16px', maxWidth: '640px' }}>
                        {error}
                        {upgradeButton}
                    </div>
                )}

                <div className="card" style={{ maxWidth: '640px' }}>
                    <h3 style={{ marginBottom: '6px', fontWeight: 600, fontSize: '15px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                        {t('openclaw.basicTitle', 'Link OpenClaw Agent')}
                        <span style={{
                            fontSize: '10px', padding: '2px 6px', borderRadius: '4px',
                            background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', fontWeight: 600,
                        }}>Lab</span>
                    </h3>
                    <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '20px' }}>
                        {t('openclaw.basicDesc', 'Give your OpenClaw agent a name and description. The LLM model, personality, and skills are configured on your OpenClaw instance.')}
                    </p>

                    <div className="form-group">
                        <label className="form-label">{t('agent.fields.name')} *</label>
                        <input className={`form-input${fieldErrors.name ? ' input-error' : ''}`} value={form.name}
                            onChange={(e) => { setForm({ ...form, name: e.target.value }); clearFieldError('name'); }}
                            placeholder={t('openclaw.namePlaceholder', 'e.g. My OpenClaw Bot')} autoFocus />
                        {fieldErrors.name && <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '4px' }}>{fieldErrors.name}</div>}
                    </div>
                    <div className="form-group">
                        <label className="form-label">{t('agent.fields.role')}</label>
                        <input className={`form-input${fieldErrors.role_description ? ' input-error' : ''}`} value={form.role_description}
                            onChange={(e) => { setForm({ ...form, role_description: e.target.value }); clearFieldError('role_description'); }}
                            placeholder={t('openclaw.rolePlaceholder', 'e.g. Personal assistant running on my Mac')} />
                        {fieldErrors.role_description && <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '4px' }}>{fieldErrors.role_description}</div>}
                    </div>

                    {/* Permissions */}
                    <div className="form-group" style={{ marginTop: '8px' }}>
                        <label className="form-label">{t('wizard.step4.title')}</label>
                        <div style={{ display: 'flex', gap: '8px' }}>
                            {[
                                { value: 'company', label: t('wizard.step4.companyWide'), desc: t('wizard.step4.companyWideDesc') },
                                { value: 'user', label: t('wizard.step4.selfOnly'), desc: t('wizard.step4.selfOnlyDesc') },
                                { value: 'custom', label: t('agent.settings.perm.custom', 'Custom'), desc: t('agent.settings.perm.customDesc', 'Only selected members and agents can see and use it. Plaza is disabled') },
                            ].map((scope) => (
                                <label key={scope.value} style={{
                                    flex: 1, display: 'flex', alignItems: 'center', gap: '10px', padding: '12px',
                                    background: form.permission_scope_type === scope.value ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                                    border: `1px solid ${form.permission_scope_type === scope.value ? 'var(--accent-primary)' : 'var(--border-default)'}`,
                                    borderRadius: '8px', cursor: 'pointer',
                                }}>
                                    <input type="radio" name="scope" checked={form.permission_scope_type === scope.value}
                                        onChange={() => setForm({ ...form, permission_scope_type: scope.value })} />
                                    <div>
                                        <div style={{ fontWeight: 500, fontSize: '13px' }}>{scope.label}</div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{scope.desc}</div>
                                    </div>
                                </label>
                            ))}
                        </div>
                    </div>

                    {/* Actions */}
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '24px' }}>
                        <button className="btn btn-secondary" onClick={() => navigate('/')}>{t('common.cancel')}</button>
                        <button className="btn btn-primary" onClick={handleFinish}
                            disabled={createMutation.isPending}>
                            {createMutation.isPending ? t('common.loading') : t('openclaw.createBtn', 'Link Agent')}
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    // ── Native mode: original multi-step wizard ──
    return (
        <div>
            <div className="page-header">
                <h1 className="page-title">{t('nav.newAgent')}</h1>
            </div>

            {typeSelector}

            {/* Stepper */}
            <div className="wizard-steps">
                {STEPS.map((s, i) => (
                    <div key={s} style={{ display: 'contents' }}>
                        <div className={`wizard-step ${i === step ? 'active' : i < step ? 'completed' : ''}`}>
                            <div className="wizard-step-number">{i < step ? '\u2713' : i + 1}</div>
                            <span>{t(`wizard.steps.${s}`)}</span>
                        </div>
                        {i < STEPS.length - 1 && <div className="wizard-connector" />}
                    </div>
                ))}
            </div>

            {/* Removed top navigation, moved to bottom */}

            {error && (
                <div style={{ background: 'var(--error-subtle)', color: 'var(--error)', padding: '8px 12px', borderRadius: '6px', fontSize: '13px', marginBottom: '16px' }}>
                    {error}
                    {upgradeButton}
                </div>
            )}

            <div className="card" style={{ maxWidth: '640px' }}>
                {/* Step 1: Basic Info + Model */}
                {step === 0 && (
                    <div>
                        <h3 style={{ marginBottom: '20px', fontWeight: 600, fontSize: '15px' }}>{t('wizard.step1.title')}</h3>

                        <div className="form-group">
                            <label className="form-label">{t('agent.fields.name')} <span style={{ color: 'var(--error)' }}>*</span></label>
                            <input className={`form-input${fieldErrors.name ? ' input-error' : ''}`} value={form.name}
                                onChange={(e) => { setForm({ ...form, name: e.target.value }); clearFieldError('name'); }}
                                placeholder={t("wizard.step1.namePlaceholder")} autoFocus />
                            {fieldErrors.name && <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '4px' }}>{fieldErrors.name}</div>}
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('agent.fields.role')}</label>
                            <input className={`form-input${fieldErrors.role_description ? ' input-error' : ''}`} value={form.role_description}
                                onChange={(e) => { setForm({ ...form, role_description: e.target.value }); clearFieldError('role_description'); }}
                                placeholder={t('wizard.roleHint')} />
                            {fieldErrors.role_description && <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '4px' }}>{fieldErrors.role_description}</div>}
                        </div>

                        {/* Model Tier Selection */}
                        <div className="form-group">
                            <label className="form-label">{t('wizard.step1.modelTier', '模型档位')} <span style={{ color: 'var(--error)' }}>*</span></label>
                            <TierSelector
                                value={form.preferred_tier}
                                onChange={(tier) => { setForm({ ...form, preferred_tier: tier }); clearFieldError('preferred_tier'); }}
                                allowedTiers={allowedTiers}
                            />
                            {fieldErrors.preferred_tier && <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '6px' }}>{fieldErrors.preferred_tier}</div>}
                        </div>

                        {/* Token limits */}
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                            <div className="form-group">
                                <label className="form-label">{t('wizard.step1.dailyTokenLimit')}</label>
                                <input className={`form-input${fieldErrors.max_tokens_per_day ? ' input-error' : ''}`} type="number" value={form.max_tokens_per_day}
                                    onChange={(e) => { setForm({ ...form, max_tokens_per_day: e.target.value }); clearFieldError('max_tokens_per_day'); }}
                                    placeholder={t("wizard.step1.unlimited")} />
                                {fieldErrors.max_tokens_per_day && <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '4px' }}>{fieldErrors.max_tokens_per_day}</div>}
                            </div>
                            <div className="form-group">
                                <label className="form-label">{t('wizard.step1.monthlyTokenLimit')}</label>
                                <input className={`form-input${fieldErrors.max_tokens_per_month ? ' input-error' : ''}`} type="number" value={form.max_tokens_per_month}
                                    onChange={(e) => { setForm({ ...form, max_tokens_per_month: e.target.value }); clearFieldError('max_tokens_per_month'); }}
                                    placeholder={t("wizard.step1.unlimited")} />
                                {fieldErrors.max_tokens_per_month && <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '4px' }}>{fieldErrors.max_tokens_per_month}</div>}
                            </div>
                        </div>
                    </div>
                )}

                {/* Step 2: Personality */}
                {step === 1 && (
                    <div>
                        <h3 style={{ marginBottom: '20px', fontWeight: 600, fontSize: '15px' }}>{t('wizard.step2.title')}</h3>
                        <div className="form-group">
                            <label className="form-label">{t('agent.fields.personality')}</label>
                            <textarea className="form-textarea" rows={4} value={form.personality}
                                onChange={(e) => setForm({ ...form, personality: e.target.value })}
                                placeholder={t("wizard.step2.personalityPlaceholder")} />
                        </div>
                        <div className="form-group">
                            <label className="form-label">{t('agent.fields.boundaries')}</label>
                            <textarea className="form-textarea" rows={4} value={form.boundaries}
                                onChange={(e) => setForm({ ...form, boundaries: e.target.value })}
                                placeholder={t("wizard.step2.boundariesPlaceholder")} />
                        </div>
                    </div>
                )}

                {/* Step 3: Skills */}
                {step === 2 && (
                    <div>
                        <h3 style={{ marginBottom: '20px', fontWeight: 600, fontSize: '15px' }}>{t('wizard.step3.title')}</h3>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '16px' }}>
                            {t('wizard.step3.description')}
                        </p>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                            {globalSkills.map((skill: any) => {
                                const isDefault = skill.is_default;
                                const isChecked = form.skill_ids.includes(skill.id);
                                return (
                                    <label key={skill.id} style={{
                                        display: 'flex', alignItems: 'center', gap: '12px', padding: '12px',
                                        background: isChecked ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                                        border: `1px solid ${isChecked ? 'var(--accent-primary)' : 'var(--border-default)'}`,
                                        borderRadius: '8px', cursor: isDefault ? 'default' : 'pointer',
                                        opacity: isDefault ? 0.85 : 1,
                                    }}>
                                        <input type="checkbox"
                                            checked={isChecked}
                                            disabled={isDefault}
                                            onChange={(e) => {
                                                if (isDefault) return;
                                                if (e.target.checked) {
                                                    setForm({ ...form, skill_ids: [...form.skill_ids, skill.id] });
                                                } else {
                                                    setForm({ ...form, skill_ids: form.skill_ids.filter((id: string) => id !== skill.id) });
                                                }
                                            }}
                                        />
                                        <div style={{ fontSize: '18px', display: 'flex', color: 'var(--text-tertiary)' }}>
                                            {/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test(skill.icon || '') ? <IconTools size={18} stroke={1.8} /> : (skill.icon || <IconTools size={18} stroke={1.8} />)}
                                        </div>
                                        <div style={{ flex: 1 }}>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                                <span style={{ fontWeight: 500, fontSize: '13px' }}>{skill.name}</span>
                                                {isDefault && <span style={{ fontSize: '10px', padding: '1px 6px', borderRadius: '4px', background: 'var(--accent-primary)', color: '#fff', fontWeight: 500 }}>{t('wizard.step3.required')}</span>}
                                            </div>
                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{skill.description}</div>
                                        </div>
                                    </label>);
                            })}
                            {globalSkills.length === 0 && (
                                <div style={{ padding: '16px', background: 'var(--bg-elevated)', borderRadius: '8px', fontSize: '13px', color: 'var(--text-tertiary)', textAlign: 'center' }}>
                                    {t('wizard.step3.noSkills')}
                                </div>
                            )}
                        </div>
                    </div>
                )}

                {/* Step 4: Permissions */}
                {step === 3 && (
                    <div>
                        <h3 style={{ marginBottom: '20px', fontWeight: 600, fontSize: '15px' }}>{t('wizard.step4.title')}</h3>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '20px' }}>
                            {[
                                { value: 'company', label: t('wizard.step4.companyWide'), desc: t('wizard.step4.companyWideDesc') },
                                { value: 'user', label: t('wizard.step4.selfOnly'), desc: t('wizard.step4.selfOnlyDesc') },
                                { value: 'custom', label: t('agent.settings.perm.custom', 'Custom'), desc: t('agent.settings.perm.customDesc', 'Only selected members and agents can see and use it. Plaza is disabled') },
                            ].map((scope) => (
                                <label key={scope.value} style={{
                                    display: 'flex', alignItems: 'center', gap: '12px', padding: '14px',
                                    background: form.permission_scope_type === scope.value ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                                    border: `1px solid ${form.permission_scope_type === scope.value ? 'var(--accent-primary)' : 'var(--border-default)'}`,
                                    borderRadius: '8px', cursor: 'pointer',
                                }}>
                                    <input type="radio" name="scope" checked={form.permission_scope_type === scope.value}
                                        onChange={() => setForm({ ...form, permission_scope_type: scope.value })} />

                                    <div>
                                        <div style={{ fontWeight: 500, fontSize: '13px' }}>{scope.label}</div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{scope.desc}</div>
                                    </div>
                                </label>
                            ))}
                        </div>

                        {/* Access Level — only for company scope */}
                        {form.permission_scope_type === 'company' && (
                            <div>
                                <label style={{ display: 'block', fontSize: '13px', fontWeight: 600, marginBottom: '10px' }}>
                                    {t('wizard.step4.accessLevel', 'Default Access Level')}
                                </label>
                                <div style={{ display: 'flex', gap: '8px' }}>
                                    {[
                                        { value: 'use', icon: <IconEye size={14} stroke={1.8} />, label: t('wizard.step4.useLevel', 'Use'), desc: t('wizard.step4.useDesc', 'Can use Task, Chat, Tools, Skills, Workspace') },
                                        { value: 'manage', icon: <IconSettings size={14} stroke={1.8} />, label: t('wizard.step4.manageLevel', 'Manage'), desc: t('wizard.step4.manageDesc', 'Full access including Settings, Mind, and Directory') },
                                    ].map((lvl) => (
                                        <label key={lvl.value} style={{
                                            flex: 1, display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '12px',
                                            background: form.permission_access_level === lvl.value ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                                            border: `1px solid ${form.permission_access_level === lvl.value ? 'var(--accent-primary)' : 'var(--border-default)'}`,
                                            borderRadius: '8px', cursor: 'pointer',
                                        }}>
                                            <input type="radio" name="access_level" checked={form.permission_access_level === lvl.value}
                                                onChange={() => setForm({ ...form, permission_access_level: lvl.value })} style={{ marginTop: '2px' }} />
                                            <div>
                                                <div style={{ fontWeight: 500, fontSize: '13px', display: 'flex', alignItems: 'center', gap: '6px' }}>{lvl.icon} {lvl.label}</div>
                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{lvl.desc}</div>
                                            </div>
                                        </label>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                )}

                {/* Step 5: Channel */}
                {step === 4 && (
                    <div>
                        <h3 style={{ marginBottom: '20px', fontWeight: 600, fontSize: '15px' }}>{t('wizard.step5.title', 'Channel Configuration')}</h3>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '16px' }}>
                            {t('wizard.step5.description', 'Connect messaging platforms to enable your agent to communicate through different channels.')}
                        </p>

                        <ChannelConfig mode="create" values={channelValues} onChange={setChannelValues} />

                        {Object.keys(channelValues).length === 0 && (
                            <div style={{ padding: '12px', background: 'var(--bg-secondary)', borderRadius: '8px', fontSize: '12px', color: 'var(--text-tertiary)', textAlign: 'center', marginTop: '12px' }}>
                                {t('wizard.step5.skipHint')}
                            </div>
                        )}
                    </div>
                )}


            </div>

            {/* Summary sidebar */}
            <div style={{ marginTop: '16px', padding: '12px', background: 'var(--bg-elevated)', borderRadius: '8px', fontSize: '12px', color: 'var(--text-secondary)', maxWidth: '640px', marginBottom: '80px' }}>
                <strong>{form.name || t('wizard.summary.unnamed')}</strong>
                {agentType === 'native' && (
                    <>
                        {' · '}
                        {t('wizard.summary.tier', '档位')}: {t(`tier.${form.preferred_tier}`, form.preferred_tier)}
                    </>
                )}
                {form.max_tokens_per_day && ` · ${t('wizard.summary.dailyLimit')}: ${Number(form.max_tokens_per_day).toLocaleString()}`}
            </div>

            {/* Navigation — sticky footer at the bottom */}
            <div style={{
                position: 'fixed', bottom: 0, left: 'var(--sidebar-width)', right: 0,
                background: 'var(--bg-primary)', borderTop: '1px solid var(--border-subtle)',
                padding: '16px 32px', zIndex: 100,
                display: 'flex', justifyContent: 'flex-start',
                transition: 'left var(--transition-default)'
            }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', maxWidth: '640px' }}>
                    <button className="btn btn-secondary" onClick={() => step > 0 ? setStep(step - 1) : navigate('/')}
                        disabled={createMutation.isPending}>
                        {step === 0 ? t('common.cancel') : t('wizard.prev')}
                    </button>
                    {step < STEPS.length - 1 ? (
                        <button className="btn btn-primary" onClick={handleNext}>
                            {t('wizard.next')} →
                        </button>
                    ) : (
                        <button className="btn btn-primary" onClick={handleFinish}
                            disabled={createMutation.isPending}>
                            {createMutation.isPending ? t('common.loading') : t('wizard.finish')}
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
