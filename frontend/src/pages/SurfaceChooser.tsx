import { IconArrowRight, IconBriefcase, IconShieldLock } from '@tabler/icons-react';
import { Navigate, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';

import { AstraWordmark } from '../components/atlas';
import { useAuthStore } from '../stores';
import {
    PRODUCT_SURFACE_PATHS,
    availablePrimarySurfaces,
    type ProductSurface,
} from '../utils/productAccess';
import './productSurfaces.css';

export default function SurfaceChooser() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const user = useAuthStore((state) => state.user);
    const isChinese = i18n.language.startsWith('zh');
    const surfaces = availablePrimarySurfaces(user);

    if (!user) return <Navigate to="/login" replace />;
    if (surfaces.length === 0) return <Navigate to="/setup-company" replace />;
    if (surfaces.length === 1) return <Navigate to={PRODUCT_SURFACE_PATHS[surfaces[0]]} replace />;

    const choose = (surface: ProductSurface) => {
        localStorage.setItem('preferred_product_surface', surface);
        navigate(PRODUCT_SURFACE_PATHS[surface], { replace: true });
    };

    return (
        <main className="surface-choice">
            <div className="surface-choice__brand"><AstraWordmark height={24} variant="ui" /></div>
            <section className="surface-choice__panel" aria-labelledby="surface-choice-title">
                <span className="surface-eyebrow">{isChinese ? '选择工作身份' : 'Choose a product surface'}</span>
                <h1 id="surface-choice-title">
                    {isChinese ? `你好，${user.display_name || user.username}` : `Welcome, ${user.display_name || user.username}`}
                </h1>
                <p>
                    {isChinese
                        ? '你的账号同时拥有公司成员身份和平台运营权限。两种身份彼此独立，本次进入哪一个？'
                        : 'Your account has both a company membership and platform authority. These are independent; choose where to work now.'}
                </p>
                <div className="surface-choice__cards">
                    <button type="button" onClick={() => choose('work')}>
                        <span className="surface-choice__icon"><IconBriefcase size={24} /></span>
                        <strong>{isChinese ? '进入公司工作区' : 'Enter company workspace'}</strong>
                        <small>
                            {isChinese
                                ? '处理任务、使用私人助理、数字员工与协作群组。'
                                : 'Work with tasks, your private assistant, digital employees, and groups.'}
                        </small>
                        <span>{isChinese ? '以公司成员身份继续' : 'Continue as company member'} <IconArrowRight size={15} /></span>
                    </button>
                    <button type="button" onClick={() => choose('platform_admin')}>
                        <span className="surface-choice__icon"><IconShieldLock size={24} /></span>
                        <strong>{isChinese ? '进入平台运营台' : 'Enter platform operations'}</strong>
                        <small>
                            {isChinese
                                ? '管理公司、注册凭证、套餐、Provider 与受审计支持会话。'
                                : 'Manage companies, registration grants, plans, providers, and audited support sessions.'}
                        </small>
                        <span>{isChinese ? '以平台运营者身份继续' : 'Continue as platform operator'} <IconArrowRight size={15} /></span>
                    </button>
                </div>
                <p className="surface-choice__hint">
                    {isChinese
                        ? '稍后可以从账户菜单切换。平台权限不会自动获得任何公司的成员或 Agent 权限。'
                        : 'You can switch later from the account menu. Platform authority never implies company membership or Agent access.'}
                </p>
            </section>
        </main>
    );
}
