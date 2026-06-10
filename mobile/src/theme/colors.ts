/**
 * AstraTrade dark trading theme — charcoal/emerald palette.
 * Mirrors the existing CSS vars from the web dashboard.
 */
export const C = {
  // Backgrounds
  bg:         '#0f1117',   // page background
  surface:    '#1a1f2e',   // cards / panels
  surfaceAlt: '#222840',   // elevated cards
  border:     '#2a3040',   // dividers

  // Text
  text:       '#f1f5f9',   // primary
  textMuted:  '#94a3b8',   // secondary
  textSubtle: '#64748b',   // placeholder

  // Accents
  accent:     '#10b981',   // emerald — primary action
  accentDim:  '#065f46',   // emerald dim (backgrounds)
  blue:       '#3b82f6',   // info / signals

  // Status
  pos:        '#22c55e',   // positive P&L
  neg:        '#ef4444',   // negative P&L
  warn:       '#f59e0b',   // warning / CAUTION

  // Regimes
  bull:       '#22c55e',
  caution:    '#f59e0b',
  bear:       '#ef4444',

  // Misc
  paper:      '#6366f1',   // paper mode badge
  crypto:     '#f59e0b',   // crypto label
  white:      '#ffffff',
  black:      '#000000',
} as const;

export type ColorKey = keyof typeof C;

export function pnlColor(value: number): string {
  return value >= 0 ? C.pos : C.neg;
}

export function regimeColor(regime: string): string {
  const r = regime?.toUpperCase();
  if (r === 'BULL') return C.bull;
  if (r === 'CAUTION') return C.caution;
  if (r === 'BEAR') return C.bear;
  return C.textMuted;
}
