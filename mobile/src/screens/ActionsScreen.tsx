import React, { useState, useCallback } from 'react';
import {
  View, Text, StyleSheet, ScrollView, TouchableOpacity,
  ActivityIndicator, RefreshControl, Alert,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { Ionicons } from '@expo/vector-icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import * as Haptics from 'expo-haptics';
import { C, pnlColor } from '../theme/colors';
import { api } from '../api/client';
import { Trade, TradeStats, DashboardData } from '../types';

interface TradesResponse {
  trades: Trade[];
  count: number;
  stats: TradeStats;
}

type ActionStatus = 'idle' | 'loading' | 'success' | 'error';

interface ActionButtonProps {
  label: string;
  description: string;
  icon: string;
  color?: string;
  onPress: () => Promise<void>;
  dangerous?: boolean;
}

function ActionButton({ label, description, icon, color, onPress, dangerous }: ActionButtonProps) {
  const [status, setStatus] = useState<ActionStatus>('idle');

  const handlePress = async () => {
    if (dangerous) {
      Alert.alert(label, `Are you sure you want to: ${description}?`, [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Confirm',
          style: 'destructive',
          onPress: async () => {
            await run();
          },
        },
      ]);
      return;
    }
    await run();
  };

  const run = async () => {
    await Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    setStatus('loading');
    try {
      await onPress();
      setStatus('success');
      setTimeout(() => setStatus('idle'), 2500);
    } catch (e: any) {
      setStatus('error');
      Alert.alert('Error', e?.response?.data?.detail || e?.message || 'Action failed');
      setTimeout(() => setStatus('idle'), 3000);
    }
  };

  const accentColor = color || C.accent;

  return (
    <TouchableOpacity
      style={[styles.actionBtn, { borderColor: accentColor + '44' }]}
      onPress={handlePress}
      disabled={status === 'loading'}
    >
      <View style={[styles.actionIconWrap, { backgroundColor: accentColor + '20' }]}>
        {status === 'loading' ? (
          <ActivityIndicator size="small" color={accentColor} />
        ) : (
          <Text style={styles.actionIcon}>{status === 'success' ? '✓' : status === 'error' ? '✕' : icon}</Text>
        )}
      </View>
      <View style={styles.actionTextWrap}>
        <Text style={[styles.actionLabel, { color: status === 'success' ? C.pos : status === 'error' ? C.neg : C.text }]}>
          {status === 'success' ? 'Done!' : status === 'error' ? 'Failed' : label}
        </Text>
        <Text style={styles.actionDesc}>{description}</Text>
      </View>
      <Ionicons name="chevron-forward" size={16} color={C.textSubtle} />
    </TouchableOpacity>
  );
}

const EXIT_REASON_LABELS: Record<string, string> = {
  STOP_LOSS: 'Stop Loss', TRAILING_STOP: 'Trailing Stop', PROFIT_TARGET_1: 'Target 20%',
  PROFIT_TARGET_2: 'Target 40%', TIME_STOP: 'Time Stop', MARKET_REGIME: 'Market Regime',
  EARNINGS_AVOID: 'Earnings', CLIMAX_TOP: 'Climax Top', THREE_WEEKS_TIGHT: '3W Tight', MANUAL: 'Manual',
};

