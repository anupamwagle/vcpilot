// ─── Auth ────────────────────────────────────────────────────────────────────

export interface AuthUser {
  access_token: string;
  token_type: string;
  user_id: number;
  org_id: number;
  email: string;
  name: string;
}

// ─── Dashboard ───────────────────────────────────────────────────────────────

export interface DashboardData {
  open_positions_count: number;
  pending_signals_count: number;
  total_unrealised_pnl: number;
  total_unrealised_pct: number;
  todays_realised_pnl: number;
  todays_trades_count: number;
  regime_asx: string;
  regime_crypto: string;
  worker_status: 'online' | 'offline' | 'starting';
  trading_paused: boolean;
  capital_aud: number;
  active_exchanges: string;
  timestamp: string;
}

// ─── Positions ───────────────────────────────────────────────────────────────

export interface Position {
  id: number;
  ticker: string;
  ticker_raw: string;
  exchange_key: string;
  asset_type: 'EQUITY' | 'CRYPTO';
  currency: string;
  entry_date: string;
  entry_price: number;
  current_price: number | null;
  current_stop: number;
  qty: number;
  unrealised_pnl: number;
  unrealised_pct: number;
  target_1: number | null;
  target_2: number | null;
  target_1_hit: boolean;
  is_paper: boolean;
  last_updated: string | null;
}

// ─── Signals ─────────────────────────────────────────────────────────────────

export type SignalStatus = 'PENDING' | 'TRIGGERED' | 'EXPIRED' | 'SKIPPED' | 'CANCELLED';

export interface Signal {
  id: number;
  ticker: string;
  ticker_raw: string;
  exchange_key: string;
  asset_type: 'EQUITY' | 'CRYPTO';
  currency: string;
  signal_date: string;
  status: SignalStatus;
  close_price: number | null;
  pivot_price: number | null;
  stop_price: number | null;
  target_1: number | null;
  target_2: number | null;
  rs_rating: number | null;
  trend_score: number | null;
  fundamental_score: number | null;
  vcp_contractions: number | null;
  vcp_weeks: number | null;
  rules_passed: number;
  rules_total: number;
  suggested_size_aud: number | null;
  risk_per_trade_aud: number | null;
  created_at: string | null;
}

// ─── Watchlist ────────────────────────────────────────────────────────────────

export interface WatchlistItem {
  id: number;
  ticker: string;
  ticker_raw: string;
  exchange_key: string;
  asset_type: 'EQUITY' | 'CRYPTO';
  currency: string;
  added_date: string;
  added_by: string;
  label: string | null;
  label_color: string | null;
  notes: string | null;
  rule_results: Record<string, boolean>;
}

// ─── Trades ───────────────────────────────────────────────────────────────────

export interface Trade {
  id: number;
  ticker: string;
  ticker_raw: string;
  exchange_key: string;
  asset_type: string;
  entry_date: string;
  exit_date: string;
  hold_days: number;
  entry_price: number;
  exit_price: number;
  qty: number;
  net_pnl_aud: number | null;
  pnl_pct: number | null;
  exit_reason: string | null;
  is_paper: boolean;
}

export interface TradeStats {
  total_pnl: number;
  win_rate: number;
  avg_winner: number;
  avg_loser: number;
}

// ─── Exit reasons ─────────────────────────────────────────────────────────────

export const EXIT_REASONS = [
  { key: 'STOP_LOSS',       label: 'Stop Loss',          group: 'Defensive' },
  { key: 'TRAILING_STOP',   label: 'Trailing Stop',       group: 'Defensive' },
  { key: 'TIME_STOP',       label: 'Time Stop',           group: 'Defensive' },
  { key: 'EARNINGS_AVOID',  label: 'Earnings Avoid',      group: 'Defensive' },
  { key: 'MARKET_REGIME',   label: 'Market Regime',       group: 'Defensive' },
  { key: 'PROFIT_TARGET_1', label: 'Profit Target 20%',   group: 'Offensive' },
  { key: 'PROFIT_TARGET_2', label: 'Profit Target 40%',   group: 'Offensive' },
  { key: 'CLIMAX_TOP',      label: 'Climax Top',          group: 'Offensive' },
  { key: 'THREE_WEEKS_TIGHT', label: '3-Weeks Tight',     group: 'Offensive' },
  { key: 'MANUAL',          label: 'Manual Exit',         group: 'Other' },
] as const;
