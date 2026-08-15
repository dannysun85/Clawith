import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { IconArrowRight, IconBuilding, IconLock, IconUser } from '@tabler/icons-react';

import { authApi, onboardingApi } from '../services/api';
import { useAuthStore } from '../stores';
import { AtlasFrame, StarField, OrbitPlate, UniverseMap } from '../components/atlas';

type Step = 'company' | 'profile' | 'assistant' | 'opening';

type OnboardingStatus = {
    status: string;
    current_step: string;
    personal_assistant_agent_id?: string | null;
    company_initialization_required?: boolean;
    company?: {
        id: string;
        name: string;
        timezone: string;
        country_region: string;
        company_size: string;
        allow_member_private_agents: boolean;
        default_approval_policy: string;
    } | null;
    member_profile?: {
        display_name: string;
        title: string;
        timezone: string;
        work_hours_start: string;
        work_hours_end: string;
    } | null;
};

function browserTimezone(): string {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

function statusStep(status: OnboardingStatus): Step {
    if (status.company_initialization_required || status.current_step === 'company') return 'company';
    if (status.current_step === 'profile') return 'profile';
    if (status.current_step === 'opening' || status.personal_assistant_agent_id) return 'opening';
    return 'assistant';
}

export default function Onboarding() {
    const { i18n } = useTranslation();
    const isZh = i18n.language.startsWith('zh');
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const { user, setUser } = useAuthStore();
    const mode = (searchParams.get('mode') === 'join' ? 'join' : 'create') as 'create' | 'join';
    const [step, setStep] = useState<Step>('profile');
    const [assistantId, setAssistantId] = useState<string | null>(null);
    const [assistantName, setAssistantName] = useState(isZh ? '私人助理' : 'Private Assistant');
    const [personalities, setPersonalities] = useState<string[]>(['warm']);
    const [workStyle, setWorkStyle] = useState('concise');
    const [proactivity, setProactivity] = useState('balanced');
    const [boundaries, setBoundaries] = useState('');
    const [expanded, setExpanded] = useState(false);
    const [loading, setLoading] = useState(false);
    const [booting, setBooting] = useState(true);
    const [error, setError] = useState('');

    const [company, setCompany] = useState({
        name: '',
        timezone: browserTimezone(),
        country_region: isZh ? 'CN' : '001',
        company_size: 'unspecified',
        allow_member_private_agents: false,
        default_approval_policy: 'high_risk',
    });
    const [profile, setProfile] = useState({
        display_name: user?.display_name || '',
        title: user?.title || '',
        timezone: user?.timezone || browserTimezone(),
        work_hours_start: user?.work_hours_start || '09:00',
        work_hours_end: user?.work_hours_end || '18:00',
    });

    const hydrate = (status: OnboardingStatus) => {
        if (status.company) {
            setCompany({
                name: status.company.name,
                timezone: status.company.timezone || browserTimezone(),
                country_region: status.company.country_region || (isZh ? 'CN' : '001'),
                company_size: status.company.company_size || 'unspecified',
                allow_member_private_agents: Boolean(status.company.allow_member_private_agents),
                default_approval_policy: status.company.default_approval_policy || 'high_risk',
            });
        }
        if (status.member_profile) {
            setProfile({
                display_name: status.member_profile.display_name || user?.display_name || '',
                title: status.member_profile.title || '',
                timezone: status.member_profile.timezone || browserTimezone(),
                work_hours_start: status.member_profile.work_hours_start || '09:00',
                work_hours_end: status.member_profile.work_hours_end || '18:00',
            });
        }
        setAssistantId(status.personal_assistant_agent_id || null);
        setStep(statusStep(status));
    };

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');
    }, []);

    useEffect(() => {
        let cancelled = false;
        setBooting(true);
        onboardingApi.start(mode)
            .then((status: OnboardingStatus) => {
                if (cancelled) return;
                if (status.status === 'completed') {
                    navigate('/work', { replace: true });
                    return;
                }
                hydrate(status);
            })
            .catch((nextError) => setError(nextError.message || 'Failed to start onboarding'))
            .finally(() => { if (!cancelled) setBooting(false); });
        return () => { cancelled = true; };
    }, [mode, navigate]);

    const personalityOptions = useMemo(() => [
        { id: 'warm', zh: '温和', en: 'Warm' },
        { id: 'precise', zh: '严谨', en: 'Precise' },
        { id: 'witty', zh: '幽默', en: 'Witty' },
        { id: 'direct', zh: '直接', en: 'Direct' },
    ], []);
    const workStyleOptions = useMemo(() => [
        { id: 'concise', zh: '简洁', en: 'Concise' },
        { id: 'efficient', zh: '高效', en: 'Efficient' },
        { id: 'detailed', zh: '详尽', en: 'Detailed' },
        { id: 'steady', zh: '稳健', en: 'Steady' },
    ], []);
    const proactivityOptions = useMemo(() => [
        { id: 'reactive', zh: '仅在我询问时', en: 'Only when asked' },
        { id: 'balanced', zh: '适度提醒', en: 'Balanced' },
        { id: 'proactive', zh: '主动跟进', en: 'Proactive' },
    ], []);

    const togglePersonality = (id: string) => {
        setPersonalities((current) => (
            current.includes(id) ? current.filter((item) => item !== id) : [...current, id]
        ));
    };

    const submitCompany = async (safeDefaults = false) => {
        setLoading(true);
        setError('');
        try {
            const status = await onboardingApi.initializeCompany({
                ...company,
                company_size: safeDefaults ? 'unspecified' : company.company_size,
                allow_member_private_agents: safeDefaults ? false : company.allow_member_private_agents,
                default_approval_policy: safeDefaults ? 'high_risk' : company.default_approval_policy,
            });
            hydrate(status);
            setStep('profile');
        } catch (nextError: any) {
            setError(nextError.message || 'Failed to initialize company');
        } finally {
            setLoading(false);
        }
    };

    const submitProfile = async () => {
        setLoading(true);
        setError('');
        try {
            const status = await onboardingApi.completeProfile(profile);
            hydrate(status);
            const refreshedUser = await authApi.me();
            setUser(refreshedUser);
            setStep('assistant');
        } catch (nextError: any) {
            setError(nextError.message || 'Failed to save member profile');
        } finally {
            setLoading(false);
        }
    };

    const createAssistant = async (useSafeDefaults = false) => {
        setError('');
        setLoading(true);
        try {
            const defaultName = isZh ? '私人助理' : 'Private Assistant';
            const nextName = useSafeDefaults ? defaultName : assistantName.trim();
            const result = await onboardingApi.createPersonalAssistant({
                name: nextName,
                personality: useSafeDefaults ? 'warm' : (personalities.join(', ') || 'warm'),
                work_style: useSafeDefaults ? 'concise' : workStyle,
                proactivity: useSafeDefaults ? 'balanced' : proactivity,
                boundaries: useSafeDefaults ? '' : boundaries,
            });
            const nextId = result?.agent?.id || result?.onboarding?.personal_assistant_agent_id;
            setAssistantName(nextName);
            setAssistantId(nextId);
            setStep('opening');
        } catch (nextError: any) {
            setError(nextError.message || 'Failed to create personal assistant');
        } finally {
            setLoading(false);
        }
    };

    const enterOffice = async () => {
        if (!assistantId) return;
        setLoading(true);
        setError('');
        try {
            await onboardingApi.complete();
            navigate('/work', { replace: true });
        } catch (nextError: any) {
            setError(nextError.message || 'Failed to complete onboarding');
            setLoading(false);
        }
    };

    const toggleLang = () => i18n.changeLanguage(isZh ? 'en' : 'zh');

    if (!user?.tenant_id) {
        return (
            <AtlasFrame onToggleLang={toggleLang}>
                <div className="atlas-screen-center atlas-screen-pad">
                    <div>
                        <h1 className="atlas-h1">{isZh ? '先创建或加入一家公司' : 'Create or join a company first'}</h1>
                        <button className="atlas-btn atlas-btn--primary" onClick={() => navigate('/setup-company')}>
                            {isZh ? '去设置公司' : 'Set up company'}
                        </button>
                    </div>
                </div>
            </AtlasFrame>
        );
    }

    if (booting) {
        return (
            <AtlasFrame onToggleLang={toggleLang}>
                <div className="atlas-screen-center atlas-screen-pad">
                    <span className="atlas-mono">{isZh ? '正在恢复初始化进度…' : 'RESTORING ONBOARDING…'}</span>
                </div>
            </AtlasFrame>
        );
    }

    if (step === 'company') {
        return (
            <AtlasFrame onToggleLang={toggleLang}>
                <div className="atlas-screen-split">
                    <div className="atlas-screen-plate atlas-screen-plate--gridded">
                        <div className="atlas-grid-bg" aria-hidden="true" />
                        <div className="atlas-policy-plate">
                            <IconBuilding size={42} stroke={1.2} />
                            <span className="atlas-mono">01 / COMPANY</span>
                            <strong>{company.name || (isZh ? '你的公司' : 'Your company')}</strong>
                            <p>{isZh ? '创建者成为唯一公司所有者。其他管理员仍然是公司员工，但不能转让所有权或删除公司。' : 'The creator becomes the sole company owner. Other admins remain employees and cannot transfer ownership or delete the company.'}</p>
                        </div>
                    </div>
                    <form className="atlas-screen-form atlas-screen-form--padded" onSubmit={(event) => { event.preventDefault(); void submitCompany(false); }}>
                        <div>
                            <span className="atlas-mono">{isZh ? '公司初始化' : 'COMPANY INITIALIZATION'}</span>
                            <h1 className="atlas-h1">{isZh ? <>确认公司的<em>工作边界</em>。</> : <>Confirm the company <em>working boundary.</em></>}</h1>
                            <p className="atlas-body atlas-body--muted">{isZh ? '这里只设置成员与审批的默认规则。Provider、模型、Skill 和 Tool 稍后在公司管理或平台运营中配置。' : 'Only member and approval defaults belong here. Providers, models, Skills and Tools are configured later in admin or platform operations.'}</p>
                        </div>
                        {error && <div className="atlas-error">{error}</div>}
                        <div className="atlas-input-wrap">
                            <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '公司名' : 'NAME'}</span><input className="atlas-input" value={company.name} onChange={(event) => setCompany({ ...company, name: event.target.value })} required /></label>
                            <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '时区' : 'TIMEZONE'}</span><input className="atlas-input" value={company.timezone} onChange={(event) => setCompany({ ...company, timezone: event.target.value })} required /></label>
                            <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '地区' : 'REGION'}</span><select className="atlas-input" value={company.country_region} onChange={(event) => setCompany({ ...company, country_region: event.target.value })}><option value="CN">CN</option><option value="HK">HK</option><option value="SG">SG</option><option value="US">US</option><option value="001">GLOBAL</option></select></label>
                            <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '规模' : 'SIZE'}</span><select className="atlas-input" value={company.company_size} onChange={(event) => setCompany({ ...company, company_size: event.target.value })}><option value="unspecified">{isZh ? '暂不填写' : 'Not specified'}</option><option value="1-10">1–10</option><option value="11-50">11–50</option><option value="51-200">51–200</option><option value="201-1000">201–1000</option><option value="1000+">1000+</option></select></label>
                        </div>
                        <label className="atlas-policy-check"><input type="checkbox" checked={company.allow_member_private_agents} onChange={(event) => setCompany({ ...company, allow_member_private_agents: event.target.checked })} /><span><strong>{isZh ? '允许普通成员创建私有 Agent' : 'Allow members to create private Agents'}</strong><small>{isZh ? '关闭时，成员仍有自己的私人助理，但不能额外创建私有数字员工。' : 'When off, members keep their private assistant but cannot create additional private employees.'}</small></span></label>
                        <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '审批' : 'APPROVAL'}</span><select className="atlas-input" value={company.default_approval_policy} onChange={(event) => setCompany({ ...company, default_approval_policy: event.target.value })}><option value="high_risk">{isZh ? '仅高风险操作审批' : 'High-risk actions only'}</option><option value="external_actions">{isZh ? '所有外部动作审批' : 'All external actions'}</option><option value="all_writes">{isZh ? '所有写入和外部动作审批' : 'All writes and external actions'}</option></select></label>
                        <div className="atlas-cta-row"><button className="atlas-btn atlas-btn--primary" type="submit" disabled={loading || !company.name.trim()}>{loading ? '…' : (isZh ? '保存并继续' : 'Save and continue')}<IconArrowRight size={14} /></button><button className="atlas-btn" type="button" disabled={loading || !company.name.trim()} onClick={() => void submitCompany(true)}>{isZh ? '使用安全默认值' : 'Use safe defaults'}</button></div>
                    </form>
                </div>
            </AtlasFrame>
        );
    }

    if (step === 'profile') {
        return (
            <AtlasFrame onToggleLang={toggleLang}>
                <div className="atlas-screen-split">
                    <div className="atlas-screen-plate atlas-screen-plate--gridded">
                        <div className="atlas-grid-bg" aria-hidden="true" />
                        <div className="atlas-policy-plate"><IconUser size={42} stroke={1.2} /><span className="atlas-mono">02 / MEMBERSHIP</span><strong>{profile.display_name || (isZh ? '你的成员身份' : 'Your membership')}</strong><p>{isZh ? '同一个账号在不同公司可以有不同职位、时区和工作时间。这里不会改变你的全局登录身份。' : 'The same account may have different roles, timezones and work hours in each company. This does not change your global login identity.'}</p></div>
                    </div>
                    <form className="atlas-screen-form atlas-screen-form--padded" onSubmit={(event) => { event.preventDefault(); void submitProfile(); }}>
                        <div><span className="atlas-mono">{isZh ? '成员入职' : 'MEMBER ONBOARDING'}</span><h1 className="atlas-h1">{isZh ? <>你在这家公司的<em>工作身份</em>。</> : <>Your <em>working identity</em> here.</>}</h1></div>
                        {error && <div className="atlas-error">{error}</div>}
                        <div className="atlas-input-wrap">
                            <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '显示名' : 'NAME'}</span><input className="atlas-input" value={profile.display_name} onChange={(event) => setProfile({ ...profile, display_name: event.target.value })} required /></label>
                            <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '职位' : 'TITLE'}</span><input className="atlas-input" value={profile.title} onChange={(event) => setProfile({ ...profile, title: event.target.value })} placeholder={isZh ? '可留空' : 'Optional'} /></label>
                            <label className="atlas-input-row"><span className="atlas-input-label">{isZh ? '时区' : 'TIMEZONE'}</span><input className="atlas-input" value={profile.timezone} onChange={(event) => setProfile({ ...profile, timezone: event.target.value })} required /></label>
                            <div className="atlas-dual-input"><label><span className="atlas-input-label">{isZh ? '开始' : 'START'}</span><input type="time" className="atlas-input" value={profile.work_hours_start} onChange={(event) => setProfile({ ...profile, work_hours_start: event.target.value })} /></label><label><span className="atlas-input-label">{isZh ? '结束' : 'END'}</span><input type="time" className="atlas-input" value={profile.work_hours_end} onChange={(event) => setProfile({ ...profile, work_hours_end: event.target.value })} /></label></div>
                        </div>
                        <div className="atlas-cta-row"><button className="atlas-btn atlas-btn--primary" type="submit" disabled={loading || !profile.display_name.trim()}>{loading ? '…' : (isZh ? '保存成员资料' : 'Save profile')}<IconArrowRight size={14} /></button></div>
                    </form>
                </div>
            </AtlasFrame>
        );
    }

    if (step === 'assistant') {
        return (
            <AtlasFrame onBack={() => setStep('profile')} onToggleLang={toggleLang}>
                <div className="atlas-screen-split">
                    <div className="atlas-screen-plate atlas-screen-plate--gridded"><div className="atlas-grid-bg" aria-hidden="true" /><OrbitPlate assistantLabel={`I — ${(assistantName || 'ASSISTANT').toUpperCase()}`} founderLabel={user.membership_role === 'org_owner' ? 'OWNER' : 'MEMBER'} width={520} /></div>
                    <div className="atlas-screen-form atlas-screen-form--padded">
                        <div><span className="atlas-mono">03 / PRIVATE ASSISTANT</span><h1 className="atlas-h1">{isZh ? <>认识你的<em>私人协调者</em>。</> : <>Meet your <em>private coordinator.</em></>}</h1><p className="atlas-body atlas-body--muted">{isZh ? '私人助理属于你在这家公司的成员身份。公司管理员和平台运营默认都不能读取它；公司 Agent 员工则按公司授权协作。' : 'This assistant belongs only to your membership in this company. Company admins and platform operators cannot read it by default; company Agent employees use explicit company grants.'}</p></div>
                        <div className="atlas-privacy-note"><IconLock size={16} /><span>{isZh ? '每个公司成员关系只有一个私人助理槽位；重复提交会恢复同一个助理。' : 'One private-assistant slot per company membership; retries restore the same assistant.'}</span></div>
                        {error && <div className="atlas-error">{error}</div>}
                        <div className="atlas-input-wrap"><div className="atlas-input-row"><span className="atlas-input-label">{isZh ? '名字' : 'NAME'}</span><input className="atlas-input atlas-input--serif" value={assistantName} onChange={(event) => setAssistantName(event.target.value)} placeholder={isZh ? '助理的名字' : 'Assistant name'} /></div></div>
                        <button className="atlas-expand" type="button" onClick={() => setExpanded((value) => !value)}><span className="atlas-body">{isZh ? '定制风格、主动程度与边界' : 'Customise style, proactivity and boundaries'}</span><span className="atlas-mono">{expanded ? (isZh ? '收起' : 'COLLAPSE') : (isZh ? '展开' : 'EXPAND')}</span></button>
                        <div className="atlas-chip-row">{personalityOptions.map((item) => <button key={item.id} type="button" className={`atlas-chip${personalities.includes(item.id) ? ' is-active' : ''}`} aria-pressed={personalities.includes(item.id)} onClick={() => togglePersonality(item.id)}>{isZh ? item.zh : item.en}</button>)}</div>
                        {expanded && <div className="atlas-options"><div><span className="atlas-input-label">{isZh ? '办事风格' : 'WORK STYLE'}</span><div className="atlas-chip-row">{workStyleOptions.map((item) => <button key={item.id} type="button" className={`atlas-chip${workStyle === item.id ? ' is-active' : ''}`} onClick={() => setWorkStyle(item.id)}>{isZh ? item.zh : item.en}</button>)}</div></div><div><span className="atlas-input-label">{isZh ? '主动程度' : 'PROACTIVITY'}</span><div className="atlas-chip-row">{proactivityOptions.map((item) => <button key={item.id} type="button" className={`atlas-chip${proactivity === item.id ? ' is-active' : ''}`} onClick={() => setProactivity(item.id)}>{isZh ? item.zh : item.en}</button>)}</div></div><textarea className="atlas-textarea" value={boundaries} onChange={(event) => setBoundaries(event.target.value)} placeholder={isZh ? '绝对不要做的事情（可留空）' : 'Things they should never do (optional)'} /></div>}
                        <div className="atlas-cta-row"><button className="atlas-btn atlas-btn--primary" onClick={() => void createAssistant(false)} disabled={loading || !assistantName.trim()}>{loading ? '…' : (isZh ? '创建我的助理' : 'Create my assistant')}<IconArrowRight size={14} /></button><button className="atlas-btn" type="button" onClick={() => void createAssistant(true)} disabled={loading}>{isZh ? '暂时跳过，使用默认助理' : 'Skip for now, use defaults'}</button></div>
                    </div>
                </div>
            </AtlasFrame>
        );
    }

    const displayName = (assistantName || (isZh ? '私人助理' : 'Private Assistant')).toUpperCase();
    return (
        <AtlasFrame onToggleLang={toggleLang}>
            <div className="atlas-screen-split">
                <div className="atlas-screen-plate atlas-screen-plate--snug"><StarField density="low" seed={9} /><UniverseMap size={640} assistantName={displayName} /></div>
                <div className="atlas-screen-form atlas-screen-form--padded">
                    <h1 className="atlas-display">{isZh ? <>灯，亮了。</> : <>The lights<br />are on.</>}</h1>
                    <p className="atlas-body atlas-body--muted">{isZh ? '你的成员身份和私人助理已经准备好。数字员工属于公司的长期岗位，私人助理只属于你；都可以继续使用原来的 Agent 消息界面。' : 'Your membership and private assistant are ready. Digital employees are durable company roles; the private assistant belongs only to you. Both continue in the existing Agent message interface.'}</p>
                    <div className="atlas-divider" />
                    <ul className="atlas-roster"><li><span className="atlas-roster-mark">★</span><span className="atlas-roster-label">{isZh ? '公司身份' : 'MEMBERSHIP'}</span><span className="atlas-roster-value">{profile.display_name.toUpperCase()}</span></li><li><span className="atlas-roster-mark">○</span><span className="atlas-roster-label">{isZh ? '私人协调者' : 'PRIVATE COORDINATOR'}</span><span className="atlas-roster-value">{displayName}</span></li><li><span className="atlas-roster-mark">·</span><span className="atlas-roster-label">{isZh ? '公司数字员工' : 'COMPANY EMPLOYEES'}</span><span className="atlas-roster-value">∞</span></li></ul>
                    {error && <div className="atlas-error">{error}</div>}
                    <div className="atlas-cta-row"><button className="atlas-btn atlas-btn--primary" onClick={() => void enterOffice()} disabled={!assistantId || loading}>{loading ? '…' : (isZh ? '开始第一项工作' : 'Start your first task')}<IconArrowRight size={14} /></button></div>
                </div>
            </div>
        </AtlasFrame>
    );
}
