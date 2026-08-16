import {
    IconActivityHeartbeat,
    IconBuilding,
    IconDatabase,
    IconFileAnalytics,
    IconHome,
    IconKey,
    IconLifebuoy,
    IconMail,
    IconReceipt,
    IconRoute,
    IconShieldCheck,
    IconStack2,
    IconUserCheck,
    IconWallet,
} from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';

import ProductConsoleShell, { type ProductConsoleNavItem } from '../components/ProductConsoleShell';
import { adminApi, authApi, governanceApi } from '../services/api';
import { useAuthStore } from '../stores';
import { hasEffectiveCapability, hasProductSurface } from '../utils/productAccess';
import { normalizeTenantRedirectUrl } from '../utils/authTransport';
import { commitSameOriginTenantSwitch, validateTenantSwitchCandidate } from '../utils/tenantSwitch';
import SaasAdmin from './SaasAdmin';
import PlatformSystemEmail from './PlatformSystemEmail';

type CompanySummary = {
    id: string;
    name: string;
    slug?: string;
    is_active: boolean;
    user_count?: number;
    agent_count?: number;
    agent_running_count?: number;
    total_tokens?: number;
    created_at?: string;
};

const messageFrom = (error: unknown) => error instanceof Error ? error.message : String(error);

function PageHeader({ title, description, actions }: { title: string; description: string; actions?: React.ReactNode }) {
    return <header className="console-page-header"><div><h1>{title}</h1><p>{description}</p></div>{actions && <div className="console-row-actions">{actions}</div>}</header>;
}

function PlatformOverview({ companies }: { companies: CompanySummary[] }) {
    const { i18n } = useTranslation();
    const isChinese = i18n.language.startsWith('zh');
    const activeCompanies = companies.filter((company) => company.is_active);
    const users = companies.reduce((sum, company) => sum + (company.user_count || 0), 0);
    const agents = companies.reduce((sum, company) => sum + (company.agent_count || 0), 0);
    return (
        <>
            <PageHeader title={isChinese ? '平台运营概览' : 'Platform operations'} description={isChinese ? '全局租户、注册、计费与 Provider 的运营控制面。这里不显示公司私人助理、数字员工、Group 或业务消息。' : 'Global operations for tenants, registration, billing, and providers. Company assistants, employees, Groups, and business messages are excluded.'} />
            <div className="console-inline-notice"><IconShieldCheck size={19} /><span>{isChinese ? '平台运营权与公司成员关系相互独立。进入公司工作面必须切换到真实 membership；支持会话只开放所选最小诊断范围。' : 'Platform authority and company membership are independent. Entering work requires a real membership; support sessions grant only selected diagnostic scope.'}</span></div>
            <div className="console-grid" style={{ marginTop: 16 }}>
                <section className="console-card"><h2>{isChinese ? '有效公司' : 'Active companies'}</h2><strong className="console-card__metric">{activeCompanies.length}</strong><p>{isChinese ? `全部 ${companies.length} 家` : `${companies.length} total`}</p></section>
                <section className="console-card"><h2>{isChinese ? '成员关系' : 'Memberships'}</h2><strong className="console-card__metric">{users}</strong><p>{isChinese ? '跨公司汇总，不包含身份私密内容' : 'Cross-company aggregate; no private identity content'}</p></section>
                <section className="console-card"><h2>Agent</h2><strong className="console-card__metric">{agents}</strong><p>{isChinese ? '仅运营汇总，不授予 Agent 使用权' : 'Operational aggregate only; no Agent access granted'}</p></section>
            </div>
        </>
    );
}

