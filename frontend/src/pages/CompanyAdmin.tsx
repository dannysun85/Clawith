import {
    IconAlertTriangle,
    IconBuilding,
    IconChecklist,
    IconCrown,
    IconFileAnalytics,
    IconHierarchy3,
    IconHome,
    IconLink,
    IconLockAccess,
    IconPlugConnected,
    IconReceipt,
    IconRefresh,
    IconRobot,
    IconSettings,
    IconShoppingCart,
    IconTrash,
    IconUserPlus,
    IconUsers,
} from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';

import ProductConsoleShell, { type ProductConsoleNavItem } from '../components/ProductConsoleShell';
import { authApi, governanceApi, membershipApi, tenantApi, type OrganizationInvitation, type TenantDepartureResult } from '../services/api';
import { useAuthStore } from '../stores';
import { hasEffectiveCapability, hasProductSurface, isCompanyOwner, isPlatformOperator } from '../utils/productAccess';
import { commitSameOriginTenantSwitch } from '../utils/tenantSwitch';
import EnterpriseSettings from './EnterpriseSettings';
import SubscriptionTab from './enterprise-settings/tabs/SubscriptionTab';
import SubscriptionDetail from './SubscriptionDetail';

type CompanyMember = {
    id: string;
    username?: string | null;
    email?: string | null;
    display_name?: string | null;
    role: 'member' | 'org_admin' | 'org_owner' | 'agent_admin';
    is_active: boolean;
    agents_count?: number;
    source?: string;
    mfa_enabled?: boolean;
    mfa_required?: boolean;
};

type IssuedCredential = {
    label: string;
    token: string;
};

type IntegrationView = 'tools' | 'skills' | 'org' | 'douyin';

const INTEGRATION_VIEWS: IntegrationView[] = ['tools', 'skills', 'org', 'douyin'];

const messageFrom = (error: unknown) => error instanceof Error ? error.message : String(error);

const deliveryLabel = (status: string, isChinese: boolean) => {
    const labels: Record<string, [string, string]> = {
        queued: ['等待发送', 'Queued'],
        sending: ['正在发送', 'Sending'],
        retry_wait: ['等待自动重试', 'Waiting to retry'],
        smtp_accepted: ['SMTP 已接受（不代表对方已读）', 'Accepted by SMTP (not proof of recipient delivery)'],
        blocked_configuration: ['邮件配置未就绪', 'Email configuration is not ready'],
        permanent_failed: ['发送失败，需处理', 'Delivery failed and needs attention'],
        cancelled: ['已取消', 'Cancelled'],
        manual_link_issued: ['已生成一次人工链接', 'One manual link issued'],
        not_queued: ['未进入邮件队列', 'Not queued for email'],
    };
    const label = labels[status] || [status, status];
    return isChinese ? label[0] : label[1];
};

function PageHeader({ title, description, actions }: { title: string; description: string; actions?: React.ReactNode }) {
    return (
        <header className="console-page-header">
            <div><h1>{title}</h1><p>{description}</p></div>
            {actions && <div className="console-row-actions">{actions}</div>}
        </header>
    );
}

function CompanyOverview({ companyName }: { companyName: string }) {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const user = useAuthStore((state) => state.user);
    const isChinese = i18n.language.startsWith('zh');
    const capabilityGroups = [
        {
            title: isChinese ? '成员治理' : 'Member governance',
            body: isChinese ? '邀请、停用和恢复成员；只有所有者可以任免公司管理员。' : 'Invite, deactivate, and reactivate members. Only the owner appoints administrators.',
            capability: 'company.members.manage',
            to: '/company-admin/members',
            icon: <IconUsers size={21} />,
        },
        {
            title: isChinese ? 'Agent 员工治理' : 'Agent governance',
            body: isChinese ? '按单个 Agent 的 use/manage 授权治理；私人助手始终 owner-only。' : 'Govern per-Agent use/manage grants. Personal assistants always stay owner-only.',
            capability: 'agent.manage.company',
            to: '/company-admin/agents',
            icon: <IconRobot size={21} />,
        },
        {
            title: isChinese ? '套餐与账单' : 'Plan and billing',
            body: isChinese ? '查看公司聚合用量与订阅；支付主体和续费动作按能力开放。' : 'Review company usage and subscriptions; billing actions remain capability-gated.',
            capability: 'company.billing.view',
            to: '/company-admin/billing',
            icon: <IconReceipt size={21} />,
        },
    ];

    return (
        <>
            <PageHeader
                title={isChinese ? '公司管理概览' : 'Company administration'}
                description={isChinese
                    ? `${companyName} 的治理控制面。管理员仍然是公司员工；这里不复制工作台、Agent 消息或私人助理内容。`
                    : `Governance for ${companyName}. Administrators remain employees; this surface does not duplicate work, Agent messages, or personal-assistant content.`}
            />
            <div className="console-inline-notice">
                <IconLockAccess size={19} />
                <span>{isChinese
                    ? '当前页面只根据服务端 effective_capabilities 开放动作。平台运营权不会自动获得本公司的治理权限。'
                    : 'Actions follow server-issued effective_capabilities. Platform authority never implies governance access to this company.'}</span>
            </div>
            <div className="console-grid" style={{ marginTop: 16 }}>
                {capabilityGroups.map((item) => {
                    const enabled = hasEffectiveCapability(user, item.capability);
                    return (
                        <section className="console-card" key={item.capability}>
                            <div className="console-row-actions" style={{ marginBottom: 12 }}>{item.icon}<span className={`console-status ${enabled ? 'console-status--active' : ''}`}>{enabled ? (isChinese ? '已授权' : 'Available') : (isChinese ? '未授权' : 'Unavailable')}</span></div>
                            <h2>{item.title}</h2>
                            <p>{item.body}</p>
                            {enabled && <button type="button" className="btn btn-secondary" style={{ marginTop: 16 }} onClick={() => navigate(item.to)}>{isChinese ? '进入' : 'Open'}</button>}
                        </section>
                    );
                })}
            </div>
        </>
    );
}

