import type { ReactNode } from 'react';
import { IconWorld } from '@tabler/icons-react';
import AstraWordmark from './AstraWordmark';

interface Props {
    /** When provided, replaces the Astra brand with a "← BACK" pill button */
    onBack?: () => void;
    /** When provided, renders the language toggle in the top-right corner */
    onToggleLang?: () => void;
    /** Page contents */
    children: ReactNode;
    className?: string;
}

export default function AtlasFrame({ onBack, onToggleLang, className, children }: Props) {
    const pageClass = ['atlas-page', className].filter(Boolean).join(' ');
    return (
        <div className={pageClass}>
            <header className="atlas-frame-top">
                <div className="atlas-frame-top-left">
                    {onBack ? (
                        <button type="button" className="atlas-back-btn" onClick={onBack}>
                            <span aria-hidden="true">←</span> Back
                        </button>
                    ) : (
                        <AstraWordmark height={28} variant="editorial" className="atlas-brand-wordmark" />
                    )}
                </div>
                {onToggleLang && (
                    <button type="button" className="atlas-globe-btn" onClick={onToggleLang} aria-label="Toggle language">
                        <IconWorld size={16} stroke={1.4} />
                    </button>
                )}
            </header>

            <main className="atlas-frame-body">{children}</main>
        </div>
    );
}
