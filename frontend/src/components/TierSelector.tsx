import { useTranslation } from 'react-i18next';
import { IconLock } from '@tabler/icons-react';
import type { SaasTier } from '../constants/tiers';

export type { SaasTier } from '../constants/tiers';
export { resolveAllowedTier } from '../constants/tiers';

interface Props {
    value: SaasTier | null;
    onChange: (tier: SaasTier) => void;
    allowedTiers?: string[];
    disabled?: boolean;
    size?: 'sm' | 'md';
}

const TIERS: { key: SaasTier; labelKey: string; descKey: string }[] = [
    { key: 'lite', labelKey: 'tier.lite', descKey: 'tier.liteDesc' },
    { key: 'pro', labelKey: 'tier.pro', descKey: 'tier.proDesc' },
    { key: 'ultra', labelKey: 'tier.ultra', descKey: 'tier.ultraDesc' },
];

export function isTierAllowed(tier: string, allowedTiers?: string[]): boolean {
    if (!allowedTiers || allowedTiers.length === 0) return true;
    return allowedTiers.includes(tier);
}

export default function TierSelector({ value, onChange, allowedTiers, disabled, size = 'md' }: Props) {
    const { t } = useTranslation();

    const isSm = size === 'sm';

    return (
        <div
            style={{
                display: 'inline-flex',
                gap: isSm ? '6px' : '8px',
                padding: isSm ? '3px' : '4px',
                background: 'var(--bg-secondary)',
                borderRadius: '999px',
                border: '1px solid var(--border-subtle)',
            }}
        >
            {TIERS.map((tier) => {
                const allowed = isTierAllowed(tier.key, allowedTiers);
                const selected = value === tier.key;
                return (
                    <button
                        key={tier.key}
                        type="button"
                        disabled={disabled || !allowed}
                        onClick={() => allowed && onChange(tier.key)}
                        title={allowed ? t(tier.descKey) : t('tier.notAllowed', '当前套餐不包含此档位')}
                        style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '4px',
                            padding: isSm ? '3px 10px' : '5px 14px',
                            fontSize: isSm ? '11px' : '13px',
                            fontWeight: 500,
                            borderRadius: '999px',
                            border: 'none',
                            cursor: disabled || !allowed ? 'not-allowed' : 'pointer',
                            background: selected ? 'var(--accent-primary)' : 'transparent',
                            color: selected
                                ? 'var(--accent-primary-text, #fff)'
                                : !allowed
                                    ? 'var(--text-disabled)'
                                    : 'var(--text-primary)',
                            opacity: !allowed ? 0.6 : 1,
                            transition: 'background 120ms, color 120ms, opacity 120ms',
                        }}
                    >
                        {t(tier.labelKey, tier.key.charAt(0).toUpperCase() + tier.key.slice(1))}
                        {!allowed && <IconLock size={isSm ? 10 : 12} stroke={2} />}
                    </button>
                );
            })}
        </div>
    );
}