function CompanyMembers({ tenantId }: { tenantId: string }) {
    const { i18n } = useTranslation();
    const queryClient = useQueryClient();
    const user = useAuthStore((state) => state.user);
    const isChinese = i18n.language.startsWith('zh');
    const [email, setEmail] = useState('');
    const [role, setRole] = useState<'member' | 'org_admin'>('member');
    const [expiresInDays, setExpiresInDays] = useState(7);
    const [joinLinkUses, setJoinLinkUses] = useState(1);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [issued, setIssued] = useState<IssuedCredential | null>(null);
    const [manualTarget, setManualTarget] = useState<OrganizationInvitation | null>(null);
    const [manualPassword, setManualPassword] = useState('');
    const [mfaResetTarget, setMfaResetTarget] = useState<CompanyMember | null>(null);
    const [mfaResetPassword, setMfaResetPassword] = useState('');
    const [mfaResetReason, setMfaResetReason] = useState('');

    const canInvite = hasEffectiveCapability(user, 'company.members.invite');
    const canManage = hasEffectiveCapability(user, 'company.members.manage');
    const canManageAdmins = hasEffectiveCapability(user, 'company.admins.manage');

    const membersQuery = useQuery({
        queryKey: ['company-governance-members', tenantId],
        queryFn: () => membershipApi.list() as Promise<CompanyMember[]>,
        enabled: !!tenantId,
    });
    const invitationsQuery = useQuery({
        queryKey: ['company-governance-invitations', tenantId],
        queryFn: () => governanceApi.organizationInvitations(tenantId),
        enabled: !!tenantId && canInvite,
    });
    const linksQuery = useQuery({
        queryKey: ['company-governance-join-links', tenantId],
        queryFn: () => governanceApi.joinLinks(tenantId),
        enabled: !!tenantId && canInvite,
    });

    const refresh = async () => {
        await Promise.all([
            queryClient.invalidateQueries({ queryKey: ['company-governance-members', tenantId] }),
            queryClient.invalidateQueries({ queryKey: ['company-governance-invitations', tenantId] }),
            queryClient.invalidateQueries({ queryKey: ['company-governance-join-links', tenantId] }),
        ]);
    };

    const createInvitation = async (event: React.FormEvent) => {
        event.preventDefault();
        setBusy('invite'); setError(''); setNotice(''); setIssued(null);
        try {
            const result = await governanceApi.createOrganizationInvitation(tenantId, {
                email: email.trim(), role, expires_in_days: expiresInDays,
            });
            setNotice(isChinese
                ? `邀请已安全受理，当前投递状态：${deliveryLabel(result.delivery_status, true)}。`
                : `Invitation accepted. Delivery status: ${deliveryLabel(result.delivery_status, false)}.`);
            setEmail('');
            await refresh();
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const createJoinLink = async () => {
        setBusy('link'); setError(''); setNotice(''); setIssued(null);
        try {
            const result = await governanceApi.createJoinLink(tenantId, { max_uses: joinLinkUses, expires_in_days: 7 });
            setIssued({ label: isChinese ? '一次性显示的低风险加入令牌' : 'Low-risk join token (shown once)', token: String(result.token) });
            await refresh();
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const mutateMember = async (member: CompanyMember, action: 'activate' | 'deactivate' | 'member' | 'org_admin') => {
        setBusy(`${member.id}:${action}`); setError('');
        try {
            if (action === 'activate') await membershipApi.reactivate(member.id);
            else if (action === 'deactivate') {
                const review = await membershipApi.deactivationPreflight(member.id);
                const responsibilityCount = Object.values(review.summary)
                    .reduce((total, value) => total + value, 0);
                const confirmed = window.confirm(
                    isChinese
                        ? `确认立即停用 ${member.display_name || member.email || '该成员'}？其当前公司访问会立即失效。系统发现 ${responsibilityCount} 项 Agent、任务、审批、交付或凭证责任；私人 Agent 内容不会向管理员开放。`
                        : `Deactivate ${member.display_name || member.email || 'this member'} now? Current company access ends immediately. The review found ${responsibilityCount} Agent, task, approval, delivery, or credential responsibilities; private Agent content remains hidden from administrators.`,
                );
                if (!confirmed) return;
                await membershipApi.deactivate(member.id, responsibilityCount > 0);
            }
            else await membershipApi.updateRole(member.id, action);
            await refresh();
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const resetMemberMfa = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!mfaResetTarget) return;
        setBusy(`${mfaResetTarget.id}:mfa-reset`); setError(''); setNotice('');
        try {
            const result = await membershipApi.resetMfa(mfaResetTarget.id, {
                current_password: mfaResetPassword,
                reason: mfaResetReason.trim(),
            });
            setNotice(isChinese
                ? `已重置 ${mfaResetTarget.display_name || mfaResetTarget.email || '该成员'} 的 MFA；其全部旧会话与恢复码已失效${result.requires_setup ? '，下次登录必须重新绑定' : ''}。`
                : `MFA reset for ${mfaResetTarget.display_name || mfaResetTarget.email || 'the member'}. All prior sessions and recovery codes are invalid${result.requires_setup ? '; setup is required at next login' : ''}.`);
            setMfaResetTarget(null); setMfaResetPassword(''); setMfaResetReason('');
            await refresh();
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const revokeInvitation = async (id: string) => {
        setBusy(id); setError('');
        try { await governanceApi.revokeOrganizationInvitation(tenantId, id); await refresh(); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const resendInvitation = async (invitation: OrganizationInvitation) => {
        setBusy(`resend:${invitation.id}`); setError(''); setNotice(''); setIssued(null);
        try {
            const result = await governanceApi.resendOrganizationInvitation(tenantId, invitation.id);
            setNotice(isChinese
                ? `旧链接已失效并重新受理，当前投递状态：${deliveryLabel(result.delivery_status, true)}。`
                : `The old link is invalid and the replacement was accepted. Status: ${deliveryLabel(result.delivery_status, false)}.`);
            await refresh();
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const issueManualLink = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!manualTarget) return;
        setBusy(`manual:${manualTarget.id}`); setError(''); setNotice(''); setIssued(null);
        try {
            const result = await governanceApi.issueOrganizationInvitationManualLink(
                tenantId,
                manualTarget.id,
                manualPassword,
            );
            setIssued({
                label: isChinese ? '敏感邀请链接（仅显示本次）' : 'Sensitive invitation link (shown once)',
                token: result.manual_url,
            });
            setNotice(isChinese
                ? '旧链接已失效。请只通过可信渠道发给目标邮箱本人。'
                : 'The old link is invalid. Share this only with the intended recipient through a trusted channel.');
            setManualTarget(null);
            setManualPassword('');
            await refresh();
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    const revokeLink = async (id: string) => {
        setBusy(id); setError('');
        try { await governanceApi.revokeJoinLink(tenantId, id); await refresh(); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };

    return (
        <>
            <PageHeader
                title={isChinese ? '成员与邀请' : 'Members and invitations'}
                description={isChinese
                    ? '邮箱邀请创建明确的公司成员关系；低风险加入链接只授予 member。平台注册凭证不在这里管理。'
                    : 'Email invitations create explicit company memberships. Low-risk join links grant member only. Platform registration grants are managed elsewhere.'}
                actions={<button type="button" className="btn btn-secondary" onClick={() => void refresh()}><IconRefresh size={15} /> {isChinese ? '刷新' : 'Refresh'}</button>}
            />
            {error && <div className="console-inline-notice console-inline-notice--error" role="alert">{error}</div>}
            {notice && <div className="console-inline-notice console-inline-notice--success" role="status">{notice}</div>}
            {issued && (
                <div className="console-inline-notice console-inline-notice--success" role="status" style={{ marginTop: 12 }}>
                    <div style={{ flex: 1 }}><strong>{issued.label}</strong><code className="console-code" style={{ marginTop: 8 }}>{issued.token}</code></div>
                    <button type="button" className="btn btn-secondary" onClick={() => void navigator.clipboard.writeText(issued.token)}>{isChinese ? '复制' : 'Copy'}</button>
                </div>
            )}
            {manualTarget && (
                <section className="console-card console-card--wide" style={{ marginTop: 12 }} aria-labelledby="manual-invite-title">
                    <h2 id="manual-invite-title">{isChinese ? '生成一次人工邀请链接' : 'Generate one manual invitation link'}</h2>
                    <p>{isChinese
                        ? `目标：${manualTarget.target_email}。此操作会立即作废旧链接，生成的链接只显示一次，并写入审计。`
                        : `Target: ${manualTarget.target_email}. This invalidates the old link immediately; the replacement is shown once and audited.`}</p>
                    <form className="console-form" onSubmit={issueManualLink} style={{ marginTop: 12 }}>
                        <label>{isChinese ? '当前密码（二次认证）' : 'Current password (reauthentication)'}
                            <input type="password" autoComplete="current-password" value={manualPassword} onChange={(event) => setManualPassword(event.target.value)} required />
                        </label>
                        <div className="console-row-actions">
                            <button type="submit" className="btn btn-primary" disabled={!manualPassword || busy === `manual:${manualTarget.id}`}>{busy === `manual:${manualTarget.id}` ? '…' : (isChinese ? '确认生成并作废旧链接' : 'Generate and invalidate old link')}</button>
                            <button type="button" className="btn btn-secondary" onClick={() => { setManualTarget(null); setManualPassword(''); }}>{isChinese ? '取消' : 'Cancel'}</button>
                        </div>
                    </form>
                </section>
            )}
            {mfaResetTarget && (
                <section className="console-card console-card--wide" style={{ marginTop: 12 }} aria-labelledby="mfa-reset-title">
                    <h2 id="mfa-reset-title">{isChinese ? '管理员重置成员 MFA' : 'Administrative member MFA reset'}</h2>
                    <p>{isChinese
                        ? `目标：${mfaResetTarget.display_name || mfaResetTarget.email || mfaResetTarget.id}。这会影响该员工的全局 Identity、撤销全部旧会话和恢复码；若该 Identity 还属于其他公司，服务端会拒绝并要求平台运营处理。`
                        : `Target: ${mfaResetTarget.display_name || mfaResetTarget.email || mfaResetTarget.id}. This affects the global Identity and revokes all sessions and recovery codes. The server refuses company-level reset when the Identity belongs to another company.`}</p>
                    <form className="console-form" onSubmit={resetMemberMfa} style={{ marginTop: 12 }}>
                        <label>{isChinese ? '当前密码（二次认证）' : 'Current password (reauthentication)'}<input type="password" autoComplete="current-password" value={mfaResetPassword} onChange={(event) => setMfaResetPassword(event.target.value)} required /></label>
                        <label>{isChinese ? '重置原因（至少 10 个字符）' : 'Reset reason (at least 10 characters)'}<textarea value={mfaResetReason} onChange={(event) => setMfaResetReason(event.target.value)} minLength={10} maxLength={500} required /></label>
                        <div className="console-row-actions"><button type="submit" className="btn btn-danger" disabled={!mfaResetPassword || mfaResetReason.trim().length < 10 || busy === `${mfaResetTarget.id}:mfa-reset`}>{isChinese ? '确认全局重置' : 'Confirm global reset'}</button><button type="button" className="btn btn-secondary" onClick={() => { setMfaResetTarget(null); setMfaResetPassword(''); setMfaResetReason(''); }}>{isChinese ? '取消' : 'Cancel'}</button></div>
                    </form>
                </section>
            )}

            {canInvite && (
                <div className="console-grid" style={{ marginTop: 16 }}>
                    <section className="console-card console-card--half">
                        <h2><IconUserPlus size={18} /> {isChinese ? '按邮箱邀请' : 'Invite by email'}</h2>
                        <p>{isChinese ? '绑定目标邮箱、公司、角色与有效期。只有所有者能邀请公司管理员。' : 'Bound to email, company, role, and expiry. Only the owner may invite an administrator.'}</p>
                        <form className="console-form" style={{ marginTop: 16 }} onSubmit={createInvitation}>
                            <label>{isChinese ? '目标邮箱' : 'Email'}<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
                            <div className="console-form-row">
                                <label>{isChinese ? '成员角色' : 'Membership role'}
                                    <select value={role} onChange={(event) => setRole(event.target.value as 'member' | 'org_admin')}>
                                        <option value="member">member</option>
                                        {canManageAdmins && <option value="org_admin">org_admin</option>}
                                    </select>
                                </label>
                                <label>{isChinese ? '有效天数' : 'Valid days'}<input type="number" min={1} max={30} value={expiresInDays} onChange={(event) => setExpiresInDays(Number(event.target.value))} /></label>
                            </div>
                            <button type="submit" className="btn btn-primary" disabled={busy === 'invite' || !email.trim()}>{busy === 'invite' ? '…' : (isChinese ? '发送邮箱邀请' : 'Send email invitation')}</button>
                        </form>
                    </section>
                    <section className="console-card console-card--half">
                        <h2><IconLink size={18} /> {isChinese ? '低风险加入链接' : 'Low-risk join link'}</h2>
                        <p>{isChinese ? '适合管理员明确分享给一组员工；始终只创建 member，不能创建管理员或所有者。' : 'For explicit sharing with employees; always creates member, never administrator or owner.'}</p>
                        <div className="console-form" style={{ marginTop: 16 }}>
                            <label>{isChinese ? '最大使用次数' : 'Maximum uses'}<input type="number" min={1} max={1000} value={joinLinkUses} onChange={(event) => setJoinLinkUses(Number(event.target.value))} /></label>
                            <button type="button" className="btn btn-secondary" disabled={busy === 'link'} onClick={() => void createJoinLink()}>{busy === 'link' ? '…' : (isChinese ? '创建 7 天加入链接' : 'Create 7-day join link')}</button>
                        </div>
                    </section>
                </div>
            )}

            <section style={{ marginTop: 24 }}>
                <h2 style={{ fontSize: 16 }}>{isChinese ? '公司成员' : 'Company members'}</h2>
                <div className="console-table-wrap">
                    <table className="console-table">
                        <thead><tr><th>{isChinese ? '成员' : 'Member'}</th><th>{isChinese ? '角色' : 'Role'}</th><th>{isChinese ? '状态' : 'Status'}</th><th>Agent</th><th>{isChinese ? '操作' : 'Actions'}</th></tr></thead>
                        <tbody>
                            {(membersQuery.data || []).map((member) => (
                                <tr key={member.id}>
                                    <td><strong>{member.display_name || member.username || '-'}</strong><br /><small>{member.email || member.source || '-'}</small></td>
                                    <td><span className="console-status">{member.role === 'agent_admin' ? 'member (legacy)' : member.role}</span></td>
                                    <td><span className={`console-status ${member.is_active ? 'console-status--active' : 'console-status--danger'}`}>{member.is_active ? (isChinese ? '有效' : 'Active') : (isChinese ? '已停用' : 'Inactive')}</span></td>
                                    <td>{member.agents_count || 0}</td>
                                    <td>
                                        <div className="console-row-actions">
                                            {canManageAdmins && member.role !== 'org_owner' && member.is_active && (
                                                <select aria-label={isChinese ? '变更成员角色' : 'Change member role'} value={member.role === 'org_admin' ? 'org_admin' : 'member'} onChange={(event) => void mutateMember(member, event.target.value as 'member' | 'org_admin')} disabled={busy.startsWith(member.id)}>
                                                    <option value="member">member</option><option value="org_admin">org_admin</option>
                                                </select>
                                            )}
                                            {canManage && member.id !== user?.id && member.role !== 'org_owner' && (
                                                <button type="button" className="btn btn-ghost" disabled={busy.startsWith(member.id)} onClick={() => void mutateMember(member, member.is_active ? 'deactivate' : 'activate')}>
                                                    {member.is_active ? (isChinese ? '停用' : 'Deactivate') : (isChinese ? '恢复' : 'Reactivate')}
                                                </button>
                                            )}
                                            {canManage && member.id !== user?.id && member.role === 'member' && member.is_active && member.mfa_enabled && (
                                                <button type="button" className="btn btn-ghost" disabled={busy.startsWith(member.id)} onClick={() => { setMfaResetTarget(member); setMfaResetPassword(''); setMfaResetReason(''); }}>
                                                    {isChinese ? '重置 MFA' : 'Reset MFA'}
                                                </button>
                                            )}
                                        </div>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                    {!membersQuery.isLoading && (membersQuery.data || []).length === 0 && <div className="console-empty">{isChinese ? '暂无成员' : 'No members'}</div>}
                </div>
            </section>

            {canInvite && (
                <div className="console-grid" style={{ marginTop: 24 }}>
                    <section className="console-card console-card--half">
                        <h2>{isChinese ? '邮箱邀请记录' : 'Email invitations'}</h2>
                        <div className="console-stack" style={{ marginTop: 14 }}>
                            {(invitationsQuery.data?.items || []).map((invitation) => (
                                <div key={invitation.id} className="console-inline-notice">
                                    <div style={{ flex: 1 }}>
                                        <strong>{invitation.target_email}</strong><br />
                                        <small>{invitation.role} · {invitation.status} · {new Date(invitation.expires_at).toLocaleString()}</small><br />
                                        <small>{deliveryLabel(invitation.delivery_status, isChinese)}{invitation.delivery?.attempt_count ? ` · ${isChinese ? '尝试' : 'attempts'} ${invitation.delivery.attempt_count}/${invitation.delivery.max_attempts}` : ''}</small>
                                        {invitation.delivery?.last_error_code && <><br /><small>{isChinese ? '错误码' : 'Error'}: {invitation.delivery.last_error_code}</small></>}
                                    </div>
                                    {invitation.status === 'pending' && (
                                        <div className="console-row-actions">
                                            <button type="button" className="btn btn-ghost" disabled={busy !== ''} onClick={() => void resendInvitation(invitation)}>{isChinese ? '重新发送' : 'Resend'}</button>
                                            <button type="button" className="btn btn-ghost" disabled={busy !== ''} onClick={() => { setManualTarget(invitation); setManualPassword(''); setIssued(null); }}>{isChinese ? '人工链接' : 'Manual link'}</button>
                                            <button type="button" className="btn btn-ghost" disabled={busy !== ''} onClick={() => void revokeInvitation(invitation.id)}>{isChinese ? '撤销' : 'Revoke'}</button>
                                        </div>
                                    )}
                                </div>
                            ))}
                            {(invitationsQuery.data?.items || []).length === 0 && <div className="console-empty">{isChinese ? '暂无邀请' : 'No invitations'}</div>}
                        </div>
                    </section>
                    <section className="console-card console-card--half">
                        <h2>{isChinese ? '加入链接记录' : 'Join links'}</h2>
                        <div className="console-stack" style={{ marginTop: 14 }}>
                            {(linksQuery.data?.items || []).map((link: any) => (
                                <div key={link.id} className="console-inline-notice">
                                    <div style={{ flex: 1 }}><strong>{link.token_prefix}…</strong><br /><small>{link.used_count}/{link.max_uses} · {link.status}</small></div>
                                    {link.status === 'active' && <button type="button" className="btn btn-ghost" disabled={busy === link.id} onClick={() => void revokeLink(link.id)}>{isChinese ? '撤销' : 'Revoke'}</button>}
                                </div>
                            ))}
                            {(linksQuery.data?.items || []).length === 0 && <div className="console-empty">{isChinese ? '暂无加入链接' : 'No join links'}</div>}
                        </div>
                    </section>
                </div>
            )}
        </>
    );
}

function AgentGovernance() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const isChinese = i18n.language.startsWith('zh');
    return (
        <>
            <PageHeader title={isChinese ? 'Agent 员工治理' : 'Agent workforce governance'} description={isChinese ? '公司级入口只治理员工可见性、负责人和对象授权；对话仍回到原 Agent 消息界面。' : 'This company-level surface governs visibility, responsibility, and object grants. Conversations stay in the existing Agent message UI.'} />
            <div className="console-grid">
                <section className="console-card console-card--half"><IconHierarchy3 size={24} /><h2 style={{ marginTop: 12 }}>{isChinese ? '员工网络与完整名册' : 'Workforce network and directory'}</h2><p>{isChinese ? '在数字员工中心切换“可用员工 / 我管理的员工 / 公司治理”视图；无协作边的员工仍保留在名册。' : 'Use Available / Managed by me / Company governance views. Employees without graph edges remain in the directory.'}</p><button type="button" className="btn btn-primary" style={{ marginTop: 16 }} onClick={() => navigate('/employees?view=directory&scope=governance')}>{isChinese ? '打开数字员工中心' : 'Open Digital Employees'}</button></section>
                <section className="console-card console-card--half"><IconLockAccess size={24} /><h2 style={{ marginTop: 12 }}>{isChinese ? '对象级 use / manage' : 'Object-level use / manage'}</h2><p>{isChinese ? '授权发生在具体 Agent 的设置页。撤权后页面会通过身份热更新与服务端 403 立即降级；私人助手不能被委派。' : 'Grant access on the specific Agent settings page. Revocation is enforced by identity refresh and server 403; personal assistants cannot be delegated.'}</p></section>
            </div>
        </>
    );
}

function CompanyPolicySettings({ tenantId, tenant }: { tenantId: string; tenant: any }) {
    const { i18n } = useTranslation();
    const queryClient = useQueryClient();
    const isChinese = i18n.language.startsWith('zh');
    const [form, setForm] = useState({
        name: '',
        timezone: 'UTC',
        country_region: '001',
        company_size: 'unspecified',
        allow_member_private_agents: false,
        default_approval_policy: 'high_risk',
    });
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [saved, setSaved] = useState(false);

    useEffect(() => {
        if (!tenant) return;
        setForm({
            name: tenant.name || '',
            timezone: tenant.timezone || 'UTC',
            country_region: tenant.country_region || '001',
            company_size: tenant.company_size || 'unspecified',
            allow_member_private_agents: Boolean(tenant.allow_member_private_agents),
            default_approval_policy: tenant.default_approval_policy || 'high_risk',
        });
    }, [tenant]);

    const save = async (event: React.FormEvent) => {
        event.preventDefault();
        setBusy(true); setError(''); setSaved(false);
        try {
            await tenantApi.update(tenantId, form);
            await queryClient.invalidateQueries({ queryKey: ['company-console-tenant', tenantId] });
            setSaved(true);
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(false); }
    };

    return (
        <>
            <PageHeader
                title={isChinese ? '公司与成员默认策略' : 'Company and member defaults'}
                description={isChinese ? '这里延续首次公司初始化的真实策略。修改后，成员创建能力会在下一次身份刷新时生效；默认审批策略应用于之后新建的 Agent。' : 'These are the live policies from company initialization. Member creation access changes on the next identity refresh; approval defaults apply to newly created Agents.'}
            />
            {error && <div className="console-inline-notice console-inline-notice--error">{error}</div>}
            {saved && <div className="console-inline-notice console-inline-notice--success">{isChinese ? '公司策略已保存。' : 'Company policy saved.'}</div>}
            <section className="console-card console-card--wide" style={{ marginTop: 16 }}>
                <form className="console-form" onSubmit={save}>
                    <div className="console-form-row">
                        <label>{isChinese ? '公司名称' : 'Company name'}<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required /></label>
                        <label>{isChinese ? '公司规模' : 'Company size'}<select value={form.company_size} onChange={(event) => setForm({ ...form, company_size: event.target.value })}><option value="unspecified">{isChinese ? '未指定' : 'Not specified'}</option><option value="1-10">1–10</option><option value="11-50">11–50</option><option value="51-200">51–200</option><option value="201-1000">201–1000</option><option value="1000+">1000+</option></select></label>
                    </div>
                    <div className="console-form-row">
                        <label>{isChinese ? '公司时区' : 'Company timezone'}<input value={form.timezone} onChange={(event) => setForm({ ...form, timezone: event.target.value })} required /></label>
                        <label>{isChinese ? '地区' : 'Region'}<select value={form.country_region} onChange={(event) => setForm({ ...form, country_region: event.target.value })}><option value="CN">CN</option><option value="HK">HK</option><option value="SG">SG</option><option value="US">US</option><option value="001">GLOBAL</option></select></label>
                    </div>
                    <label className="console-check"><input type="checkbox" checked={form.allow_member_private_agents} onChange={(event) => setForm({ ...form, allow_member_private_agents: event.target.checked })} /><span><strong>{isChinese ? '允许普通成员创建额外的私有 Agent' : 'Allow members to create additional private Agents'}</strong><small>{isChinese ? '私人助理不受此开关影响，始终保留一个 owner-only 槽位。' : 'This never removes the one owner-only private-assistant slot.'}</small></span></label>
                    <label>{isChinese ? '新 Agent 默认审批策略' : 'Default approval policy for new Agents'}<select value={form.default_approval_policy} onChange={(event) => setForm({ ...form, default_approval_policy: event.target.value })}><option value="high_risk">{isChinese ? '仅高风险操作审批' : 'High-risk actions only'}</option><option value="external_actions">{isChinese ? '所有外部动作审批' : 'All external actions'}</option><option value="all_writes">{isChinese ? '所有写入和外部动作审批' : 'All writes and external actions'}</option></select></label>
                    <button type="submit" className="btn btn-primary" disabled={busy || !form.name.trim()}>{busy ? '…' : (isChinese ? '保存公司策略' : 'Save company policy')}</button>
                </form>
            </section>
            <div style={{ marginTop: 24 }}><EnterpriseSettings initialTab="info" embedded /></div>
        </>
    );
}

function CompanyOwnership({ tenantId, companyName, members }: { tenantId: string; companyName: string; members: CompanyMember[] }) {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const { user, setAuth, setUser, logout } = useAuthStore();
    const isChinese = i18n.language.startsWith('zh');
    const [targetId, setTargetId] = useState('');
    const [password, setPassword] = useState('');
    const [deleteName, setDeleteName] = useState('');
    const [deletePassword, setDeletePassword] = useState('');
    const [pending, setPending] = useState<any | null>(null);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [message, setMessage] = useState('');
    const owner = isCompanyOwner(user);

    const loadPending = async () => {
        try { setPending((await tenantApi.pendingOwnershipTransfer(tenantId)).item); }
        catch { setPending(null); }
    };
    useEffect(() => { void loadPending(); }, [tenantId]);

    const requestTransfer = async (event: React.FormEvent) => {
        event.preventDefault(); setBusy('transfer'); setError(''); setMessage('');
        try {
            await tenantApi.requestOwnershipTransfer(tenantId, { new_owner_user_id: targetId, current_password: password });
            setPassword(''); setMessage(isChinese ? '已发起所有权转移，目标成员需在“公司与邀请”中确认。' : 'Ownership transfer requested. The target must confirm it in Company & invitations.');
            await loadPending();
        } catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };
    const cancelTransfer = async () => {
        if (!pending) return; setBusy('cancel'); setError('');
        try { await tenantApi.cancelOwnershipTransfer(tenantId, pending.id); await loadPending(); }
        catch (nextError) { setError(messageFrom(nextError)); }
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
                // The previous company context is no longer usable after a
                // successful departure, so reject mixed local identity state.
            }
        }
        queryClient.clear();
        logout();
        navigate('/login', { replace: true });
    };
    const leaveCompany = () => navigate('/account/companies');
    const deleteCompany = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!window.confirm(isChinese ? '确认进入 30 天可恢复停用期？公司数据不会立即永久删除。' : 'Schedule the 30-day recoverable deletion window? Data is not immediately destroyed.')) return;
        setBusy('delete'); setError('');
        try {
            const result = await tenantApi.scheduleDeletion(tenantId, { company_name: deleteName, current_password: deletePassword });
            await finishDeparture(result);
        } catch (nextError) { setError(messageFrom(nextError)); setBusy(''); }
    };

    const candidates = members.filter((member) => member.is_active && member.id !== user?.id && member.role !== 'org_owner');
    return (
        <>
            <PageHeader title={isChinese ? '所有权与公司生命周期' : 'Ownership and company lifecycle'} description={isChinese ? '所有权是独立高风险角色。转移需要当前所有者再次认证、目标成员确认；删除先进入 30 天可恢复停用期。' : 'Ownership is a separate high-risk role. Transfer requires reauthentication and target confirmation; deletion starts a 30-day recoverable window.'} />
            {error && <div className="console-inline-notice console-inline-notice--error" role="alert">{error}</div>}
            {message && <div className="console-inline-notice console-inline-notice--success" role="status">{message}</div>}
            {pending && <div className="console-inline-notice" style={{ marginTop: 12 }}><IconCrown size={19} /><div style={{ flex: 1 }}><strong>{isChinese ? '待确认的所有权转移' : 'Pending ownership transfer'}</strong><br /><small>{pending.proposed_owner_user_id} · {new Date(pending.expires_at).toLocaleString()}</small></div>{owner && <button type="button" className="btn btn-ghost" disabled={busy === 'cancel'} onClick={() => void cancelTransfer()}>{isChinese ? '取消' : 'Cancel'}</button>}</div>}
            <div className="console-grid" style={{ marginTop: 16 }}>
                {owner ? (
                    <section className="console-card console-card--half">
                        <h2><IconCrown size={18} /> {isChinese ? '转移公司所有权' : 'Transfer company ownership'}</h2>
                        <p>{isChinese ? '目标必须是已验证、有效的本公司成员。发起后 24 小时内由目标确认，原所有者自动降为 org_admin。' : 'The target must be a verified active member. They confirm within 24 hours; the prior owner becomes org_admin.'}</p>
                        <form className="console-form" style={{ marginTop: 16 }} onSubmit={requestTransfer}>
                            <label>{isChinese ? '新所有者' : 'New owner'}<select value={targetId} onChange={(event) => setTargetId(event.target.value)} required><option value="">{isChinese ? '选择有效成员' : 'Select an active member'}</option>{candidates.map((member) => <option key={member.id} value={member.id}>{member.display_name || member.email || member.id} · {member.role}</option>)}</select></label>
                            <label>{isChinese ? '当前密码（二次认证）' : 'Current password (reauthentication)'}<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required autoComplete="current-password" /></label>
                            <button type="submit" className="btn btn-primary" disabled={busy === 'transfer' || !targetId || !password}>{busy === 'transfer' ? '…' : (isChinese ? '发起转移' : 'Request transfer')}</button>
                        </form>
                    </section>
                ) : (
                    <section className="console-card console-card--half"><h2>{isChinese ? '退出当前公司' : 'Leave current company'}</h2><p>{isChinese ? '退出属于账户级公司关系流程。请前往“公司与邀请”，由服务端检查 Agent 所有权、审批、待办、交付物和个人凭证后再确认。' : 'Leaving is an account-level company relationship flow. Open Company & invitations for the server-side review of Agent ownership, approvals, work, deliverables, and personal credentials.'}</p><button type="button" className="btn btn-secondary" style={{ marginTop: 16 }} onClick={leaveCompany}>{isChinese ? '打开退出检查' : 'Open leave review'}</button></section>
                )}
                <section className="console-card console-card--half"><h2>{isChinese ? '成员确认入口' : 'Target confirmation'}</h2><p>{isChinese ? '被选中的新所有者不一定是管理员，因此确认入口位于员工工作面的“公司与邀请”，不会要求先获得公司管理权限。' : 'The proposed owner may not be an admin, so confirmation lives under Company & invitations in the work surface.'}</p><button type="button" className="btn btn-secondary" style={{ marginTop: 16 }} onClick={() => navigate('/account/companies')}>{isChinese ? '打开确认入口' : 'Open confirmation entry'}</button></section>
            </div>
            {owner && <section className="console-card console-card--wide console-danger-zone" style={{ marginTop: 24 }}><h2><IconAlertTriangle size={18} /> {isChinese ? '可恢复删除公司' : 'Recoverable company deletion'}</h2><p>{isChinese ? `输入公司名“${companyName}”并再次认证。提交后公司立即停用，但 30 天内可以恢复；这里不执行即时物理删除。` : `Type “${companyName}” and reauthenticate. The company is suspended immediately but recoverable for 30 days; this does not destroy data immediately.`}</p><form className="console-form" style={{ marginTop: 16 }} onSubmit={deleteCompany}><div className="console-form-row"><label>{isChinese ? '确认公司名' : 'Confirm company name'}<input value={deleteName} onChange={(event) => setDeleteName(event.target.value)} required /></label><label>{isChinese ? '当前密码' : 'Current password'}<input type="password" value={deletePassword} onChange={(event) => setDeletePassword(event.target.value)} required autoComplete="current-password" /></label></div><button type="submit" className="btn btn-danger" disabled={busy === 'delete' || deleteName !== companyName || !deletePassword}><IconTrash size={15} /> {busy === 'delete' ? '…' : (isChinese ? '进入可恢复停用期' : 'Schedule recoverable deletion')}</button></form></section>}
        </>
    );
}

export default function CompanyAdmin() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const location = useLocation();
    const user = useAuthStore((state) => state.user);
    const isChinese = i18n.language.startsWith('zh');
    const tenantId = user?.tenant_id || '';
    const section = location.pathname.split('/')[2] || 'overview';
    const settingsView = location.pathname.split('/')[3] || '';
    const integrationView = INTEGRATION_VIEWS.includes(settingsView as IntegrationView)
        ? settingsView as IntegrationView
        : 'tools';
    const tenantQuery = useQuery({ queryKey: ['company-console-tenant', tenantId], queryFn: () => tenantApi.me(), enabled: !!tenantId });
    const membersQuery = useQuery({ queryKey: ['company-governance-members', tenantId], queryFn: () => membershipApi.list() as Promise<CompanyMember[]>, enabled: !!tenantId && hasEffectiveCapability(user, 'company.members.view') });
    const companyName = tenantQuery.data?.name || (isChinese ? '当前公司' : 'Current company');

    const allItems: Array<ProductConsoleNavItem & { capability?: string; ownerOnly?: boolean }> = [
        { to: '/company-admin', exact: true, label: isChinese ? '概览' : 'Overview', icon: <IconHome size={17} /> },
        { to: '/company-admin/members', label: isChinese ? '成员与邀请' : 'Members & invitations', icon: <IconUsers size={17} />, capability: 'company.members.view' },
        { to: '/company-admin/agents', label: isChinese ? 'Agent 员工治理' : 'Agent governance', icon: <IconRobot size={17} />, capability: 'agent.manage.company' },
        { to: '/company-admin/approvals', label: isChinese ? '权限与审批' : 'Permissions & approvals', icon: <IconChecklist size={17} />, capability: 'company.settings.manage' },
        { to: '/company-admin/integrations', label: isChinese ? '企业知识与集成' : 'Knowledge & integrations', icon: <IconPlugConnected size={17} />, capability: 'company.settings.manage' },
        { to: '/company-admin/billing', label: isChinese ? '套餐、账单与用量' : 'Plan, billing & usage', icon: <IconReceipt size={17} />, capability: 'company.billing.view' },
        { to: '/company-admin/market', label: isChinese ? '购买套餐与额度' : 'Buy plans & credits', icon: <IconShoppingCart size={17} />, capability: 'company.billing.manage' },
        { to: '/company-admin/audit', label: isChinese ? '审计日志' : 'Audit log', icon: <IconFileAnalytics size={17} />, capability: 'company.audit.view' },
        { to: '/company-admin/settings', label: isChinese ? '公司设置' : 'Company settings', icon: <IconSettings size={17} />, capability: 'company.settings.manage' },
        { to: '/company-admin/ownership', label: isChinese ? '所有权与删除' : 'Ownership & deletion', icon: <IconCrown size={17} />, capability: 'company.ownership.transfer' },
    ];
    const items = allItems.filter((item) => !item.capability || hasEffectiveCapability(user, item.capability));
    const validSections = new Set(items.map((item) => item.to.split('/')[2] || 'overview'));

    useEffect(() => {
        if (!validSections.has(section)) navigate('/company-admin', { replace: true });
    }, [navigate, section, validSections]);

    useEffect(() => {
        if (section === 'integrations' && settingsView && !INTEGRATION_VIEWS.includes(settingsView as IntegrationView)) {
            navigate('/company-admin/integrations/tools', { replace: true });
        }
    }, [navigate, section, settingsView]);

    const content = useMemo(() => {
        if (section === 'members') return <CompanyMembers tenantId={tenantId} />;
        if (section === 'agents') return <AgentGovernance />;
        if (section === 'settings' && settingsView === 'okr') {
            return <>
                <PageHeader
                    title={isChinese ? '目标、复盘与公司 CEO' : 'Objectives, reviews, and Company CEO'}
                    description={isChinese
                        ? '在公司治理边界内配置 OKR 节奏与唯一的公司 CEO；不会新增左侧主导航，也不会把 CEO 设置混入企业知识与集成。'
                        : 'Configure OKR cadence and the single Company CEO inside company governance without adding a primary navigation item or mixing CEO settings into integrations.'}
                />
                <EnterpriseSettings initialTab="okr" embedded />
            </>;
        }
        if (section === 'settings') return <CompanyPolicySettings tenantId={tenantId} tenant={tenantQuery.data} />;
        if (section === 'billing') return <><PageHeader title={isChinese ? '套餐、账单与用量' : 'Plan, billing and usage'} description={isChinese ? '公司管理员查看聚合用量；支付主体、订单与续费仍按 company.billing.manage 单独守卫。' : 'Company admins see aggregate usage; payer, order, and renewal actions remain gated by company.billing.manage.'} /><SubscriptionDetail /></>;
        if (section === 'market') return <><PageHeader title={isChinese ? '购买套餐与额度' : 'Buy plans and credits'} description={isChinese ? '选择适合团队的套餐或补充额度包；页面会根据当前支付配置显示在线支付或人工订单流程。' : 'Pick a plan or top up credits; the page shows either online checkout or an offline order based on the active billing configuration.'} /><SubscriptionTab /></>;
        if (section === 'ownership') return <CompanyOwnership tenantId={tenantId} companyName={companyName} members={membersQuery.data || []} />;
        if (section === 'integrations') {
            const integrationTabs: Array<{ key: IntegrationView; label: string }> = [
                { key: 'tools', label: isChinese ? '工具与 MCP' : 'Tools & MCP' },
                { key: 'skills', label: isChinese ? '技能库' : 'Skills' },
                { key: 'org', label: isChinese ? '组织同步' : 'Organization sync' },
                { key: 'douyin', label: isChinese ? '外部账号' : 'External accounts' },
            ];
            return <>
                <PageHeader
                    title={isChinese ? '企业知识与集成' : 'Knowledge & integrations'}
                    description={isChinese
                        ? '管理公司级工具、技能、组织目录同步和外部账号；未开通的 Provider 会保留可解释的关闭态。'
                        : 'Manage company tools, skills, directory synchronization, and external accounts. Unavailable providers remain visible with an explicit closed state.'}
                />
                <nav className="tabs" role="tablist" aria-label={isChinese ? '企业知识与集成二级功能' : 'Knowledge and integrations sections'}>
                    {integrationTabs.map((tab) => (
                        <button
                            key={tab.key}
                            type="button"
                            role="tab"
                            aria-selected={integrationView === tab.key}
                            className={`tab ${integrationView === tab.key ? 'active' : ''}`}
                            onClick={() => navigate(`/company-admin/integrations/${tab.key}`)}
                        >
                            {tab.label}
                        </button>
                    ))}
                </nav>
                <EnterpriseSettings key={integrationView} initialTab={integrationView} embedded />
            </>;
        }
        const legacyTabs: Record<string, 'approvals' | 'audit'> = { approvals: 'approvals', audit: 'audit' };
        if (legacyTabs[section]) return <><PageHeader title={items.find((item) => item.to.endsWith(section))?.label || section} description={isChinese ? '该能力继续复用已验证的企业设置数据源，并置于新的公司治理边界内。' : 'This capability reuses the existing enterprise data source inside the new company-governance boundary.'} /><EnterpriseSettings key={section} initialTab={legacyTabs[section]} embedded /></>;
        return <CompanyOverview companyName={companyName} />;
    }, [companyName, integrationView, isChinese, items, membersQuery.data, navigate, section, settingsView, tenantId, tenantQuery.data]);

    return (
        <ProductConsoleShell
            kind="company"
            title={companyName}
            subtitle={user?.membership_role === 'org_owner' ? (isChinese ? '公司所有者' : 'Company owner') : (isChinese ? '公司管理员' : 'Company administrator')}
            navLabel={isChinese ? '公司管理二级导航' : 'Company administration navigation'}
            items={items}
            backTo="/work"
            backLabel={isChinese ? '返回员工工作面' : 'Back to work'}
            headerActions={<>{isPlatformOperator(user) && <button type="button" className="btn btn-ghost" onClick={() => navigate('/admin/platform')}>{isChinese ? '平台运营' : 'Platform operations'}</button>}<button type="button" className="btn btn-secondary" onClick={() => navigate('/account/companies')}>{isChinese ? '公司与邀请' : 'Companies & invitations'}</button></>}
        >
            {content}
        </ProductConsoleShell>
    );
}

export function CompanyAdminGuardFallback() {
    const user = useAuthStore((state) => state.user);
    return hasProductSurface(user, 'company_admin') ? null : <div />;
}