function PlatformCompanies({ companies, loading }: { companies: CompanySummary[]; loading: boolean }) {
    const { i18n } = useTranslation();
    const queryClient = useQueryClient();
    const isChinese = i18n.language.startsWith('zh');
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [holdTypes, setHoldTypes] = useState<Record<string, 'legal' | 'operations'>>({});
    const [holdReasons, setHoldReasons] = useState<Record<string, string>>({});
    const [releaseReasons, setReleaseReasons] = useState<Record<string, string>>({});
    const deletionsQuery = useQuery({
        queryKey: ['platform-tenant-deletions'],
        queryFn: () => adminApi.listTenantDeletions(),
        refetchInterval: 30_000,
    });
    const deletionItems = deletionsQuery.data?.items || [];
    const deletionByTenant = new Map(deletionItems.map((item) => [item.tenant_id, item]));
    const refresh = () => queryClient.invalidateQueries({ queryKey: ['platform-tenant-deletions'] });
    const run = async (key: string, action: () => Promise<unknown>) => {
        setBusy(key); setError('');
        try { await action(); await refresh(); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };
    const createHold = (tenantId: string) => {
        const reasonCode = (holdReasons[tenantId] || '').trim();
        if (!reasonCode) return;
        return run(`hold:${tenantId}`, () => adminApi.createTenantDeletionHold(tenantId, {
            hold_type: holdTypes[tenantId] || 'operations',
            reason_code: reasonCode,
        }));
    };
    const releaseHold = (tenantId: string, holdId: string) => {
        const reasonCode = (releaseReasons[holdId] || '').trim();
        if (!reasonCode) return;
        return run(`release:${holdId}`, () => adminApi.releaseTenantDeletionHold(tenantId, holdId, reasonCode));
    };
    return (
        <>
            <PageHeader title={isChinese ? '租户与生命周期' : 'Tenants and lifecycle'} description={isChinese ? '公司先进入 30 天可恢复停用；到期后才进入独立的清理队列。平台运营者不会因这张列表获得公司内容或 Agent 权限。' : 'A company first enters a 30-day recoverable suspension, then a separate purge queue. This list never grants company content or Agent access.'} />
            <div className="console-inline-notice"><IconShieldCheck size={19} /><span>{isChinese ? '浏览器只提供无删除的 dry-run、法务/运营暂停和解除暂停。物理清理由受控执行器处理，不暴露网页按钮。' : 'The browser exposes only non-destructive dry-run, legal/operations holds, and hold release. Physical purge belongs to a controlled runner and has no web button.'}</span></div>
            {error && <div className="console-inline-notice console-inline-notice--error" role="alert">{error}</div>}
            <div className="console-table-wrap" style={{ marginTop: 16 }}><table className="console-table"><thead><tr><th>{isChinese ? '公司' : 'Company'}</th><th>{isChinese ? '状态' : 'Status'}</th><th>{isChinese ? '清理阶段' : 'Purge stage'}</th><th>{isChinese ? '成员' : 'Members'}</th><th>Agent</th><th>{isChinese ? '创建时间' : 'Created'}</th></tr></thead><tbody>{companies.map((company) => { const deletion = deletionByTenant.get(company.id); return <tr key={company.id}><td><strong>{company.name}</strong><br /><small>{company.id}</small></td><td><span className={`console-status ${company.is_active ? 'console-status--active' : 'console-status--danger'}`}>{company.is_active ? (isChinese ? '有效' : 'Active') : (isChinese ? '可恢复停用' : 'Recoverable suspension')}</span></td><td>{deletion ? <><span className="console-status">{deletion.job_status}</span><br /><small>{deletion.is_due ? (isChinese ? '已到期' : 'Due') : `${isChinese ? '到期' : 'Eligible'} ${new Date(deletion.eligible_at).toLocaleString()}`}</small></> : '-'}</td><td>{company.user_count || 0}</td><td>{company.agent_count || 0}</td><td>{company.created_at ? new Date(company.created_at).toLocaleDateString() : '-'}</td></tr>; })}</tbody></table>{!loading && companies.length === 0 && <div className="console-empty">{isChinese ? '暂无公司' : 'No companies'}</div>}</div>

            <section style={{ marginTop: 24 }} data-testid="tenant-purge-queue">
                <h2 style={{ fontSize: 16 }}>{isChinese ? '到期清理队列' : 'Expired tenant purge queue'}</h2>
                <p style={{ color: 'var(--text-secondary)', marginTop: 6 }}>{isChinese ? 'reason_code 只记录案件编号或标准分类，不要填写姓名、邮箱、消息内容或凭证。' : 'Use reason_code only for a case ID or standard category; never enter names, emails, message content, or credentials.'}</p>
                <div className="console-stack" style={{ marginTop: 14 }}>
                    {deletionItems.map((item) => <section className="console-card console-card--wide" key={item.tenant_id}>
                        <div className="console-page-header"><div><h2>{item.tenant_name}</h2><p>{item.tenant_id} · {item.is_due ? (isChinese ? '恢复窗口已结束' : 'Restore window elapsed') : `${isChinese ? '可恢复至' : 'Recoverable until'} ${new Date(item.eligible_at).toLocaleString()}`}</p></div><span className={`console-status ${item.job_status === 'failed' ? 'console-status--danger' : item.job_status === 'dry_run_passed' ? 'console-status--active' : ''}`}>{item.job_status}</span></div>
                        {item.last_error_code && <div className="console-inline-notice console-inline-notice--error" role="alert">{item.last_error_code}</div>}
                        <div className="console-row-actions" style={{ marginTop: 12 }}>
                            <button type="button" className="btn btn-secondary" disabled={!item.is_due || item.holds.length > 0 || busy === `dry:${item.tenant_id}`} onClick={() => void run(`dry:${item.tenant_id}`, () => adminApi.dryRunTenantDeletion(item.tenant_id))}>{busy === `dry:${item.tenant_id}` ? '…' : (isChinese ? '执行无删除 dry-run' : 'Run non-destructive dry-run')}</button>
                            {item.plan_digest && <code className="console-code">plan {item.plan_digest.slice(0, 12)}…</code>}
                            <span className="console-status">{isChinese ? '尝试' : 'Attempts'} {item.attempt_count}</span>
                        </div>
                        <div className="console-form-row" style={{ marginTop: 14 }}>
                            <label>{isChinese ? '暂停类型' : 'Hold type'}<select value={holdTypes[item.tenant_id] || 'operations'} onChange={(event) => setHoldTypes((current) => ({ ...current, [item.tenant_id]: event.target.value as 'legal' | 'operations' }))}><option value="operations">operations</option><option value="legal">legal</option></select></label>
                            <label>{isChinese ? '暂停 reason_code' : 'Hold reason_code'}<input value={holdReasons[item.tenant_id] || ''} pattern="[a-z0-9][a-z0-9_.-]+" placeholder="case.legal.123" onChange={(event) => setHoldReasons((current) => ({ ...current, [item.tenant_id]: event.target.value }))} /></label>
                            <button type="button" className="btn btn-secondary" disabled={(holdReasons[item.tenant_id] || '').trim().length < 3 || busy === `hold:${item.tenant_id}`} onClick={() => void createHold(item.tenant_id)}>{isChinese ? '添加暂停' : 'Add hold'}</button>
                        </div>
                        {item.holds.map((hold) => <div className="console-form-row" style={{ marginTop: 10 }} key={hold.id}><div><span className="console-status console-status--danger">{hold.hold_type}</span> <code>{hold.reason_code}</code></div><label>{isChinese ? '解除 reason_code' : 'Release reason_code'}<input value={releaseReasons[hold.id] || ''} pattern="[a-z0-9][a-z0-9_.-]+" placeholder="case.review.complete" onChange={(event) => setReleaseReasons((current) => ({ ...current, [hold.id]: event.target.value }))} /></label><button type="button" className="btn btn-ghost" disabled={(releaseReasons[hold.id] || '').trim().length < 3 || busy === `release:${hold.id}`} onClick={() => void releaseHold(item.tenant_id, hold.id)}>{isChinese ? '解除暂停' : 'Release hold'}</button></div>)}
                    </section>)}
                    {!deletionsQuery.isLoading && deletionItems.length === 0 && <div className="console-empty">{isChinese ? '没有待清理公司' : 'No tenants awaiting purge'}</div>}
                </div>
            </section>

            {(deletionsQuery.data?.tombstones || []).length > 0 && <section style={{ marginTop: 24 }} data-testid="tenant-purge-receipts"><h2 style={{ fontSize: 16 }}>{isChinese ? '最小化清理回执' : 'Minimal purge receipts'}</h2><div className="console-table-wrap"><table className="console-table"><thead><tr><th>Tenant ID</th><th>{isChinese ? '清理时间' : 'Purged'}</th><th>{isChinese ? '行数' : 'Rows'}</th><th>{isChinese ? '回执' : 'Receipt'}</th></tr></thead><tbody>{(deletionsQuery.data?.tombstones || []).map((receipt) => <tr key={receipt.tenant_id}><td><code>{receipt.tenant_id}</code></td><td>{new Date(receipt.purged_at).toLocaleString()}</td><td>{receipt.rows_total}</td><td><code>{receipt.receipt_hash.slice(0, 16)}…</code></td></tr>)}</tbody></table></div></section>}
        </>
    );
}

function RegistrationGrants() {
    const { i18n } = useTranslation();
    const queryClient = useQueryClient();
    const isChinese = i18n.language.startsWith('zh');
    const [count, setCount] = useState(1);
    const [maxUses, setMaxUses] = useState(1);
    const [expiresDays, setExpiresDays] = useState(30);
    const [issued, setIssued] = useState<any[]>([]);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const grantsQuery = useQuery({ queryKey: ['platform-registration-grants'], queryFn: () => governanceApi.registrationGrants() });

    const refresh = () => queryClient.invalidateQueries({ queryKey: ['platform-registration-grants'] });
    const create = async (event: React.FormEvent) => {
        event.preventDefault(); setBusy('create'); setError(''); setIssued([]);
        try { const result = await governanceApi.createRegistrationGrants({ count, max_uses: maxUses, expires_in_days: expiresDays }); setIssued(result.items || []); await refresh(); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };
    const revoke = async (id: string) => {
        setBusy(id); setError('');
        try { await governanceApi.revokeRegistrationGrant(id); await refresh(); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };
    return (
        <>
            <PageHeader title={isChinese ? '平台注册授权' : 'Platform registration grants'} description={isChinese ? 'RegistrationGrant 只允许创建全局账号，不绑定任何公司、成员角色或管理员权限。公司邀请在公司管理面单独处理。' : 'A RegistrationGrant only allows global account creation. It never binds a company, membership role, or admin authority. Company invitations are separate.'} />
            {error && <div className="console-inline-notice console-inline-notice--error" role="alert">{error}</div>}
            {issued.length > 0 && <div className="console-card console-card--wide" style={{ marginTop: 16 }}><h2>{isChinese ? '新凭证（仅本次完整显示）' : 'New grants (full token shown once)'}</h2><div className="console-stack" style={{ marginTop: 12 }}>{issued.map((grant) => <div key={grant.id} className="console-inline-notice console-inline-notice--success"><code className="console-code" style={{ flex: 1 }}>{grant.token}</code><button type="button" className="btn btn-secondary" onClick={() => void navigator.clipboard.writeText(grant.token)}>{isChinese ? '复制' : 'Copy'}</button></div>)}</div></div>}
            <section className="console-card console-card--wide" style={{ marginTop: 16 }}><h2>{isChinese ? '签发平台注册凭证' : 'Issue registration grants'}</h2><form className="console-form" style={{ marginTop: 14 }} onSubmit={create}><div className="console-form-row"><label>{isChinese ? '数量' : 'Count'}<input type="number" min={1} max={100} value={count} onChange={(event) => setCount(Number(event.target.value))} /></label><label>{isChinese ? '每个最大使用次数' : 'Max uses per grant'}<input type="number" min={1} max={1000} value={maxUses} onChange={(event) => setMaxUses(Number(event.target.value))} /></label></div><label>{isChinese ? '有效天数' : 'Valid days'}<input type="number" min={1} max={365} value={expiresDays} onChange={(event) => setExpiresDays(Number(event.target.value))} /></label><button type="submit" className="btn btn-primary" disabled={busy === 'create'}>{busy === 'create' ? '…' : (isChinese ? '签发凭证' : 'Issue grants')}</button></form></section>
            <section style={{ marginTop: 24 }}><h2 style={{ fontSize: 16 }}>{isChinese ? '凭证记录' : 'Grant records'}</h2><div className="console-table-wrap"><table className="console-table"><thead><tr><th>{isChinese ? '前缀' : 'Prefix'}</th><th>{isChinese ? '使用量' : 'Usage'}</th><th>{isChinese ? '状态' : 'Status'}</th><th>{isChinese ? '有效期' : 'Expires'}</th><th>{isChinese ? '操作' : 'Action'}</th></tr></thead><tbody>{(grantsQuery.data?.items || []).map((grant: any) => <tr key={grant.id}><td><code>{grant.token_prefix}…</code>{grant.legacy && <small> legacy</small>}</td><td>{grant.used_count}/{grant.max_uses}</td><td><span className={`console-status ${grant.status === 'active' ? 'console-status--active' : ''}`}>{grant.status}</span></td><td>{grant.expires_at ? new Date(grant.expires_at).toLocaleString() : (isChinese ? '永久' : 'Never')}</td><td>{grant.status === 'active' && <button type="button" className="btn btn-ghost" disabled={busy === grant.id} onClick={() => void revoke(grant.id)}>{isChinese ? '撤销' : 'Revoke'}</button>}</td></tr>)}</tbody></table>{!grantsQuery.isLoading && (grantsQuery.data?.items || []).length === 0 && <div className="console-empty">{isChinese ? '暂无平台注册凭证' : 'No registration grants'}</div>}</div></section>
        </>
    );
}

const SUPPORT_SCOPES = ['tenant.metadata.read', 'tenant.diagnostics.read', 'tenant.lifecycle.manage'];

function SupportSessions({ companies }: { companies: CompanySummary[] }) {
    const { i18n } = useTranslation();
    const { user, setUser } = useAuthStore();
    const isChinese = i18n.language.startsWith('zh');
    const [tenantId, setTenantId] = useState('');
    const [reason, setReason] = useState('');
    const [duration, setDuration] = useState(30);
    const [scopes, setScopes] = useState<string[]>(['tenant.metadata.read']);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');

    const refreshIdentity = async () => setUser(await authApi.me());
    const create = async (event: React.FormEvent) => {
        event.preventDefault(); setBusy('create'); setError('');
        try { await governanceApi.createSupportSession({ tenant_id: tenantId, reason: reason.trim(), scopes, duration_minutes: duration }); await refreshIdentity(); setReason(''); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };
    const end = async () => {
        const id = String(user?.current_support_session?.id || '');
        if (!id) return; setBusy('end'); setError('');
        try { await governanceApi.endSupportSession(id); await refreshIdentity(); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };
    const toggleScope = (scope: string) => setScopes((current) => current.includes(scope) ? current.filter((item) => item !== scope) : [...current, scope]);
    const current = user?.current_support_session;
    const currentSessionId = String(current?.id || '');
    const currentTenantId = String(current?.tenant_id || '');
    const summaryQuery = useQuery({
        queryKey: ['platform-support-tenant-summary', currentSessionId, currentTenantId],
        queryFn: () => governanceApi.supportTenantSummary(currentSessionId, currentTenantId),
        enabled: Boolean(currentSessionId && currentTenantId),
        retry: false,
        refetchInterval: currentSessionId ? 30_000 : false,
    });
    return (
        <>
            <PageHeader title={isChinese ? '受审计支持会话' : 'Audited support sessions'} description={isChinese ? '选择公司、工单原因、最小范围和过期时间。支持会话不授予私人助理、私人 Workspace、附件或消息访问权。' : 'Choose a company, ticket reason, least privilege scope, and expiry. Support never grants access to personal assistants, private Workspace files, attachments, or messages.'} />
            {error && <div className="console-inline-notice console-inline-notice--error" role="alert">{error}</div>}
            {current ? (
                <section className="console-card console-card--wide">
                    <h2><IconLifebuoy size={18} /> {isChinese ? '支持模式进行中' : 'Support mode active'}</h2>
                    <p>{current.reason}</p>
                    <div className="console-row-actions" style={{ marginTop: 12 }}>
                        {(current.scopes || []).map((scope: string) => <span className="console-status console-status--active" key={scope}>{scope}</span>)}
                        <span className="console-status">{new Date(current.expires_at).toLocaleString()}</span>
                    </div>
                    {summaryQuery.isLoading && <div className="console-empty">{isChinese ? '正在读取支持范围内的诊断摘要…' : 'Loading scoped diagnostic summary…'}</div>}
                    {summaryQuery.error && <div className="console-inline-notice console-inline-notice--error" role="alert">{messageFrom(summaryQuery.error)}</div>}
                    {summaryQuery.data && (
                        <div className="console-grid" style={{ marginTop: 16 }} data-testid="support-tenant-summary">
                            {summaryQuery.data.metadata && (
                                <section className="console-card">
                                    <h3>{isChinese ? '公司元数据' : 'Company metadata'}</h3>
                                    <strong>{summaryQuery.data.metadata.name}</strong>
                                    <p>{summaryQuery.data.metadata.is_active ? (isChinese ? '有效' : 'Active') : (isChinese ? '已停用' : 'Inactive')} · {summaryQuery.data.metadata.timezone}</p>
                                </section>
                            )}
                            {summaryQuery.data.diagnostics && (
                                <section className="console-card">
                                    <h3>{isChinese ? '聚合诊断' : 'Aggregate diagnostics'}</h3>
                                    <p>{isChinese ? '有效成员' : 'Active memberships'}：{summaryQuery.data.diagnostics.memberships_active}/{summaryQuery.data.diagnostics.memberships_total}</p>
                                    <p>{isChinese ? '有效 Agent' : 'Active Agents'}：{summaryQuery.data.diagnostics.agents_active}/{summaryQuery.data.diagnostics.agents_total}</p>
                                </section>
                            )}
                        </div>
                    )}
                    <p style={{ marginTop: 14, fontSize: 12 }}>{isChinese ? '该摘要仅包含公司元数据和聚合计数；不返回成员身份明细、Agent 内容、消息、附件或 Workspace 文件。' : 'This summary contains only tenant metadata and aggregate counts; it excludes member identity details, Agent content, messages, attachments, and Workspace files.'}</p>
                    <button type="button" className="btn btn-secondary" style={{ marginTop: 16 }} disabled={busy === 'end'} onClick={() => void end()}>{isChinese ? '结束支持会话' : 'End support session'}</button>
                </section>
            ) : (
                <section className="console-card console-card--wide">
                    <h2>{isChinese ? '创建支持会话' : 'Create support session'}</h2>
                    <form className="console-form" style={{ marginTop: 14 }} onSubmit={create}>
                        <label>{isChinese ? '目标公司' : 'Target company'}<select value={tenantId} onChange={(event) => setTenantId(event.target.value)} required><option value="">{isChinese ? '选择公司' : 'Select a company'}</option>{companies.map((company) => <option key={company.id} value={company.id}>{company.name} · {company.id}</option>)}</select></label>
                        <label>{isChinese ? '工单或支持原因（至少 10 字）' : 'Ticket or support reason (at least 10 characters)'}<textarea value={reason} onChange={(event) => setReason(event.target.value)} required minLength={10} /></label>
                        <div><span style={{ display: 'block', marginBottom: 8, fontSize: 12, color: 'var(--text-secondary)' }}>{isChinese ? '最小访问范围' : 'Least-privilege scopes'}</span><div className="console-row-actions">{SUPPORT_SCOPES.map((scope) => <label key={scope} className="console-status"><input type="checkbox" checked={scopes.includes(scope)} onChange={() => toggleScope(scope)} /> {scope}</label>)}</div></div>
                        <label>{isChinese ? '有效分钟（5–60）' : 'Duration in minutes (5–60)'}<input type="number" min={5} max={60} value={duration} onChange={(event) => setDuration(Number(event.target.value))} /></label>
                        <button type="submit" className="btn btn-primary" disabled={busy === 'create' || !tenantId || reason.trim().length < 10 || scopes.length === 0}>{busy === 'create' ? '…' : (isChinese ? '创建并记录审计' : 'Create and audit')}</button>
                    </form>
                </section>
            )}
        </>
    );
}

function OwnershipResolutions() {
    const { i18n } = useTranslation();
    const queryClient = useQueryClient();
    const isChinese = i18n.language.startsWith('zh');
    const [owners, setOwners] = useState<Record<string, string>>({});
    const [reasons, setReasons] = useState<Record<string, string>>({});
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const query = useQuery({ queryKey: ['platform-ownership-resolutions'], queryFn: () => governanceApi.ownershipResolutions() });
    const resolve = async (item: any) => {
        setBusy(item.id); setError('');
        try { await governanceApi.resolveOwnership(item.id, { owner_user_id: owners[item.id], reason: reasons[item.id] }); await queryClient.invalidateQueries({ queryKey: ['platform-ownership-resolutions'] }); }
        catch (nextError) { setError(messageFrom(nextError)); }
        finally { setBusy(''); }
    };
    return (
        <>
            <PageHeader title={isChinese ? '所有权迁移待决清单' : 'Ownership resolution queue'} description={isChinese ? '旧数据中无法确定唯一 owner 的公司必须进入显式待决清单；平台不能静默猜测，也不能覆盖正常公司的 owner 流程。' : 'Legacy companies without an unambiguous owner enter this explicit queue. The platform must not guess or override the normal owner flow.'} />
            {error && <div className="console-inline-notice console-inline-notice--error" role="alert">{error}</div>}
            <div className="console-stack" style={{ marginTop: 16 }}>{(query.data?.items || []).map((item: any) => <section className="console-card console-card--wide" key={item.id}><h2>{item.tenant_name}</h2><p>{item.reason}</p><form className="console-form" style={{ marginTop: 14 }} onSubmit={(event) => { event.preventDefault(); void resolve(item); }}><label>{isChinese ? '从迁移候选中选择 owner' : 'Select owner from migration candidates'}<select value={owners[item.id] || ''} onChange={(event) => setOwners((current) => ({ ...current, [item.id]: event.target.value }))} required><option value="">{isChinese ? '选择候选成员' : 'Select a candidate'}</option>{(item.candidate_user_ids || []).map((id: string) => <option key={id} value={id}>{id}</option>)}</select></label><label>{isChinese ? '人工判断依据（写入审计）' : 'Human resolution rationale (audited)'}<textarea value={reasons[item.id] || ''} onChange={(event) => setReasons((current) => ({ ...current, [item.id]: event.target.value }))} required minLength={5} /></label><button type="submit" className="btn btn-primary" disabled={busy === item.id || !owners[item.id] || (reasons[item.id] || '').trim().length < 5}>{isChinese ? '确认唯一所有者' : 'Confirm unique owner'}</button></form></section>)}{!query.isLoading && (query.data?.items || []).length === 0 && <div className="console-empty">{isChinese ? '没有待处理的 owner 迁移问题' : 'No ownership migrations require resolution'}</div>}</div>
        </>
    );
}

function PlatformTenantSwitcher() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const setAuth = useAuthStore((state) => state.setAuth);
    const isChinese = i18n.language.startsWith('zh');
    const [busy, setBusy] = useState(false);
    const tenantsQuery = useQuery({ queryKey: ['platform-my-memberships'], queryFn: () => authApi.getMyTenants() });
    const switchTenant = async (tenantId: string) => {
        if (!tenantId || busy) return; setBusy(true);
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
        } finally { setBusy(false); }
    };
    if ((tenantsQuery.data || []).length === 0) return null;
    return <select aria-label={isChinese ? '以真实成员关系进入公司' : 'Enter company with a real membership'} value="" disabled={busy} onChange={(event) => void switchTenant(event.target.value)}><option value="">{busy ? (isChinese ? '切换中…' : 'Switching…') : (isChinese ? '进入我的公司工作区' : 'Enter my company workspace')}</option>{(tenantsQuery.data || []).map((tenant: any) => <option key={tenant.tenant_id} value={tenant.tenant_id}>{tenant.tenant_name}</option>)}</select>;
}

export default function PlatformOperations() {
    const { i18n } = useTranslation();
    const location = useLocation();
    const navigate = useNavigate();
    const user = useAuthStore((state) => state.user);
    const isChinese = i18n.language.startsWith('zh');
    const section = location.pathname.split('/')[3] || 'overview';
    const companiesQuery = useQuery({ queryKey: ['platform-company-summaries'], queryFn: () => adminApi.listCompanies() as Promise<CompanySummary[]> });
    const companies = companiesQuery.data || [];
    const items: ProductConsoleNavItem[] = [
        { to: '/admin/platform', exact: true, label: isChinese ? '运营概览' : 'Overview', icon: <IconHome size={17} /> },
        ...(hasEffectiveCapability(user, 'platform.tenants.manage') ? [{ to: '/admin/platform/companies', label: isChinese ? '租户与生命周期' : 'Tenants & lifecycle', icon: <IconBuilding size={17} /> }] : []),
        ...(hasEffectiveCapability(user, 'platform.registration.manage') ? [{ to: '/admin/platform/registration', label: isChinese ? '注册授权' : 'Registration grants', icon: <IconKey size={17} /> }] : []),
        ...(hasEffectiveCapability(user, 'platform.registration.manage') ? [{ to: '/admin/platform/system-email', label: isChinese ? '系统邮件' : 'System email', icon: <IconMail size={17} /> }] : []),
        ...(hasEffectiveCapability(user, 'platform.billing.manage') ? [{ to: '/admin/platform/billing?tab=plans', label: isChinese ? '套餐与 Credits' : 'Plans & Credits', icon: <IconWallet size={17} /> }] : []),
        ...(hasEffectiveCapability(user, 'platform.providers.manage') ? [
            { to: '/admin/platform/providers?tab=accounts', label: isChinese ? 'Provider 账号池' : 'Provider accounts', icon: <IconDatabase size={17} /> },
            { to: '/admin/platform/routes?tab=model-routes', label: isChinese ? '模型与媒体路由' : 'Model & media routes', icon: <IconRoute size={17} /> },
        ] : []),
        ...(hasEffectiveCapability(user, 'platform.support_session.create') ? [{ to: '/admin/platform/support', label: isChinese ? '支持会话' : 'Support sessions', icon: <IconLifebuoy size={17} /> }] : []),
        { to: '/admin/platform/ownership', label: isChinese ? '所有权待决' : 'Ownership queue', icon: <IconUserCheck size={17} /> },
        { to: '/admin/platform/health?tab=production-issues', label: isChinese ? '平台健康与发布' : 'Health & release evidence', icon: <IconActivityHeartbeat size={17} /> },
    ];

    const content = useMemo(() => {
        if (section === 'companies') return <PlatformCompanies companies={companies} loading={companiesQuery.isLoading} />;
        if (section === 'registration') return <RegistrationGrants />;
        if (section === 'system-email') return <PlatformSystemEmail />;
        if (section === 'support') return <SupportSessions companies={companies} />;
        if (section === 'ownership') return <OwnershipResolutions />;
        if (['billing', 'providers', 'routes', 'health'].includes(section)) return <SaasAdmin />;
        return <PlatformOverview companies={companies} />;
    }, [companies, companiesQuery.isLoading, section]);

    return (
        <ProductConsoleShell
            kind="platform"
            title={isChinese ? '平台运营' : 'Platform operations'}
            subtitle={isChinese ? '全局运营权限，不是公司管理员' : 'Global authority, not company administration'}
            navLabel={isChinese ? '平台运营导航' : 'Platform operations navigation'}
            items={items}
            backTo={hasProductSurface(user, 'work') ? '/work' : undefined}
            backLabel={isChinese ? '返回公司工作面' : 'Back to company work'}
            headerActions={<PlatformTenantSwitcher />}
            banner={user?.current_support_session && <div className="product-console__banner"><IconLifebuoy size={18} /><span><strong>{isChinese ? '支持模式' : 'Support mode'}：</strong>{user.current_support_session.reason} · {(user.current_support_session.scopes || []).join(', ')} · {new Date(user.current_support_session.expires_at).toLocaleString()}</span></div>}
        >
            {content}
        </ProductConsoleShell>
    );
}
