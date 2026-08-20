import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';

import { useDialog } from '../../../components/Dialog/DialogProvider';
import { agentApi, ceoApi, type CeoOrchestratorSettings } from '../../../services/api';
import type { Agent } from '../../../types';

/**
 * One company CEO with observer-by-default and opt-in coordinator authority.
 *
 * Rendered as an independent card on the OKR/company settings tab. It never
 * mixes into the OKR (094) settings semantics: all state comes from the
 * /companies/current/ceo/* endpoints. When the rollout canary does not cover
 * this tenant (feature_available=false) the card renders nothing at all.
 */
export default function CeoCard({ tenantId }: { tenantId: string }) {
    const { i18n } = useTranslation();
    const zh = i18n.language?.startsWith('zh');
    const qc = useQueryClient();
    const dialog = useDialog();
    const navigate = useNavigate();

    const [selectedMemberIds, setSelectedMemberIds] = useState<string[]>([]);
    const [dailyCap, setDailyCap] = useState('20');
    const [monthlyCap, setMonthlyCap] = useState('300');
    const [maxParallel, setMaxParallel] = useState('3');
    const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
    const [saveError, setSaveError] = useState('');
    const saveTimerRef = useRef<number | null>(null);

    const { data: settings, isLoading } = useQuery({
        queryKey: ['ceo-orchestrator-settings', tenantId],
        queryFn: () => ceoApi.settings(),
        retry: false,
    });

    const { data: agents = [] } = useQuery({
        queryKey: ['agents', tenantId],
        queryFn: () => agentApi.list(tenantId || undefined),
        enabled: Boolean(tenantId) && Boolean(settings?.feature_available),
        retry: false,
    });

    useEffect(() => {
        if (!settings) return;
        setSelectedMemberIds(settings.meeting_member_agent_ids ?? []);
        setDailyCap(String(settings.daily_credit_cap ?? 20));
        setMonthlyCap(String(settings.monthly_credit_cap ?? 300));
        setMaxParallel(String(settings.max_parallel_delegations ?? 3));
    }, [settings]);

    useEffect(() => () => {
        if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    }, []);

    const invalidate = () => {
        qc.invalidateQueries({ queryKey: ['ceo-orchestrator-settings'] });
        qc.invalidateQueries({ queryKey: ['workforce-topology'] });
        qc.invalidateQueries({ queryKey: ['agents'] });
    };

    const noteSaved = () => {
        setSaveState('saved');
        if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
        saveTimerRef.current = window.setTimeout(() => {
            setSaveState('idle');
            saveTimerRef.current = null;
        }, 1800);
    };

    const enableMutation = useMutation({
        mutationFn: () => ceoApi.enable({
            member_agent_ids: selectedMemberIds,
            daily_credit_cap: Math.max(0, Number.parseInt(dailyCap, 10) || 0),
            monthly_credit_cap: Math.max(0, Number.parseInt(monthlyCap, 10) || 0),
        }),
        onSuccess: () => {
            invalidate();
            noteSaved();
        },
        onError: (error: unknown) => {
            setSaveState('error');
            setSaveError(error instanceof Error ? error.message : String(error));
        },
    });

    const patchMutation = useMutation({
        mutationFn: (data: Parameters<typeof ceoApi.updateSettings>[0]) => ceoApi.updateSettings(data),
        onSuccess: () => {
            invalidate();
            noteSaved();
        },
        onError: (error: unknown) => {
            setSaveState('error');
            setSaveError(error instanceof Error ? error.message : String(error));
        },
    });

    const disableMutation = useMutation({
        mutationFn: () => ceoApi.disable(),
        onSuccess: () => {
            invalidate();
            noteSaved();
        },
        onError: (error: unknown) => {
            setSaveState('error');
            setSaveError(error instanceof Error ? error.message : String(error));
        },
    });

    if (isLoading) return null;
    // Rollout gate closed → zero entry points.
    if (!settings?.feature_available) return null;

    const employeeAgents = (agents as Agent[]).filter(
        (agent) => !agent.is_system && agent.id !== settings.ceo_agent_id,
    );
    const configured = settings.configured;
    const enabled = settings.enabled;

    const toggleMember = (agentId: string, checked: boolean) => {
        const next = checked
            ? [...selectedMemberIds, agentId]
            : selectedMemberIds.filter((id) => id !== agentId);
        setSelectedMemberIds(next);
        if (configured) {
            setSaveState('saving');
            setSaveError('');
            patchMutation.mutate({ member_agent_ids: next });
        }
    };

    const toggleCadence = (field: 'briefing_enabled' | 'morning_meeting_enabled', value: boolean) => {
        setSaveState('saving');
        setSaveError('');
        patchMutation.mutate({ [field]: value });
    };

    const saveCaps = () => {
        setSaveState('saving');
        setSaveError('');
        patchMutation.mutate({
            daily_credit_cap: Math.max(0, Number.parseInt(dailyCap, 10) || 0),
            monthly_credit_cap: Math.max(0, Number.parseInt(monthlyCap, 10) || 0),
        });
    };

    const saveParallelism = () => {
        const value = Math.min(12, Math.max(1, Number.parseInt(maxParallel, 10) || 3));
        setMaxParallel(String(value));
        setSaveState('saving');
        setSaveError('');
        patchMutation.mutate({ max_parallel_delegations: value });
    };

    const toggleCoordination = async (value: boolean) => {
        if (value) {
            const confirmed = await dialog.confirm(
                zh
                    ? '协调型 CEO 可以在当前人工对话中，根据员工能力目录下发任务。它仍不能替你审批、付款、签约或发布外部内容；所有委派都必须留下运行与交付回执。'
                    : 'Coordinator mode may delegate from a current human chat using Directory capability evidence. It still cannot approve, pay, sign, or publish externally; every delegation must leave Runtime and delivery receipts.',
                {
                    title: zh ? '启用 CEO 协调权限' : 'Enable CEO coordination',
                    confirmLabel: zh ? '确认启用' : 'Enable coordination',
                },
            );
            if (!confirmed) return;
        }
        setSaveState('saving');
        setSaveError('');
        patchMutation.mutate({ coordination_enabled: value });
    };

    const toggleAutoDispatch = async (value: boolean) => {
        if (value) {
            const confirmed = await dialog.confirm(
                zh
                    ? '自动派发会允许 CEO 的非人工对话运行（例如已授权的系统节奏）继续下发任务。默认建议保持关闭，直到人工对话委派已完成验收。'
                    : 'Autonomous dispatch lets authorized non-chat CEO runs delegate work. Keep it off until human-chat delegation has passed acceptance testing.',
                {
                    title: zh ? '启用自动派发' : 'Enable autonomous dispatch',
                    confirmLabel: zh ? '确认启用' : 'Enable autonomous dispatch',
                },
            );
            if (!confirmed) return;
        }
        setSaveState('saving');
        setSaveError('');
        patchMutation.mutate({ auto_dispatch_enabled: value });
    };

    const confirmDisable = async () => {
        const confirmed = await dialog.confirm(
            zh
                ? '停用后 CEO 的简报与晨会节奏将全部关闭；Agent、历史简报与纪要会保留，可随时重新启用。'
                : 'Disabling turns off every CEO cadence. The Agent, past briefings, and minutes are retained and you can re-enable at any time.',
            {
                title: zh ? '停用公司 CEO' : 'Disable company CEO',
                confirmLabel: zh ? '确认停用' : 'Disable',
            },
        );
        if (confirmed) disableMutation.mutate();
    };

    const toggleStyle = (on: boolean, disabledToggle: boolean) => ({
        track: {
            position: 'absolute' as const, top: 0, left: 0, right: 0, bottom: 0, borderRadius: '28px',
            cursor: disabledToggle ? 'not-allowed' : 'pointer',
            background: on ? 'var(--accent-primary)' : 'var(--border-subtle)', transition: '0.2s',
            opacity: disabledToggle ? 0.5 : 1,
        },
        knob: {
            position: 'absolute' as const, left: on ? '26px' : '2px', top: '2px', width: '24px', height: '24px',
            borderRadius: '50%', background: '#fff', transition: '0.2s', boxShadow: '0 1px 3px rgba(0,0,0,0.15)',
        },
    });

    const renderToggle = (on: boolean, disabledToggle: boolean, onChange: (value: boolean) => void, label: string) => (
        <label style={{ position: 'relative', display: 'inline-block', width: '52px', height: '28px', flexShrink: 0 }}>
            <input
                type="checkbox"
                aria-label={label}
                checked={on}
                disabled={disabledToggle}
                onChange={(event) => onChange(event.target.checked)}
                style={{ opacity: 0, width: 0, height: 0 }}
            />
            <span style={toggleStyle(on, disabledToggle).track}>
                <span style={toggleStyle(on, disabledToggle).knob} />
            </span>
        </label>
    );

    return (
        <div className="card" style={{ marginBottom: '24px' }}>
            <div style={{ padding: '20px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px' }}>
                    <div>
                        <div style={{ fontWeight: 600, fontSize: '15px', color: 'var(--text-primary)', marginBottom: '4px' }}>
                            {zh ? '公司 CEO' : 'Company CEO'}
                            <span style={{
                                marginLeft: '8px', padding: '1px 8px', borderRadius: '999px', fontSize: '11px',
                                background: 'rgba(99,102,241,0.12)', color: 'var(--accent-primary)', fontWeight: 500,
                            }}>
                                {settings.operating_mode === 'coordinator_auto'
                                    ? (zh ? '协调型 · 自动派发' : 'Coordinator · autonomous')
                                    : settings.operating_mode === 'coordinator'
                                        ? (zh ? '协调型' : 'Coordinator')
                                        : (zh ? '观察型 · 不占员工席位' : 'Observer · no seat used')}
                            </span>
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.6, maxWidth: '620px' }}>
                            {zh
                                ? 'CEO 默认以观察型运行：汇总业务全景、生成简报并主持晨会。管理员可另行开启协调型权限，让它在人工对话中依据能力目录委派任务；自动派发仍是独立开关。运行消耗租户 Credits。'
                                : 'The CEO defaults to observer mode for panorama, briefings, and meetings. A governor may separately enable coordinator authority for capability-based delegation from human chat; autonomous dispatch remains an independent switch. Runs consume tenant Credits.'}
                        </div>
                    </div>
                    {configured && settings.ceo_agent_id && (
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => navigate(`/agents/${settings.ceo_agent_id}/chat`)}
                        >
                            {zh ? '打开 CEO 详情' : 'Open CEO page'}
                        </button>
                    )}
                </div>

                {saveState !== 'idle' && (
                    <div style={{
                        marginTop: '12px', fontSize: '12px',
                        color: saveState === 'error' ? 'var(--danger, #dc2626)' : saveState === 'saved' ? 'var(--success, #16a34a)' : 'var(--text-tertiary)',
                    }}>
                        {saveState === 'saving' && (zh ? '正在保存 CEO 设置...' : 'Saving CEO settings...')}
                        {saveState === 'saved' && (zh ? 'CEO 设置已保存' : 'CEO settings saved')}
                        {saveState === 'error' && saveError}
                    </div>
                )}

                <div style={{ marginTop: '16px', borderTop: '1px solid var(--border-subtle)', paddingTop: '16px' }}>
                    <div style={{ fontWeight: 500, fontSize: '13px', marginBottom: '8px' }}>
                        {zh ? '会议成员（参与晨会定向询问的员工）' : 'Meeting members (employees asked in meetings)'}
                    </div>
                    {employeeAgents.length === 0 ? (
                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                            {zh ? '当前公司还没有可选择的数字员工。' : 'No digital employees available for selection yet.'}
                        </div>
                    ) : (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 16px' }}>
                            {employeeAgents.map((agent) => (
                                <label key={agent.id} style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '13px', cursor: 'pointer' }}>
                                    <input
                                        type="checkbox"
                                        checked={selectedMemberIds.includes(agent.id)}
                                        onChange={(event) => toggleMember(agent.id, event.target.checked)}
                                    />
                                    {agent.name}
                                </label>
                            ))}
                        </div>
                    )}

                    <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap', marginTop: '16px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px' }}>
                            <span style={{ color: 'var(--text-secondary)' }}>{zh ? '日预算帽（Credits）' : 'Daily cap (Credits)'}</span>
                            <input
                                className="form-input"
                                type="number"
                                min={0}
                                value={dailyCap}
                                onChange={(event) => setDailyCap(event.target.value)}
                                onBlur={() => configured && saveCaps()}
                                style={{ width: '90px' }}
                            />
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px' }}>
                            <span style={{ color: 'var(--text-secondary)' }}>{zh ? '月预算帽（Credits）' : 'Monthly cap (Credits)'}</span>
                            <input
                                className="form-input"
                                type="number"
                                min={0}
                                value={monthlyCap}
                                onChange={(event) => setMonthlyCap(event.target.value)}
                                onBlur={() => configured && saveCaps()}
                                style={{ width: '90px' }}
                            />
                        </div>
                    </div>

                    {configured && enabled && (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '16px' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', maxWidth: '560px' }}>
                                <div style={{ fontSize: '13px' }}>
                                    <div style={{ fontWeight: 500 }}>{zh ? '每日简报节奏' : 'Daily briefing cadence'}</div>
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                        {zh ? '每日 09:00 简报 + 18:00 日报可见性摘要 + 周一 09:00 周报' : '09:00 brief, 18:00 collection digest, Monday 09:00 weekly brief'}
                                    </div>
                                </div>
                                {renderToggle(settings.briefing_enabled, patchMutation.isPending, (value) => toggleCadence('briefing_enabled', value), 'briefing_enabled')}
                            </div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', maxWidth: '560px' }}>
                                <div style={{ fontSize: '13px' }}>
                                    <div style={{ fontWeight: 500 }}>{zh ? '晨会节奏' : 'Morning meeting cadence'}</div>
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                        {zh ? '工作日 09:00 自动主持晨会；也可在 CEO 详情页手动开始' : 'Weekday 09:00 auto meeting; manual start stays available on the CEO page'}
                                    </div>
                                </div>
                                {renderToggle(settings.morning_meeting_enabled, patchMutation.isPending, (value) => toggleCadence('morning_meeting_enabled', value), 'morning_meeting_enabled')}
                            </div>
                        </div>
                    )}

                    {configured && enabled && (
                        <div style={{ marginTop: '18px', paddingTop: '16px', borderTop: '1px solid var(--border-subtle)' }}>
                            <div style={{ fontWeight: 600, fontSize: '13px', marginBottom: '12px' }}>
                                {zh ? '协调与派发权限' : 'Coordination and dispatch authority'}
                            </div>
                            {!settings.coordination_feature_available ? (
                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                    {zh
                                        ? '当前部署尚未为本公司开放协调型 canary；CEO 将保持观察型。'
                                        : 'The coordinator canary is not open for this company; the CEO remains observer-only.'}
                                </div>
                            ) : (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', maxWidth: '620px', gap: '16px' }}>
                                        <div style={{ fontSize: '13px' }}>
                                            <div style={{ fontWeight: 500 }}>{zh ? '协调型 CEO' : 'Coordinator mode'}</div>
                                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                                {zh ? '仅在当前人工对话中，依据最新能力目录委派并等待回执。' : 'Delegate only from a current human chat using current Directory evidence and correlated receipts.'}
                                            </div>
                                        </div>
                                        {renderToggle(settings.coordination_enabled, patchMutation.isPending, (value) => void toggleCoordination(value), 'coordination_enabled')}
                                    </div>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', maxWidth: '620px', gap: '16px' }}>
                                        <div style={{ fontSize: '13px' }}>
                                            <div style={{ fontWeight: 500 }}>{zh ? '允许非人工运行自动派发' : 'Allow autonomous dispatch from non-chat runs'}</div>
                                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                                {zh ? '独立高风险开关；关闭时，触发器、心跳与被委派运行都不能继续派发。' : 'Independent high-risk switch; when off, triggers, heartbeats, and delegated runs cannot dispatch onward.'}
                                            </div>
                                        </div>
                                        {renderToggle(settings.auto_dispatch_enabled, patchMutation.isPending || !settings.coordination_enabled, (value) => void toggleAutoDispatch(value), 'auto_dispatch_enabled')}
                                    </div>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '13px' }}>
                                        <span style={{ color: 'var(--text-secondary)' }}>{zh ? '最大并行委派数' : 'Maximum parallel delegations'}</span>
                                        <input
                                            className="form-input"
                                            type="number"
                                            min={1}
                                            max={12}
                                            value={maxParallel}
                                            onChange={(event) => setMaxParallel(event.target.value)}
                                            onBlur={saveParallelism}
                                            style={{ width: '90px' }}
                                        />
                                    </label>
                                </div>
                            )}
                        </div>
                    )}

                    <div style={{ display: 'flex', gap: '12px', marginTop: '20px' }}>
                        {!enabled ? (
                            <button
                                type="button"
                                className="btn btn-primary"
                                disabled={enableMutation.isPending}
                                onClick={() => enableMutation.mutate()}
                            >
                                {enableMutation.isPending
                                    ? (zh ? '正在启用…' : 'Enabling…')
                                    : configured
                                        ? (zh ? '重新启用公司 CEO' : 'Re-enable company CEO')
                                        : (zh ? '启用公司 CEO' : 'Enable company CEO')}
                            </button>
                        ) : (
                            <button
                                type="button"
                                className="btn btn-ghost"
                                disabled={disableMutation.isPending}
                                onClick={() => void confirmDisable()}
                            >
                                {zh ? '停用公司 CEO' : 'Disable company CEO'}
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
