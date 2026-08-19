import { IconShieldLock, IconX } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router';

import { authApi } from '../services/api';
import { useAuthStore } from '../stores';
import '../pages/accountSecurity.css';

const DISMISS_KEY = 'astra.mfa-recommendation.dismissed';

export default function MfaRecommendationBanner() {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const location = useLocation();
    const token = useAuthStore((state) => state.token);
    const isChinese = i18n.language.startsWith('zh');
    const [dismissed, setDismissed] = useState(
        () => sessionStorage.getItem(DISMISS_KEY) === '1',
    );
    const { data: status } = useQuery({
        queryKey: ['mfa-status'],
        queryFn: () => authApi.mfaStatus(),
        enabled: Boolean(token) && !dismissed,
        staleTime: 60_000,
        retry: false,
    });

    if (
        dismissed
        || !status
        || status.enabled
        || !status.recommended
        || location.pathname.startsWith('/account/security')
        || location.pathname.includes('/chat')
    ) {
        return null;
    }

    const dismiss = () => {
        sessionStorage.setItem(DISMISS_KEY, '1');
        setDismissed(true);
    };

    return (
        <div className="mfa-recommendation-banner" role="status">
            <IconShieldLock size={18} />
            <span>
                {isChinese
                    ? '强烈建议为公司管理员绑定验证器。现在可以跳过，不影响登录和使用；绑定后下次登录才需要动态码。'
                    : 'We strongly recommend binding an authenticator for company admins. You can skip this for now without blocking login; a code is required only after you enable it.'}
            </span>
            <button type="button" className="btn btn-secondary" onClick={() => navigate('/account/security')}>
                {isChinese ? '去设置' : 'Set up'}
            </button>
            <button type="button" className="mfa-recommendation-banner__dismiss" onClick={dismiss} aria-label={isChinese ? '稍后提醒' : 'Remind later'}>
                <IconX size={16} />
            </button>
        </div>
    );
}
