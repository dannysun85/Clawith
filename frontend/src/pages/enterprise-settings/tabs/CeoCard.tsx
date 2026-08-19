import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';

import { useDialog } from '../../../components/Dialog/DialogProvider';
import { agentApi, ceoApi, type CeoOrchestratorSettings } from '../../../services/api';
import type { Agent } from '../../../types';

/**
 * CEO orchestrator (P1 observer) settings card.
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
                            {zh ? '公司 CEO（观察型）' : 'Company CEO (observer)'}
                            <span style={{
                                marginLeft: '8px', padding: '1px 8px', borderRadius: '999px', fontSize: '11px',
                                background: 'rgba(99,102,241,0.12)', color: 'var(--accent-primary)', fontWeight: 500,
                            }}>
                                {zh ? '系统岗位 · 不占员工席位' : 'System role · no seat used'}
                            </span>
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.6, maxWidth: '620px' }}>
                            {zh
                                ? '启用后，公司 CEO 只读汇总业务全景、按节奏生成日报/周报，并可主持晨会产出纪要。行动项仅为文本建议，不会自动派发任务。运行消耗租户 Credits，可在下方设置日/月预算帽。'
                                : 'The company CEO reads the business panorama, produces daily/weekly briefings on a cadence, and can chair a morning meeting into minutes. Suggested actions are text only — nothing is dispatched automatically. Runs consume tenant Credits; cap them below.'}
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
