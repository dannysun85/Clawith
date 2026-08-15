import type { ReactNode } from 'react';
import { IconArrowLeft, IconLogout, IconShieldLock } from '@tabler/icons-react';
import { NavLink, useNavigate } from 'react-router';

import { useAuthStore } from '../stores';
import { AstraWordmark } from './atlas';
import '../pages/productConsole.css';

export type ProductConsoleNavItem = {
    to: string;
    label: string;
    icon: ReactNode;
    exact?: boolean;
    badge?: number | string | null;
};

type ProductConsoleShellProps = {
    kind: 'company' | 'platform';
    title: string;
    subtitle: string;
    navLabel: string;
    items: ProductConsoleNavItem[];
    children: ReactNode;
    backTo?: string;
    backLabel?: string;
    headerActions?: ReactNode;
    banner?: ReactNode;
};

export default function ProductConsoleShell({
    kind,
    title,
    subtitle,
    navLabel,
    items,
    children,
    backTo,
    backLabel,
    headerActions,
    banner,
}: ProductConsoleShellProps) {
    const navigate = useNavigate();
    const { user, logout } = useAuthStore();

    const handleLogout = () => {
        logout();
        navigate('/login', { replace: true });
    };

    return (
        <div className={`product-console product-console--${kind}`}>
            <aside className="product-console__rail">
                <div className="product-console__brand">
                    <AstraWordmark height={22} variant="ui" />
                </div>
                <div className="product-console__identity">
                    <span>{kind === 'company' ? 'Company administration' : 'Platform operations'}</span>
                    <strong>{title}</strong>
                    <small>{subtitle}</small>
                </div>
                <nav aria-label={navLabel} className="product-console__nav">
                    {items.map((item) => (
                        <NavLink
                            key={item.to}
                            to={item.to}
                            end={item.exact}
                            className={({ isActive }) => isActive ? 'active' : undefined}
                        >
                            <span className="product-console__nav-icon">{item.icon}</span>
                            <span>{item.label}</span>
                            {item.badge !== null && item.badge !== undefined && (
                                <small className="product-console__nav-badge">{item.badge}</small>
                            )}
                        </NavLink>
                    ))}
                </nav>
                <div className="product-console__rail-footer">
                    {backTo && (
                        <button type="button" onClick={() => navigate(backTo)}>
                            <IconArrowLeft size={16} />
                            <span>{backLabel}</span>
                        </button>
                    )}
                    <div className="product-console__account">
                        <div>
                            <strong>{user?.display_name || user?.username}</strong>
                            <small>{user?.email}</small>
                        </div>
                        <button type="button" onClick={() => navigate('/account/security')} aria-label="登录安全" title="登录安全">
                            <IconShieldLock size={16} />
                        </button>
                        <button type="button" onClick={handleLogout} aria-label="退出登录" title="退出登录">
                            <IconLogout size={16} />
                        </button>
                    </div>
                </div>
            </aside>
            <section className="product-console__workspace">
                <header className="product-console__topbar">
                    <div>
                        <span>{kind === 'company' ? '公司治理范围' : '全局平台范围'}</span>
                        <strong>{title}</strong>
                    </div>
                    <div className="product-console__actions">{headerActions}</div>
                </header>
                {banner}
                <main className="product-console__content">{children}</main>
            </section>
        </div>
    );
}
