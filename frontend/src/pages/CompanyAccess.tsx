import {
    IconArrowRight,
    IconBuilding,
    IconCheck,
    IconClock,
    IconKey,
    IconPlus,
    IconX,
} from '@tabler/icons-react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';

import { AstraWordmark } from '../components/atlas';
import {
    authApi,
    governanceApi,
    tenantApi,
    type PendingOrganizationInvitation,
} from '../services/api';
import { useAuthStore } from '../stores';
import { hasEffectiveCapability, hasProductSurface } from '../utils/productAccess';
import { createRandomUUID } from '../utils/randomUUID';
import { commitSameOriginTenantSwitch } from '../utils/tenantSwitch';
import './productSurfaces.css';

type AcceptedCompany = {
    tenantId: string;
    tenantName: string;
    accessToken: string;
};

export default function CompanyAccess() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const [searchParams] = useSearchParams();
    const { user, setAuth } = useAuthStore();
    const isChinese = i18n.language.startsWith('zh');
    const [pendingInvitations, setPendingInvitations] = useState<PendingOrganizationInvitation[]>([]);
    const [loadingInvitations, setLoadingInvitations] = useState(true);
    const [busyId, setBusyId] = useState<string | null>(null);
    const [companyName, setCompanyName] = useState('');
    const [companyTimezone, setCompanyTimezone] = useState(
        () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    );
    const [companyRegion, setCompanyRegion] = useState(() => isChinese ? 'CN' : '001');
    const [companyAuthorityConfirmed, setCompanyAuthorityConfirmed] = useState(false);
    const [joinToken, setJoinToken] = useState(() => searchParams.get('join_token') || '');
    const [error, setError] = useState('');
    const [acceptedCompany, setAcceptedCompany] = useState<AcceptedCompany | null>(null);
    const createIdempotencyKey = useRef(createRandomUUID());

    const canCreate = hasEffectiveCapability(user, 'company.create');
    const hasWorkspace = hasProductSurface(user, 'work');

    const establishMembership = async (tenantId: string, accessToken: string) => {
        await commitSameOriginTenantSwitch({
            tenantId,
            accessToken,
            validateToken: authApi.me,
            establishAuth: setAuth,
            persistTenantId: (value) => localStorage.setItem('current_tenant_id', value),
            clearTenantId: () => localStorage.removeItem('current_tenant_id'),
            currentTenantId: () => localStorage.getItem('current_tenant_id'),
            resolvedTenantId: (candidate) => candidate.tenant_id,
        });
        queryClient.clear();
    };

    const loadPendingInvitations = async () => {
        setLoadingInvitations(true);
        try {
            const result = await governanceApi.pendingInvitations();
            setPendingInvitations(result.items || []);
        } catch (nextError) {
            setError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setLoadingInvitations(false);
        }
    };

    useEffect(() => {
        void loadPendingInvitations();
    }, []);

    const commitMembership = async (result: any, tenantName?: string) => {
        const tenantId = String(result?.tenant?.id || '');
        const accessToken = String(result?.access_token || '');
        if (!tenantId || !accessToken) throw new Error('Membership response is incomplete');
        if (hasWorkspace) {
            setAcceptedCompany({
                tenantId,
                tenantName: tenantName || result.tenant.name || (isChinese ? '新公司' : 'New company'),
                accessToken,
            });
            await loadPendingInvitations();
            return;
        }
        await establishMembership(tenantId, accessToken);
        navigate('/onboarding?mode=join', { replace: true });
    };

    const switchToAcceptedCompany = async () => {
        if (!acceptedCompany) return;
        setBusyId('switch');
        setError('');
        try {
            await establishMembership(acceptedCompany.tenantId, acceptedCompany.accessToken);
            localStorage.setItem('preferred_product_surface', 'work');
            navigate('/onboarding?mode=join', { replace: true });
        } catch (nextError) {
            setError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setBusyId(null);
        }
    };

    const acceptInvitation = async (invitation: PendingOrganizationInvitation) => {
        setBusyId(invitation.id);
        setError('');
        try {
            const result = await tenantApi.acceptInvitation(invitation.id);
            await commitMembership(result, invitation.tenant_name);
        } catch (nextError) {
            setError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setBusyId(null);
        }
    };

    const declineInvitation = async (invitationId: string) => {
        setBusyId(invitationId);
        setError('');
        try {
            await tenantApi.declineInvitation(invitationId);
            await loadPendingInvitations();
        } catch (nextError) {
            setError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setBusyId(null);
        }
    };

    const joinWithToken = async (event: React.FormEvent) => {
        event.preventDefault();
        setBusyId('join');
        setError('');
        try {
            const result = await tenantApi.join(joinToken.trim());
            setJoinToken('');
            await commitMembership(result);
        } catch (nextError) {
            setError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setBusyId(null);
        }
    };

    const createCompany = async (event: React.FormEvent) => {
        event.preventDefault();
        setBusyId('create');
        setError('');
        try {
            const result = await tenantApi.selfCreate(
                {
                    name: companyName.trim(),
                    timezone: companyTimezone,
                    country_region: companyRegion,
                },
                createIdempotencyKey.current,
            );
            const accessToken = String(result?.access_token || '');
            const tenantId = String(result?.tenant?.id || '');
            await establishMembership(tenantId, accessToken);
            localStorage.setItem('preferred_product_surface', 'work');
            createIdempotencyKey.current = createRandomUUID();
            navigate('/onboarding?mode=create', { replace: true });
        } catch (nextError) {
            setError(nextError instanceof Error ? nextError.message : String(nextError));
        } finally {
            setBusyId(null);
        }
    };

    const invitationSummary = useMemo(() => {
        if (loadingInvitations) return isChinese ? '正在读取邀请…' : 'Loading invitations…';
        if (pendingInvitations.length === 0) return isChinese ? '暂无待处理邀请' : 'No pending invitations';
        return isChinese
            ? `${pendingInvitations.length} 个待处理邀请`
            : `${pendingInvitations.length} pending invitation${pendingInvitations.length === 1 ? '' : 's'}`;
    }, [isChinese, loadingInvitations, pendingInvitations.length]);

    return (
        <main className="company-access">
            <header className="company-access__topbar">
                <AstraWordmark height={23} variant="ui" />
                {hasWorkspace && (
                    <button type="button" className="btn btn-secondary" onClick={() => navigate('/work')}>
                        {isChinese ? '返回当前公司' : 'Back to current company'}
                    </button>
                )}
            </header>
            <section className="company-access__intro">
                <span className="surface-eyebrow">{isChinese ? '公司访问' : 'Company access'}</span>
                <h1>{isChinese ? '选择你要加入或创建的公司' : 'Choose a company to join or create'}</h1>
                <p>
                    {isChinese
                        ? '公司邀请只创建成员关系；平台注册凭证只创建全局账号，两者不会再混用。'
                        : 'Company invitations create memberships. Registration grants create global accounts. They are never interchangeable.'}
                </p>
            </section>

            {error && <div className="surface-alert surface-alert--error" role="alert">{error}</div>}
            {acceptedCompany && (
                <div className="surface-alert surface-alert--success" role="status">
                    <IconCheck size={20} />
                    <span>
                        <strong>{isChinese ? `已加入 ${acceptedCompany.tenantName}` : `Joined ${acceptedCompany.tenantName}`}</strong>
                        {isChinese ? '成员关系已保留。是否立即切换？' : 'The membership is saved. Switch now?'}
                    </span>
                    <button type="button" className="btn btn-primary" onClick={() => void switchToAcceptedCompany()} disabled={busyId === 'switch'}>
                        {isChinese ? '立即切换' : 'Switch now'} <IconArrowRight size={15} />
                    </button>
                    <button type="button" className="btn btn-ghost" onClick={() => setAcceptedCompany(null)}>
                        {isChinese ? '留在当前公司' : 'Stay here'}
                    </button>
                </div>
            )}

            <div className="company-access__grid">
                <section className="surface-card company-access__invitations">
                    <header>
                        <span className="surface-card__icon"><IconBuilding size={21} /></span>
                        <div>
                            <h2>{isChinese ? '发给我的公司邀请' : 'Invitations sent to me'}</h2>
                            <p>{invitationSummary}</p>
                        </div>
                    </header>
                    {pendingInvitations.length > 0 ? (
                        <div className="company-access__invitation-list">
                            {pendingInvitations.map((invitation) => (
                                <article key={invitation.id}>
                                    <div>
                                        <strong>{invitation.tenant_name}</strong>
                                        <span>{invitation.role === 'org_admin'
                                            ? (isChinese ? '公司管理员' : 'Company admin')
                                            : (isChinese ? '普通成员' : 'Member')}</span>
                                        <small><IconClock size={13} /> {new Date(invitation.expires_at).toLocaleString()}</small>
                                    </div>
                                    <div>
                                        <button type="button" className="btn btn-primary" disabled={busyId === invitation.id} onClick={() => void acceptInvitation(invitation)}>
                                            {isChinese ? '接受' : 'Accept'}
                                        </button>
                                        <button type="button" className="btn btn-ghost" disabled={busyId === invitation.id} onClick={() => void declineInvitation(invitation.id)}>
                                            <IconX size={15} /> {isChinese ? '拒绝' : 'Decline'}
                                        </button>
                                    </div>
                                </article>
                            ))}
                        </div>
                    ) : !loadingInvitations && (
                        <div className="surface-card__empty">
                            {isChinese ? '管理员通过邮箱邀请你后，会在这里显示。' : 'Email-bound invitations from company admins appear here.'}
                        </div>
                    )}
                </section>

                <section className="surface-card">
                    <header>
                        <span className="surface-card__icon"><IconKey size={21} /></span>
                        <div>
                            <h2>{isChinese ? '使用邀请令牌加入' : 'Join with an invitation token'}</h2>
                            <p>{isChinese ? '用于管理员明确分享的加入链接。' : 'For an explicit join link shared by an admin.'}</p>
                        </div>
                    </header>
                    <form className="surface-form" onSubmit={joinWithToken}>
                        <label>
                            <span>{isChinese ? '公司邀请令牌' : 'Company invitation token'}</span>
                            <input value={joinToken} onChange={(event) => setJoinToken(event.target.value)} autoComplete="off" required />
                        </label>
                        <button type="submit" className="btn btn-primary" disabled={busyId === 'join' || !joinToken.trim()}>
                            {busyId === 'join' ? (isChinese ? '加入中…' : 'Joining…') : (isChinese ? '加入公司' : 'Join company')}
                        </button>
                    </form>
                </section>

                <section className={`surface-card${canCreate ? '' : ' surface-card--disabled'}`}>
                    <header>
                        <span className="surface-card__icon"><IconPlus size={21} /></span>
                        <div>
                            <h2>{isChinese ? '创建一家新公司' : 'Create a new company'}</h2>
                            <p>{canCreate
                                ? (isChinese ? '你拥有账户级 company.create 权益。' : 'Your account has the company.create capability.')
                                : (isChinese ? '当前账号没有创建公司权益，可接受邀请加入。' : 'This account cannot create a company; you can still accept an invitation.')}</p>
                        </div>
                    </header>
                    {canCreate && (
                        <form className="surface-form" onSubmit={createCompany}>
                            <label>
                                <span>{isChinese ? '公司名称' : 'Company name'}</span>
                                <input maxLength={200} value={companyName} onChange={(event) => setCompanyName(event.target.value)} required />
                            </label>
                            <label>
                                <span>{isChinese ? '地区' : 'Region'}</span>
                                <select value={companyRegion} onChange={(event) => setCompanyRegion(event.target.value)}>
                                    <option value="CN">{isChinese ? '中国大陆' : 'Mainland China'}</option>
                                    <option value="HK">{isChinese ? '中国香港' : 'Hong Kong SAR'}</option>
                                    <option value="SG">{isChinese ? '新加坡' : 'Singapore'}</option>
                                    <option value="US">{isChinese ? '美国' : 'United States'}</option>
                                    <option value="001">{isChinese ? '其他 / 全球' : 'Other / Global'}</option>
                                </select>
                            </label>
                            <label>
                                <span>{isChinese ? '公司时区' : 'Company timezone'}</span>
                                <input maxLength={50} value={companyTimezone} onChange={(event) => setCompanyTimezone(event.target.value)} required />
                            </label>
                            <label className="surface-check">
                                <input
                                    type="checkbox"
                                    checked={companyAuthorityConfirmed}
                                    onChange={(event) => setCompanyAuthorityConfirmed(event.target.checked)}
                                    required
                                />
                                <span>
                                    {isChinese
                                        ? '我确认有权创建该公司空间，并将成为唯一公司所有者。'
                                        : 'I am authorized to create this company workspace and will become its sole owner.'}
                                </span>
                            </label>
                            <button type="submit" className="btn btn-primary" disabled={busyId === 'create' || !companyName.trim() || !companyAuthorityConfirmed}>
                                {busyId === 'create' ? (isChinese ? '创建中…' : 'Creating…') : (isChinese ? '创建并初始化' : 'Create and initialize')}
                            </button>
                        </form>
                    )}
                </section>
            </div>
        </main>
    );
}