export function ActionsScreen() {
  const qc = useQueryClient();

  const dashQuery = useQuery<DashboardData>({
    queryKey: ['dashboard'],
    queryFn: () => api.dashboard().then((r) => r.data),
  });

  const tradesQuery = useQuery<TradesResponse>({
    queryKey: ['trades'],
    queryFn: () => api.trades(30).then((r) => r.data),
    refetchInterval: 120_000,
  });

  const refresh = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['dashboard'] });
    qc.invalidateQueries({ queryKey: ['trades'] });
  }, [qc]);

  const paused = dashQuery.data?.trading_paused ?? false;
  const exchanges = (dashQuery.data?.active_exchanges || 'ASX').split(',').map((e) => e.trim());
  const hasASX = exchanges.includes('ASX');
  const hasCrypto = exchanges.some((e) => e.startsWith('CRYPTO'));
  const cryptoKey = exchanges.find((e) => e.startsWith('CRYPTO')) || 'CRYPTO_INDEPENDENTRESERVE';

  const stats = tradesQuery.data?.stats;
  const trades = tradesQuery.data?.trades ?? [];

  return (
    <ScrollView
      style={styles.root}
      contentContainerStyle={styles.content}
      refreshControl={
        <RefreshControl refreshing={dashQuery.isFetching} onRefresh={refresh} tintColor={C.accent} />
      }
    >
      <StatusBar style="light" />
      <Text style={styles.pageTitle}>Actions</Text>

      {/* Trading toggle */}
      <Text style={styles.sectionTitle}>Trading Control</Text>
      {paused ? (
        <ActionButton
          label="Resume Trading"
          description="Re-enable automated entry checks"
          icon="▶"
          color={C.pos}
          onPress={async () => { await api.resume(); qc.invalidateQueries({ queryKey: ['dashboard'] }); }}
        />
      ) : (
        <ActionButton
          label="Pause Trading"
          description="Stop all automated entries (positions stay open)"
          icon="⏸"
          color={C.warn}
          dangerous
          onPress={async () => { await api.pause(); qc.invalidateQueries({ queryKey: ['dashboard'] }); }}
        />
      )}

      {/* Data & screening */}
      <Text style={styles.sectionTitle}>Data & Screening</Text>
      {hasASX && (
        <ActionButton
          label="Force Screen — ASX"
          description="Run Minervini screener on full ASX universe now"
          icon="🔍"
          onPress={() => api.forceScreen().then()}
        />
      )}
      {hasCrypto && (
        <ActionButton
          label="Force Screen — Crypto"
          description="Run VCP screener on crypto universe now"
          icon="🔍"
          color={C.warn}
          onPress={() => api.forceScreen().then()}
        />
      )}
      {hasASX && (
        <ActionButton
          label="Refresh Price Data — ASX"
          description="Fetch latest EOD data from yfinance"
          icon="📥"
          onPress={() => api.refreshData('ASX').then()}
        />
      )}
      {hasCrypto && (
        <ActionButton
          label="Refresh Price Data — Crypto"
          description="Fetch latest crypto prices from IR"
          icon="📥"
          color={C.warn}
          onPress={() => api.refreshData(cryptoKey).then()}
        />
      )}

      {/* Regime & system */}
      <Text style={styles.sectionTitle}>System</Text>
      {hasASX && (
        <ActionButton
          label="Evaluate ASX Regime"
          description="Recalculate BULL/CAUTION/BEAR for ASX"
          icon="🧭"
          onPress={() => api.evaluateRegime('ASX').then()}
        />
      )}
      {hasCrypto && (
        <ActionButton
          label="Evaluate Crypto Regime"
          description="Recalculate market regime for crypto"
          icon="🧭"
          color={C.warn}
          onPress={() => api.evaluateRegime(cryptoKey).then()}
        />
      )}
      <ActionButton
        label="Ping Worker"
        description="Trigger heartbeat check immediately"
        icon="💓"
        onPress={() => api.pingWorker().then()}
      />
      <ActionButton
        label="Send Daily Report"
        description="Push WhatsApp P&L summary now"
        icon="📲"
        onPress={() => api.sendReport().then()}
      />

      {/* Trade history */}
      <Text style={styles.sectionTitle}>Recent Trades (30 days)</Text>

      {stats && (
        <View style={styles.statsBar}>
          <View style={styles.statsItem}>
            <Text style={[styles.statsVal, { color: pnlColor(stats.total_pnl) }]}>
              {stats.total_pnl >= 0 ? '+' : ''}${Math.abs(stats.total_pnl).toFixed(0)}
            </Text>
            <Text style={styles.statsLabel}>Total P&L</Text>
          </View>
          <View style={styles.statsItem}>
            <Text style={styles.statsVal}>{stats.win_rate.toFixed(0)}%</Text>
            <Text style={styles.statsLabel}>Win Rate</Text>
          </View>
          <View style={styles.statsItem}>
            <Text style={[styles.statsVal, { color: C.pos }]}>+${stats.avg_winner.toFixed(0)}</Text>
            <Text style={styles.statsLabel}>Avg Win</Text>
          </View>
          <View style={styles.statsItem}>
            <Text style={[styles.statsVal, { color: C.neg }]}>${stats.avg_loser.toFixed(0)}</Text>
            <Text style={styles.statsLabel}>Avg Loss</Text>
          </View>
        </View>
      )}

      {tradesQuery.isLoading ? (
        <ActivityIndicator color={C.accent} style={{ marginVertical: 20 }} />
      ) : trades.length === 0 ? (
        <Text style={styles.noTrades}>No closed trades in the last 30 days.</Text>
      ) : (
        trades.map((t) => (
          <View key={t.id} style={styles.tradeRow}>
            <View style={styles.tradeLeft}>
              <Text style={styles.tradeTicker}>{t.ticker}</Text>
              <Text style={styles.tradeMeta}>
                {t.exit_date} · {t.hold_days}d · {EXIT_REASON_LABELS[t.exit_reason || ''] || t.exit_reason}
              </Text>
            </View>
            <View style={styles.tradeRight}>
              <Text style={[styles.tradePnl, { color: pnlColor(t.net_pnl_aud ?? 0) }]}>
                {(t.net_pnl_aud ?? 0) >= 0 ? '+' : ''}${Math.abs(t.net_pnl_aud ?? 0).toFixed(2)}
              </Text>
              <Text style={[styles.tradePct, { color: pnlColor(t.pnl_pct ?? 0) }]}>
                {(t.pnl_pct ?? 0) >= 0 ? '+' : ''}{(t.pnl_pct ?? 0).toFixed(1)}%
              </Text>
            </View>
          </View>
        ))
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  content: { padding: 16, paddingTop: 56, paddingBottom: 40 },
  pageTitle: { fontSize: 20, fontWeight: '700', color: C.text, marginBottom: 16 },
  sectionTitle: {
    fontSize: 11, color: C.textMuted, textTransform: 'uppercase',
    letterSpacing: 0.8, marginTop: 20, marginBottom: 8,
  },
  actionBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: C.surface,
    borderRadius: 12,
    padding: 14,
    marginBottom: 8,
    borderWidth: 1,
    gap: 12,
  },
  actionIconWrap: { width: 40, height: 40, borderRadius: 10, justifyContent: 'center', alignItems: 'center' },
  actionIcon: { fontSize: 18 },
  actionTextWrap: { flex: 1 },
  actionLabel: { fontSize: 15, fontWeight: '600', color: C.text },
  actionDesc: { fontSize: 12, color: C.textMuted, marginTop: 1 },
  statsBar: {
    flexDirection: 'row',
    backgroundColor: C.surface,
    borderRadius: 12,
    padding: 14,
    borderWidth: 1,
    borderColor: C.border,
    marginBottom: 12,
    justifyContent: 'space-between',
  },
  statsItem: { alignItems: 'center' },
  statsVal: { fontSize: 15, fontWeight: '700', color: C.text },
  statsLabel: { fontSize: 10, color: C.textSubtle, marginTop: 2, textTransform: 'uppercase' },
  noTrades: { color: C.textMuted, textAlign: 'center', padding: 20 },
  tradeRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    backgroundColor: C.surface,
    borderRadius: 10,
    padding: 12,
    marginBottom: 6,
    borderWidth: 1,
    borderColor: C.border,
  },
  tradeLeft: { flex: 1 },
  tradeTicker: { fontSize: 15, fontWeight: '600', color: C.text },
  tradeMeta: { fontSize: 11, color: C.textMuted, marginTop: 2 },
  tradeRight: { alignItems: 'flex-end' },
  tradePnl: { fontSize: 15, fontWeight: '700' },
  tradePct: { fontSize: 12, marginTop: 2 },
});
