interface Props {
  /** Rendered height in px */
  height?: number;
  className?: string;
  /** 'editorial' = glyph + Newsreader italic (onboarding); 'ui' = glyph + Inter semibold (sidebar/headers) */
  variant?: 'editorial' | 'ui';
  /** Render only the aster glyph (no text), e.g. collapsed sidebar / favicon contexts */
  glyphOnly?: boolean;
}

/**
 * Shared aster-glyph paths (4-pointed north star + orbit ring) used in both
 * the full wordmark and glyph-only rendering. viewBox is 0 0 24 24.
 */
function AsterGlyph() {
  return (
    <g>
      {/* Top spike (slightly elongated) */}
      <path d="M12 1.5 L12.9 10.8 L12 11.4 L11.1 10.8 Z" />
      <path d="M12 22.5 L12.9 13.2 L12 12.6 L11.1 13.2 Z" />
      <path d="M1.5 12 L10.8 12.9 L11.4 12 L10.8 11.1 Z" />
      <path d="M22.5 12 L13.2 11.1 L12.6 12 L13.2 12.9 Z" />
      <path d="M12 10 L14 12 L12 14 L10 12 Z" />
      <path d="M13.4 10.6 L14.2 11.4 L13.4 12.2 L12.6 11.4 Z" opacity="0.85" />
      <path d="M10.6 10.6 L11.4 9.8 L12.2 10.6 L11.4 11.4 Z" opacity="0.85" />
      <circle
        cx="12"
        cy="12"
        r="9"
        fill="none"
        stroke="currentColor"
        strokeWidth="0.55"
        strokeDasharray="42.4 14.1"
        strokeLinecap="round"
        opacity="0.45"
        transform="rotate(-30 12 12)"
      />
    </g>
  );
}

/**
 * Astra wordmark — Aster glyph + "Astra" text.
 * - editorial (default): Newsreader italic for the premium onboarding feel
 * - ui: Inter semibold for tighter UI chrome (sidebar, form headers)
 * - glyphOnly: just the star, for favicon / collapsed sidebar
 *
 * Uses fill="currentColor" so surrounding CSS controls the ink and theme
 * switching (light ↔ dark) works automatically.
 */
export default function AstraWordmark({
  height = 32,
  className,
  variant = 'editorial',
  glyphOnly = false,
}: Props) {
  if (glyphOnly) {
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        height={height}
        fill="currentColor"
        className={className}
        aria-label="Astra"
      >
        <AsterGlyph />
      </svg>
    );
  }

  // viewBox: glyph in 0..96 (scaled 4x from 24), then text at x=108
  const textProps =
    variant === 'editorial'
      ? {
          fontFamily: "'Newsreader', Georgia, 'Times New Roman', serif",
          fontStyle: 'italic' as const,
          fontWeight: 400,
          fontSize: 92,
          x: 112,
          y: 112,
          letterSpacing: '-0.01em',
        }
      : {
          fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
          fontStyle: 'normal' as const,
          fontWeight: 600,
          fontSize: 72,
          x: 108,
          y: 102,
          letterSpacing: '-0.02em',
        };

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 520 160"
      height={height}
      fill="currentColor"
      className={className}
      aria-label="Astra"
    >
      <g transform="translate(8 32) scale(4)">
        <AsterGlyph />
      </g>
      <text {...textProps} fill="currentColor">
        Astra
      </text>
    </svg>
  );
}
