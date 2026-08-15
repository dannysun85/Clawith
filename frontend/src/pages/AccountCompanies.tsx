import { IconArrowRight, IconBuilding, IconCrown, IconMail, IconPlus, IconShieldLock } from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';

import { AstraWordmark } from '../components/atlas';
import {
    authApi,
    governanceApi,
    tenantApi,
    type PendingOrganizationInvitation,
    type TenantDepartureResult,
    type TenantLeavePreflight,
    type TenantOwnershipTransfer,
} from '../services/api';
import { useAuthStore } from '../stores';
import { normalizeTenantRedirectUrl } from '../utils/authTransport';
import { hasEffectiveCapability, hasProductSurface, isPlatformOperator } from '../utils/productAccess';
import { commitSameOriginTenantSwitch, validateTenantSwitchCandidate } from '../utils/tenantSwitch';
import './productSurfaces.css';

const messageFrom = (error: unknown) => error instanceof Error ? error.message : String(error);

export default function AccountCompanies() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const { user, setAuth, setUser, logout } = useAuthStore();
    const isChinese = i18n.language.startsWith('zh');
    const [pendingTransfer, setPendingTransfer] = useState<TenantOwnershipTransfer | null>(null);
    const [leavePreflight, setLeavePreflight] = useState<TenantLeavePreflight | null>(null);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');

    const tenantsQuery = useQuery({ queryKey: ['account-company-memberships'], queryFn: () => authApi.getMyTenants() });
    const invitationsQuery = useQuery({ queryKey: ['account-pending-invitations'], queryFn: () => governanceApi.pendingInvitations() });

    const loadTransfer = async () => {
        if (!user?.tenant_id) { setPendingTransfer(null); return; }
        try { setPendingTransfer((await tenantApi.pendingOwnershipTransfer(user.tenant_id)).item); }
        catch { setPendingTransfer(null); }
    };
    useEffect(() => { void loadTransfer(); }, [user?.tenant_id]);

    const switchTenant = async (tenantId: string) => {
        setBusy(tenantId); setError('');
        try {
            const result = await authApi.switchTenant(tenantId);
            if (result.target_tenant_id !== tenantId) throw new Error('Tenant switch response does not match the requested company');
            if (result.redirect_url) {
                await validateTenantSwitchCandidate({ tenantId, accessToken: result.access_token, validateToken: authApi.me, resolvedTenantId: (candidate) => candidate.tenant_id });
                window.location.href = normalizeTenantRedirectUrl(result.redirect_url, window.location.href, tenantId);
                return;
            }
            await commitSameOriginTenantSwitch({ tenantId, accessToken: result.access_token, validateToken: authApi.me, establishAuth: setAuth, persistTenantId: (value) => localStorage.setItem('current_tenant_id', value), clearTenantId: () => localStorage.removeItem('current_tenant_id'), currentTenantId: () => localStorage.getItem('current_tenant_id'), resolvedTenantId: (candidate) => candidate.tenant_id });
            localStorage.setItem('preferred_product_surface', 'work');
            navigate('/work', { replace: true });
        } catch (nextError) { setError(messageFrom(nextError)); setBusy(''); }
    };

    const acceptInvitation = async (invitation: PendingOrganizationInvitation) => {
        setBusy(invitation.id); setError('');
        try {
            const result = await tenantApi.acceptInvitation(invitation.id);
            await queryClient.invalidateQueries({ queryKey: ['account-pending-invitations'] });
            await queryClient.invalidateQueries({ queryKey: ['account-company-memberships'] });
            if (window.confirm(isChinese ? `已加入 ${invitation.tenant_name}，现在切换？` : `Joined ${invitation.tenant_name}. Switch now?`)) {
                await commitSameOriginTenantSwitch({ tenantId: invitation.tenant_id, accessToken: result.access_token, validateToken: authApi.me, establishAuth: setAuth, persistTenantId: (value) => localStorage.setItem('current_tenant_id', value), clearTenantId: () => localStorage.removeItem('current_tenant_id'), currentTenantId: () => localStorage.getItem('current_tenant_id'), resolvedTenantId: (candidate) => candidate.tenant_id });
                queryClient.clear();
                navigate('/onboarding?mode=join', { replace: true });
            }
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const declineInvitation = async (invitationId: string) => {
        setBusy(invitationId); setError('');
        try { await tenantApi.declineInvitation(invitationId); await queryClient.invalidateQueries({ queryKey: ['account-pending-invitations'] }); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const acceptOwnership = async () => {
        if (!user?.tenant_id || !pendingTransfer) return;
        setBusy('ownership'); setError('');
        try {
            await tenantApi.acceptOwnershipTransfer(user.tenant_id, pendingTransfer.id);
            const nextUser = await authApi.me();
            setUser(nextUser);
            setPendingTransfer(null);
            navigate('/company-admin/ownership', { replace: true });
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const finishDeparture = async (result: TenantDepartureResult) => {
        if (result.fallback_tenant_id && result.access_token) {
            try {
                await commitSameOriginTenantSwitch({ tenantId: result.fallback_tenant_id, accessToken: result.access_token, validateToken: authApi.me, establishAuth: setAuth, persistTenantId: (value) => localStorage.setItem('current_tenant_id', value), clearTenantId: () => localStorage.removeItem('current_tenant_id'), currentTenantId: () => localStorage.getItem('current_tenant_id'), resolvedTenantId: (candidate) => candidate.tenant_id });
                queryClient.clear();
                localStorage.setItem('preferred_product_surface', 'work');
                navigate('/work', { replace: true });
                return;
            } catch {
                // A completed departure invalidates the old membership. Never
                // retain a mixed browser identity if fallback validation fails.
            }
        }
        queryClient.clear();
        logout();
        navigate('/login', { replace: true });
    };

    const reviewLeave = async () => {
        if (!user?.tenant_id) return;
        setBusy('leave-review'); setError('');
        try { setLeavePreflight(await tenantApi.leavePreflight(user.tenant_id)); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const leaveCurrent = async () => {
        if (!user?.tenant_id || !leavePreflight || !leavePreflight.can_leave) return;
        const warningCount = Object.entries(leavePreflight.summary)
            .filter(([key]) => key !== 'owned_agents')
            .reduce((total, [, value]) => total + value, 0);
        const confirmation = isChinese
            ? `确认退出当前公司？${warningCount ? `仍有 ${warningCount} 项工作或授权会按页面说明处理。` : ''}`
            : `Leave the current company? ${warningCount ? `${warningCount} work or access items will be handled as described.` : ''}`;
        if (!window.confirm(confirmation)) return;
        setBusy('leave'); setError('');
        try {
            const result = await tenantApi.leave(
                user.tenant_id,
                leavePreflight.requires_acknowledgement,
            );
            await finishDeparture(result);
        } catch (nextError) {
            setError(messageFrom(nextError));
            setBusy('');
            try { setLeavePreflight(await tenantApi.leavePreflight(user.tenant_id)); }
            catch { /* Keep the original departure error visible. */ }
        }
    };

    const memberships = tenantsQuery.data || [];
    const pendingInvitations = invitationsQuery.data?.items || [];
    const proposedTransfer = pendingTransfer?.proposed_owner_user_id === user?.id
        ? pendingTransfer
        : null;

    return (
        <main className="company-access account-companies">
            <header className="company-access__topbar">
                <AstraWordmark height={23} variant="ui" />
                <div className="console-row-actions">
                    <button type="button" className="btn btn-ghost" onClick={() => navigate('/account/security')}><IconShieldLock size={16} /> {isChinese ? '登录安全' : 'Login security'}</button>
                    {hasProductSurface(user, 'work') && <button type="button" className="btn btn-secondary" onClick={() => navigate('/work')}>{isChinese ? '返回工作台' : 'Back to work'}</button>}
                    {isPlatformOperator(user) && <button type="button" className="btn btn-ghost" onClick={() => navigate('/admin/platform')}>{isChinese ? '平台运营' : 'Platform operations'}</button>}
                </div>
            </header>
            <section className="company-access__intro">
                <span className="surface-eyebrow">{isChinese ? '账户级公司关系' : 'Account-level company relationships'}</span>
                <h1>{isChinese ? '公司、邀请与身份切换' : 'Companies, invitations, and identity switching'}</h1>
                <p>{isChinese ? '同一自然人可以拥有多家公司 membership。切换会获取新的 membership-scoped token，并清空旧公司的缓存；这里不靠 localStorage 决定权限。' : 'One identity may hold memberships in several companies. Switching obtains a new membership-scoped token and clears prior company cache; localStorage is never the authority.'}</p>
            </section>
            {error && <div className="surface-alert surface-alert--error" role="alert">{error}</div>}
            {proposedTransfer && <section className="surface-alert surface-alert--success" role="status"><IconCrown size={20} /><span><strong>{isChinese ? '你被提议为当前公司的新所有者' : 'You are the proposed owner of this company'}</strong>{isChinese ? `请在 ${new Date(proposedTransfer.expires_at).toLocaleString()} 前确认。接受后原所有者降为管理员。` : `Confirm by ${new Date(proposedTransfer.expires_at).toLocaleString()}. The prior owner becomes an administrator.`}</span><button type="button" className="btn btn-primary" disabled={busy === 'ownership'} onClick={() => void acceptOwnership()}>{isChinese ? '接受所有权' : 'Accept ownership'}</button></section>}
            <div className="company-access__grid">
                <section className="surface-card company-access__invitations">
                    <header><span className="surface-card__icon"><IconBuilding size={21} /></span><div><h2>{isChinese ? '我的公司成员关系' : 'My company memberships'}</h2><p>{isChinese ? `${memberships.length} 家可访问公司` : `${memberships.length} accessible companies`}</p></div></header>
                    <div className="company-access__invitation-list">
                        {memberships.map((tenant) => <article key={tenant.tenant_id}><div><strong>{tenant.tenant_name}</strong><span>{tenant.membership_role}</span><small>{tenant.tenant_id === user?.tenant_id ? (isChinese ? '当前公司' : 'Current company') : tenant.tenant_id}</small></div><div>{tenant.tenant_id !== user?.tenant_id && <button type="button" className="btn btn-primary" disabled={busy === tenant.tenant_id} onClick={() => void switchTenant(tenant.tenant_id)}>{isChinese ? '切换' : 'Switch'} <IconArrowRight size={15} /></button>}</div></article>)}
                        {!tenantsQuery.isLoading && memberships.length === 0 && <div className="surface-card__empty">{isChinese ? '当前没有有效公司成员关系。' : 'No active company membership.'}</div>}
                    </div>
                </section>
                <section className="surface-card company-access__invitations">
                    <header><span className="surface-card__icon"><IconMail size={21} /></span><div><h2>{isChinese ? '发给我的公司邀请' : 'Company invitations sent to me'}</h2><p>{isChinese ? `${pendingInvitations.length} 个待处理` : `${pendingInvitations.length} pending`}</p></div></header>
                    <div className="company-access__invitation-list">
                        {pendingInvitations.map((invitation) => <article key={invitation.id}><div><strong>{invitation.tenant_name}</strong><span>{invitation.role}</span><small>{new Date(invitation.expires_at).toLocaleString()}</small></div><div><button type="button" className="btn btn-primary" disabled={busy === invitation.id} onClick={() => void acceptInvitation(invitation)}>{isChinese ? '接受' : 'Accept'}</button><button type="button" className="btn btn-ghost" disabled={busy === invitation.id} onClick={() => void declineInvitation(invitation.id)}>{isChinese ? '拒绝' : 'Decline'}</button></div></article>)}
                        {!invitationsQuery.isLoading && pendingInvitations.length === 0 && <div className="surface-card__empty">{isChinese ? '暂无待处理邀请。' : 'No pending invitations.'}</div>}
                    </div>
                </section>
                <section className="surface-card">
                    <header><span className="surface-card__icon"><IconPlus size={21} /></span><div><h2>{isChinese ? '加入或创建其他公司' : 'Join or create another company'}</h2><p>{hasEffectiveCapability(user, 'company.create') ? (isChinese ? '你的账户拥有 company.create 权益。' : 'Your account has company.create.') : (isChinese ? '你可以接受邀请或使用管理员分享的加入令牌。' : 'You may accept an invitation or use an admin-shared join token.')}</p></div></header>
                    <button type="button" className="btn btn-secondary" onClick={() => navigate('/setup-company')}>{isChinese ? '打开公司访问流程' : 'Open company access flow'} <IconArrowRight size={15} /></button>
                </section>
                {user?.tenant_id && user.membership_role !== 'org_owner' && (
                    <section className="surface-card surface-card--disabled">
                        <header>
                            <span className="surface-card__icon"><IconBuilding size={21} /></span>
                            <div>
                                <h2>{isChinese ? '退出当前公司' : 'Leave current company'}</h2>
                                <p>{isChinese ? '先由服务端检查 Agent 所有权、审批、任务、交付物与个人凭证。退出只停用当前 membership，不删除全局账号和其他公司关系。' : 'The server first reviews Agent ownership, approvals, tasks, deliverables, and personal credentials. Leaving deactivates only this membership and preserves the global account and other companies.'}</p>
                            </div>
                        </header>
                        {!leavePreflight && (
                            <button
                                type="button"
                                className="btn btn-secondary"
                                disabled={busy === 'leave-review'}
                                onClick={() => void reviewLeave()}
                            >
                                {isChinese ? '检查退出条件' : 'Review leave conditions'}
                            </button>
                        )}
                        {leavePreflight && (
                            <div style={{ display: 'grid', gap: 12 }}>
                                {leavePreflight.blockers.length > 0 && (
                                    <div className="surface-alert surface-alert--error" role="alert">
                                        <span>
                                            <strong>{isChinese ? '需要先完成交接' : 'Handoff required'}</strong>
                                            {leavePreflight.blockers.map(item => (
                                                <span key={item.code} style={{ display: 'block' }}>
                                                    {item.message} ({item.count})
                                                </span>
                                            ))}
                                        </span>
                                    </div>
                                )}
                                {leavePreflight.owned_agents.length > 0 && (
                                    <div className="company-access__invitation-list">
                                        {leavePreflight.owned_agents.map(agent => (
                                            <article key={agent.id}>
                                                <div>
                                                    <strong>{agent.name}</strong>
                                                    <span>
                                                        {agent.is_personal_assistant
                                                            ? (isChinese ? '私人助理：因隐私不能转交，需删除' : 'Personal assistant: private, delete before leaving')
                                                            : (isChinese ? 'Agent 员工：转交所有权或删除' : 'Agent employee: handover or delete')}
                                                    </span>
                                                </div>
                                                <button type="button" className="btn btn-secondary" onClick={() => navigate(`/agents/${agent.id}`)}>
                                                    {isChinese ? '前往处理' : 'Open Agent'}
                                                </button>
                                            </article>
                                        ))}
                                    </div>
                                )}
                                <ul style={{ margin: 0, paddingLeft: 20, color: 'var(--text-secondary)', fontSize: 13 }}>
                                    <li>{isChinese ? `未完成任务：${leavePreflight.summary.open_tasks}；待处理审批：${leavePreflight.summary.pending_approvals}；交付物：${leavePreflight.summary.open_deliverables}` : `Open tasks: ${leavePreflight.summary.open_tasks}; approvals: ${leavePreflight.summary.pending_approvals}; deliverables: ${leavePreflight.summary.open_deliverables}`}</li>
                                    <li>{isChinese ? `受托管理授权：${leavePreflight.summary.delegated_agents}；个人凭证：${leavePreflight.summary.personal_credentials}` : `Delegated Agent grants: ${leavePreflight.summary.delegated_agents}; personal credentials: ${leavePreflight.summary.personal_credentials}`}</li>
                                    <li>{isChinese ? '退出时会撤销成员级 Agent 授权、使个人凭证失效；历史任务、交付物和审计记录保留。' : 'Leaving revokes membership-level Agent grants and expires personal credentials; historical tasks, deliverables, and audit records remain.'}</li>
                                </ul>
                                <div className="console-row-actions">
                                    <button type="button" className="btn btn-ghost" disabled={busy === 'leave-review'} onClick={() => void reviewLeave()}>
                                        {isChinese ? '重新检查' : 'Refresh review'}
                                    </button>
                                    <button type="button" className="btn btn-secondary" disabled={!leavePreflight.can_leave || busy === 'leave'} onClick={() => void leaveCurrent()}>
                                        {isChinese ? '确认并退出公司' : 'Confirm and leave'}
                                    </button>
                                </div>
                            </div>
                        )}
                    </section>
                )}
            </div>
        </main>
    );
}
